"""Provider abstraction.

Nothing above this layer names a model. Call sites ask the config registry for
a *role* -- ``"rerank"``, ``"routing"`` -- and the registry hands back a model
spec, which this layer turns into a call. That indirection is what makes the
directive "Opus 5 everywhere now, Gemini for translation and routing later" a
YAML edit rather than a code change.

The important design decision here is ``Usage``. Anthropic and Google report
token consumption with different names and different shapes, and the cost
ledger needs one shape. Normalising at this boundary means the ledger has
exactly one code path, and adding a third provider later does not touch it.

Cache tokens are kept as separate fields rather than folded into ``input``.
They are priced very differently -- on Opus 5 a cache read is 10% of the input
rate and a cache write is 125% -- so collapsing them would overstate the cost
of a cached call by roughly tenfold and hide the entire benefit of prompt
caching, which is the single largest per-query saving in this system.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass
class Usage:
    """Normalised token accounting across providers."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    # Reasoning tokens, where the provider reports them separately. Billed as
    # output on both providers; tracked apart for visibility only.
    reasoning_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
        )

    @property
    def total(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )


@dataclass
class Completion:
    text: str
    usage: Usage
    model_id: str
    # Populated when the call used structured output.
    parsed: dict[str, Any] | None = None
    stop_reason: str | None = None


@dataclass
class EmbeddingResult:
    vectors: list[list[float]]
    usage: Usage
    model_id: str
    dimensions: int


class ProviderError(RuntimeError):
    """Provider call failed. Carries whether a retry is worth attempting."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class MissingCredentialError(ProviderError):
    """No API key configured.

    Distinct from a generic failure because it is the expected state in
    degraded mode, and the UI says something useful about it rather than
    showing a stack trace.
    """

    def __init__(self, env_var: str) -> None:
        super().__init__(
            f"{env_var} is not set. Ingestion estimates and the schema work "
            f"without it; live answering does not.",
            retryable=False,
        )
        self.env_var = env_var


@runtime_checkable
class LLMProvider(Protocol):
    """Chat-capable provider."""

    model_id: str

    async def complete(
        self,
        system: str,
        user: str,
        *,
        max_output_tokens: int = 2048,
        effort: str | None = None,
        cache_system: bool = False,
    ) -> Completion: ...

    async def complete_structured(
        self,
        system: str,
        user: str,
        schema: dict[str, Any],
        *,
        max_output_tokens: int = 2048,
        effort: str | None = None,
        cache_system: bool = False,
    ) -> Completion: ...

    def stream(
        self,
        system: str,
        user: str,
        *,
        max_output_tokens: int = 4096,
        effort: str | None = None,
        cache_system: bool = False,
    ): ...

    async def count_tokens(self, system: str, user: str) -> int: ...


@runtime_checkable
class EmbeddingProvider(Protocol):
    model_id: str
    dimensions: int

    async def embed(
        self, texts: list[str], *, is_query: bool = False
    ) -> EmbeddingResult: ...
