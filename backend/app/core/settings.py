"""Process settings.

Deliberately thin. Anything that shapes *behaviour* -- models, prompts,
retrieval parameters, thresholds, prices -- belongs in versioned YAML under
``config/`` so it is hashed and recorded against every run. This module holds
only what YAML cannot: where the database is, and which credentials to use.

The test is simple: if changing a value should change an answer, it does not
belong here, because a value living here leaves no trace in the provenance
record.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- Database ---------------------------------------------------------
    # Note the role. The application must NOT connect as a superuser: a
    # superuser (and any role with BYPASSRLS) ignores row-level security
    # entirely, including FORCE ROW LEVEL SECURITY, which would make every
    # tenant policy inert while appearing to work.
    database_url: str = Field(
        default="postgresql://rag_app@localhost:5432/travel_rag",
        description="asyncpg-compatible DSN; must use a non-superuser role.",
    )
    db_pool_min: int = 1
    db_pool_max: int = 10

    # ---- Providers --------------------------------------------------------
    # Both may be absent. With no keys the system still runs in degraded mode:
    # schema, config registration, parsing, chunking, token counting and
    # ingestion cost estimation all work. Only live embedding and live
    # answering require credentials.
    anthropic_api_key: str | None = None
    gemini_api_key: str | None = None

    # ---- Runtime ----------------------------------------------------------
    config_dir: Path = REPO_ROOT / "config"
    corpus_dir: Path = REPO_ROOT / "corpus"
    app_env: str = "local"
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    # ---- Default tenant ---------------------------------------------------
    # One user with one document today. The schema is multi-tenant throughout,
    # so this is a seeded default rather than an assumption baked into queries.
    default_tenant_slug: str = "acme"
    default_user_email: str = "user@example.com"

    @property
    def has_anthropic(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def has_gemini(self) -> bool:
        return bool(self.gemini_api_key)

    @property
    def degraded(self) -> bool:
        """True when at least one provider credential is missing.

        Surfaced all the way to the UI, because a system that silently
        stops calling models is indistinguishable from one that is broken.
        """
        return not (self.has_anthropic and self.has_gemini)

    def missing_keys(self) -> list[str]:
        missing = []
        if not self.has_anthropic:
            missing.append("ANTHROPIC_API_KEY")
        if not self.has_gemini:
            missing.append("GEMINI_API_KEY")
        return missing


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
