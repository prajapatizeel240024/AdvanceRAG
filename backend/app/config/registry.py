"""YAML configuration registry -- canonicalise, hash, register, pin.

This module implements the project's first requirement: *record in the database
which version we used*.

The flow, once per process start:

    1. Read every YAML file under ``config/``.
    2. Canonicalise each one (sorted keys, LF, no trailing whitespace) and take
       the sha256 of the result.
    3. Upsert each into ``config_file_versions``, keyed by (name, content_hash).
       Unchanged files resolve to their existing row and register nothing new.
    4. Derive a bundle hash from the sorted member hashes and upsert a
       ``config_bundles`` row.
    5. Pin that bundle id for the process lifetime.

Every run then stores that bundle id, so "which config produced this answer?"
is a foreign key rather than an inference.

Why hash as well as semver: the ``version:`` field in a YAML file is a claim a
human makes, and humans edit files without bumping versions. The hash is the
identity; the semver is the label. Canonicalisation matters because otherwise
reordering two keys yields a "new" configuration that is semantically
identical, and the registry fills with noise that makes real changes hard to
spot.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml

# ``prompts/rerank.yaml`` registers under the logical name ``prompts/rerank``,
# so a prompt file is addressable independently of the directory layout.
CONFIG_GLOB = "**/*.yaml"


def canonicalise(parsed: Any) -> str:
    """Render parsed YAML to a stable, hashable string.

    JSON with sorted keys is used rather than re-dumping YAML because
    ``yaml.safe_dump`` output varies with anchors, flow style and line width,
    which would produce different hashes for identical content.
    """
    return json.dumps(parsed, sort_keys=True, separators=(",", ":"), default=str)


def content_hash(parsed: Any) -> str:
    return hashlib.sha256(canonicalise(parsed).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ConfigFile:
    name: str
    semver: str
    hash: str
    raw_text: str
    parsed: dict[str, Any]


@dataclass
class ConfigBundle:
    """A resolved, pinned configuration for one process."""

    id: UUID
    bundle_hash: str
    files: dict[str, ConfigFile] = field(default_factory=dict)

    def get(self, name: str) -> dict[str, Any]:
        if name not in self.files:
            raise KeyError(
                f"config file {name!r} is not in bundle {self.bundle_hash[:12]}; "
                f"present: {sorted(self.files)}"
            )
        return self.files[name].parsed

    # -- typed accessors for the files the code actually reaches for ---------

    @property
    def models(self) -> dict[str, Any]:
        return self.get("models")

    @property
    def retrieval(self) -> dict[str, Any]:
        return self.get("retrieval")

    @property
    def chunking(self) -> dict[str, Any]:
        return self.get("chunking")

    @property
    def costs(self) -> dict[str, Any]:
        return self.get("costs")

    def role(self, role_name: str) -> dict[str, Any]:
        """Resolve a role to its model entry plus call parameters.

        Nothing in the codebase names a model. Call sites ask for a role --
        ``"rerank"``, ``"routing"`` -- and this resolves it through the
        registered config. Flipping the system from all-Opus-5 to the
        Gemini-mixed target state is therefore a YAML change, not a code change.
        """
        models_cfg = self.models
        roles = models_cfg.get("roles", {})
        if role_name not in roles:
            raise KeyError(
                f"role {role_name!r} is not assigned in models.yaml; "
                f"assigned: {sorted(roles)}"
            )
        assignment = dict(roles[role_name])
        model_key = assignment.get("model")
        catalogue = models_cfg.get("models", {})
        if model_key not in catalogue:
            raise KeyError(
                f"role {role_name!r} points at model key {model_key!r}, "
                f"which is not in the catalogue: {sorted(catalogue)}"
            )
        assignment["model_spec"] = catalogue[model_key]
        return assignment


def load_config_files(config_dir: Path) -> list[ConfigFile]:
    """Read and hash every YAML file under ``config_dir``."""
    files: list[ConfigFile] = []
    for path in sorted(config_dir.glob(CONFIG_GLOB)):
        if path.name.startswith("."):
            continue
        raw = path.read_text(encoding="utf-8")
        parsed = yaml.safe_load(raw)
        if parsed is None:
            continue
        if not isinstance(parsed, dict):
            raise ValueError(f"{path}: top level of a config file must be a mapping")

        name = path.relative_to(config_dir).with_suffix("").as_posix()
        semver = str(parsed.get("version", "0.0.0"))
        files.append(
            ConfigFile(
                name=name,
                semver=semver,
                hash=content_hash(parsed),
                raw_text=raw,
                parsed=parsed,
            )
        )

    if not files:
        raise FileNotFoundError(f"no YAML configuration found under {config_dir}")
    return files


def bundle_hash_of(files: list[ConfigFile]) -> str:
    """Hash the bundle from its members.

    Sorted by name so member ordering cannot change the bundle identity, and
    built from ``name:hash`` pairs so a file being renamed counts as a change.
    """
    joined = "\n".join(f"{f.name}:{f.hash}" for f in sorted(files, key=lambda f: f.name))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


async def register_bundle(
    conn,
    config_dir: Path,
    label: str | None = None,
    git_sha: str | None = None,
) -> ConfigBundle:
    """Register the on-disk config and return the pinned bundle.

    Idempotent: running twice against unchanged files registers nothing new and
    returns the same bundle id. That property is what makes it safe to call on
    every process start.
    """
    files = load_config_files(config_dir)
    bhash = bundle_hash_of(files)

    existing = await conn.fetchrow(
        "SELECT id FROM config_bundles WHERE bundle_hash = $1", bhash
    )
    if existing:
        return ConfigBundle(
            id=existing["id"],
            bundle_hash=bhash,
            files={f.name: f for f in files},
        )

    file_ids: list[UUID] = []
    for f in files:
        # Insert-or-select. These tables are append-only under a trigger, so
        # `ON CONFLICT DO UPDATE` is rejected even when the update is a no-op
        # written purely to get RETURNING back. The CTE inserts when the row is
        # absent and selects when it is present, without ever issuing an UPDATE.
        row = await conn.fetchrow(
            """
            WITH ins AS (
                INSERT INTO config_file_versions (name, semver, content_hash, content, parsed)
                VALUES ($1, $2, $3, $4, $5::jsonb)
                ON CONFLICT (name, content_hash) DO NOTHING
                RETURNING id
            )
            SELECT id FROM ins
            UNION ALL
            SELECT id FROM config_file_versions WHERE name = $1 AND content_hash = $3
            LIMIT 1
            """,
            f.name,
            f.semver,
            f.hash,
            f.raw_text,
            json.dumps(f.parsed, default=str),
        )
        file_ids.append(row["id"])

    bundle = await conn.fetchrow(
        """
        WITH ins AS (
            INSERT INTO config_bundles (bundle_hash, label, git_sha)
            VALUES ($1, $2, $3)
            ON CONFLICT (bundle_hash) DO NOTHING
            RETURNING id
        )
        SELECT id FROM ins
        UNION ALL
        SELECT id FROM config_bundles WHERE bundle_hash = $1
        LIMIT 1
        """,
        bhash,
        label,
        git_sha,
    )
    bundle_id = bundle["id"]

    for fid in file_ids:
        await conn.execute(
            """
            INSERT INTO config_bundle_members (bundle_id, config_file_id)
            VALUES ($1, $2) ON CONFLICT DO NOTHING
            """,
            bundle_id,
            fid,
        )

    return ConfigBundle(
        id=bundle_id, bundle_hash=bhash, files={f.name: f for f in files}
    )
