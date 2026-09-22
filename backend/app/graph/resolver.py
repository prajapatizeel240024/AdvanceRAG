"""Role resolution -- config to a live, costed, provenance-linked call.

This is the join between the YAML registry and the runtime. A node asks for a
role by name; the resolver returns the provider instance to call, the prompt to
render, and -- crucially -- the ``model_version_id`` and ``prompt_version_id``
that the run_step and cost_ledger rows will point at.

Those two ids are what make the provenance claim true rather than aspirational.
A node cannot make a model call without having resolved a role, and resolving a
role registers the model and prompt in the database, so there is no code path
that spends money without leaving a traceable record of what it spent it on.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from uuid import UUID

from app.core.settings import Settings
from app.providers.anthropic_provider import AnthropicProvider
from app.providers.base import MissingCredentialError
from app.providers.gemini_provider import GeminiChatProvider, GeminiEmbeddingProvider


@dataclass
class ResolvedRole:
    role: str
    provider: Any
    model_version_id: UUID
    prompt_version_id: UUID | None
    prompt: dict[str, Any]
    effort: str | None
    max_output_tokens: int


class RoleResolver:
    """Builds and caches providers, and registers versions on first use."""

    def __init__(self, bundle, settings: Settings) -> None:
        self.bundle = bundle
        self.settings = settings
        self._providers: dict[str, Any] = {}
        self._model_ids: dict[str, UUID] = {}
        self._prompt_ids: dict[str, UUID | None] = {}

    # -- registration ------------------------------------------------------

    async def register_all(self, conn) -> None:
        """Register every model, price and prompt in the bundle.

        Run once at startup so that a mid-request registration can never be
        the thing that fails a user's question.
        """
        models_cfg = self.bundle.models
        config_file_id = await self._config_file_id(conn, "models")

        for key, spec in models_cfg.get("models", {}).items():
            mid = await conn.fetchval(
                """
                INSERT INTO model_versions (provider, model_id, kind, dimensions, capabilities, config_file_id)
                VALUES ($1,$2,$3,$4,$5::jsonb,$6)
                ON CONFLICT (provider, model_id, kind, dimensions)
                    DO UPDATE SET capabilities = EXCLUDED.capabilities
                RETURNING id
                """,
                spec["provider"],
                spec["id"],
                spec["kind"],
                spec.get("dimensions"),
                _json(spec.get("supports", {})),
                config_file_id,
            )
            self._model_ids[key] = mid

        await self._register_prices(conn)
        await self._register_prompts(conn)
        await self._register_role_assignments(conn, config_file_id)

    async def _config_file_id(self, conn, name: str) -> UUID | None:
        f = self.bundle.files.get(name)
        if not f:
            return None
        return await conn.fetchval(
            "SELECT id FROM config_file_versions WHERE name=$1 AND content_hash=$2",
            f.name,
            f.hash,
        )

    async def _register_prices(self, conn) -> None:
        costs_cfg = self.bundle.costs
        config_file_id = await self._config_file_id(conn, "costs")
        by_model_id = {
            spec["id"]: key for key, spec in self.bundle.models.get("models", {}).items()
        }

        for price in costs_cfg.get("prices", []):
            key = by_model_id.get(price["model_id"])
            if key is None or key not in self._model_ids:
                continue
            mv = self._model_ids[key]

            tiers = price.get("tiered_by_prompt_tokens")
            rows = (
                [
                    (t.get("up_to"), t["input"], t["output"])
                    for t in tiers
                ]
                if tiers
                else [(None, price["input"], price.get("output", 0))]
            )
            for up_to, inp, out in rows:
                await conn.execute(
                    """
                    INSERT INTO model_prices (
                        model_version_id, effective_from, input_per_1m, output_per_1m,
                        cache_read_per_1m, cache_write_per_1m, tier_max_prompt_tokens,
                        confidence, source, config_file_id)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                    ON CONFLICT (model_version_id, effective_from, tier_max_prompt_tokens)
                        DO UPDATE SET input_per_1m = EXCLUDED.input_per_1m,
                                      output_per_1m = EXCLUDED.output_per_1m
                    """,
                    mv,
                    _as_date(price["effective_from"]),
                    inp,
                    out,
                    price.get("cache_read"),
                    price.get("cache_write"),
                    up_to,
                    price.get("confidence", "assumed"),
                    price.get("source"),
                    config_file_id,
                )

    async def _register_prompts(self, conn) -> None:
        for name, f in self.bundle.files.items():
            if not name.startswith("prompts/"):
                continue
            spec = f.parsed
            role = spec.get("role") or name.split("/", 1)[1]
            # Insert-or-select, never insert-or-update. prompt_versions is
            # append-only under a trigger, and `ON CONFLICT DO UPDATE` -- even
            # as a no-op written only to get RETURNING back -- is an UPDATE and
            # is correctly rejected. The CTE inserts when absent and falls
            # through to a SELECT when present, mutating nothing either way.
            pid = await conn.fetchval(
                """
                WITH ins AS (
                    INSERT INTO prompt_versions (role, semver, content_hash, template,
                                                 input_variables, config_file_id)
                    VALUES ($1,$2,$3,$4,$5,$6)
                    ON CONFLICT (role, content_hash) DO NOTHING
                    RETURNING id
                )
                SELECT id FROM ins
                UNION ALL
                SELECT id FROM prompt_versions WHERE role = $1 AND content_hash = $3
                LIMIT 1
                """,
                role,
                str(spec.get("version", "0.0.0")),
                f.hash,
                _template_text(spec),
                spec.get("input_variables") or [],
                await self._config_file_id(conn, name),
            )
            self._prompt_ids[role] = pid

    async def _register_role_assignments(self, conn, config_file_id) -> None:
        for role, assignment in self.bundle.models.get("roles", {}).items():
            key = assignment.get("model")
            if key not in self._model_ids:
                continue
            await conn.execute(
                """
                INSERT INTO role_assignments (config_file_id, role, model_version_id,
                                              effort, max_output_tokens, params)
                VALUES ($1,$2,$3,$4,$5,$6::jsonb)
                ON CONFLICT (config_file_id, role)
                    DO UPDATE SET model_version_id = EXCLUDED.model_version_id,
                                  effort = EXCLUDED.effort
                """,
                config_file_id,
                role,
                self._model_ids[key],
                assignment.get("effort"),
                assignment.get("max_output_tokens"),
                _json({k: v for k, v in assignment.items() if k not in
                       ("model", "effort", "max_output_tokens", "model_spec", "notes")}),
            )

    # -- resolution --------------------------------------------------------

    def model_version_id_for(self, role: str) -> UUID:
        """The registered model_version id for a role, without building a provider.

        Needed because the cost estimator must run with no API keys at all --
        that is the entire point of estimating before spending. It needs the
        model_version_id to look up which chunks are already embedded and which
        prices apply, both of which are database facts. Going through
        ``__call__`` would construct a provider and raise
        MissingCredentialError, making the keyless path impossible.
        """
        assignment = self.bundle.role(role)
        key = assignment["model"]
        if key not in self._model_ids:
            raise KeyError(f"model {key!r} for role {role!r} was never registered")
        return self._model_ids[key]

    def __call__(self, role: str) -> ResolvedRole:
        assignment = self.bundle.role(role)
        spec = assignment["model_spec"]
        key = assignment["model"]

        provider = self._providers.get(key)
        if provider is None:
            provider = self._build_provider(spec)
            self._providers[key] = provider

        prompt_spec: dict[str, Any] = {}
        prompt_id = self._prompt_ids.get(role)
        pf = self.bundle.files.get(f"prompts/{role}")
        if pf:
            prompt_spec = pf.parsed

        return ResolvedRole(
            role=role,
            provider=provider,
            model_version_id=self._model_ids[key],
            prompt_version_id=prompt_id,
            prompt=prompt_spec,
            effort=assignment.get("effort"),
            max_output_tokens=int(assignment.get("max_output_tokens") or 2048),
        )

    def _build_provider(self, spec: dict[str, Any]):
        provider_name = spec["provider"]
        if provider_name == "anthropic":
            return AnthropicProvider(self.settings.anthropic_api_key, spec["id"])
        if provider_name == "google":
            if spec["kind"] == "embedding":
                return GeminiEmbeddingProvider(
                    self.settings.gemini_api_key,
                    spec["id"],
                    dimensions=int(spec.get("dimensions", 1536)),
                    normalize_after_truncation=bool(
                        spec.get("normalize_after_truncation", True)
                    ),
                    max_batch_size=int(spec.get("max_batch_size", 100)),
                )
            return GeminiChatProvider(self.settings.gemini_api_key, spec["id"])
        raise ValueError(f"unknown provider {provider_name!r} in models.yaml")

    def missing_credentials(self) -> list[str]:
        """Which roles cannot run, and why. Drives the degraded-mode banner."""
        missing = []
        for role in self.bundle.models.get("roles", {}):
            try:
                self(role)
            except MissingCredentialError as exc:
                missing.append(f"{role}: {exc.env_var}")
            except Exception:  # noqa: BLE001
                pass
        return missing


def _template_text(spec: dict[str, Any]) -> str:
    """Store the full prompt text so a run is reconstructable from the DB alone."""
    parts = []
    if spec.get("system"):
        parts.append("=== SYSTEM ===\n" + spec["system"])
    if spec.get("user"):
        parts.append("=== USER ===\n" + spec["user"])
    return "\n\n".join(parts)


def _as_date(value: Any) -> date:
    """Coerce a YAML date to ``datetime.date``.

    PyYAML yields a ``date`` for an unquoted ``2026-01-01`` and a ``str`` for a
    quoted one, and asyncpg rejects the string. Normalising here means the
    quoting style in costs.yaml is a cosmetic choice rather than a silent
    startup failure.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _json(value: Any) -> str:
    import json

    return json.dumps(value, default=str)
