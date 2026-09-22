"""Gemini provider -- embeddings today, translation and routing in the target state.

Uses ``google-genai``, the current SDK. Not ``google-generativeai``, which is
the deprecated one and has a different call surface.

Two details in here are easy to get wrong and invisible when you do:

**Task types.** ``gemini-embedding-001`` is an *asymmetric* embedding model:
documents must be embedded with ``RETRIEVAL_DOCUMENT`` and queries with
``RETRIEVAL_QUERY``. Using one task type for both still produces perfectly
valid-looking vectors and measurably worse retrieval, with no error anywhere.
That is why ``embed()`` takes an explicit ``is_query`` flag rather than
defaulting.

**Matryoshka truncation.** The model emits 3072 dimensions natively. We store
1536, because pgvector's HNSW index rejects a ``vector`` column above 2000
dimensions -- verified on this machine. Truncating an MRL embedding leaves it
no longer unit-length, so it must be re-normalised before storage or cosine
distance is quietly wrong for every comparison.
"""

from __future__ import annotations

import json
import math
from typing import Any

from app.providers.base import (
    Completion,
    EmbeddingResult,
    MissingCredentialError,
    ProviderError,
    Usage,
)

_CHAT_DEFAULT = "gemini-3.8-flash"
_EMBED_DEFAULT = "gemini-embedding-001"


def _usage_from(meta: Any) -> Usage:
    """Map Gemini's ``usage_metadata`` onto the normalised shape."""
    if meta is None:
        return Usage()
    prompt = getattr(meta, "prompt_token_count", 0) or 0
    cached = getattr(meta, "cached_content_token_count", 0) or 0
    return Usage(
        # Gemini's prompt_token_count INCLUDES cached tokens, unlike Anthropic
        # where they are reported separately. Subtracting here keeps the ledger
        # consistent across providers; without it, cached tokens would be
        # billed twice.
        input_tokens=max(0, prompt - cached),
        output_tokens=getattr(meta, "candidates_token_count", 0) or 0,
        cache_read_tokens=cached,
        reasoning_tokens=getattr(meta, "thoughts_token_count", 0) or 0,
    )


def _normalise(vec: list[float]) -> list[float]:
    """Re-scale to unit length.

    Required after MRL truncation. Cosine distance on non-unit vectors is not
    the cosine of the angle, so skipping this silently degrades every
    similarity comparison in the system.
    """
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0:
        return vec
    return [v / norm for v in vec]


class GeminiEmbeddingProvider:
    def __init__(
        self,
        api_key: str | None,
        model_id: str = _EMBED_DEFAULT,
        dimensions: int = 1536,
        normalize_after_truncation: bool = True,
        max_batch_size: int = 100,
    ) -> None:
        if not api_key:
            raise MissingCredentialError("GEMINI_API_KEY")
        from google import genai
        from google.genai import types

        self._genai = genai
        self._types = types
        self._client = genai.Client(api_key=api_key)
        self.model_id = model_id
        self.dimensions = dimensions
        self._normalize = normalize_after_truncation
        self._max_batch = max_batch_size

    async def embed(
        self, texts: list[str], *, is_query: bool = False
    ) -> EmbeddingResult:
        if not texts:
            return EmbeddingResult([], Usage(), self.model_id, self.dimensions)

        task_type = "RETRIEVAL_QUERY" if is_query else "RETRIEVAL_DOCUMENT"
        vectors: list[list[float]] = []
        usage = Usage()

        for start in range(0, len(texts), self._max_batch):
            batch = texts[start : start + self._max_batch]
            try:
                res = await self._client.aio.models.embed_content(
                    model=self.model_id,
                    contents=batch,
                    config=self._types.EmbedContentConfig(
                        task_type=task_type,
                        output_dimensionality=self.dimensions,
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - SDK raises varied types
                raise ProviderError(
                    f"Gemini embedding failed: {exc}",
                    retryable="429" in str(exc) or "503" in str(exc),
                ) from exc

            for emb in res.embeddings:
                vec = list(emb.values)
                if self._normalize and len(vec) != 3072:
                    vec = _normalise(vec)
                if len(vec) != self.dimensions:
                    raise ProviderError(
                        f"expected {self.dimensions} dimensions, got {len(vec)}; "
                        "the pgvector column type will reject this"
                    )
                vectors.append(vec)

            meta = getattr(res, "usage_metadata", None)
            if meta is not None:
                usage = usage + Usage(
                    input_tokens=getattr(meta, "prompt_token_count", 0) or 0
                )
            else:
                # Some SDK versions omit usage on embedding responses. Fall
                # back to a local estimate rather than recording zero cost,
                # which would make the ledger quietly understate the bill.
                from app.ingest.chunker import estimate_tokens

                usage = usage + Usage(
                    input_tokens=sum(estimate_tokens(t) for t in batch)
                )

        return EmbeddingResult(
            vectors=vectors,
            usage=usage,
            model_id=self.model_id,
            dimensions=self.dimensions,
        )


class GeminiChatProvider:
    """Chat provider for the target state, where translation and routing move here."""

    def __init__(self, api_key: str | None, model_id: str = _CHAT_DEFAULT) -> None:
        if not api_key:
            raise MissingCredentialError("GEMINI_API_KEY")
        from google import genai
        from google.genai import types

        self._genai = genai
        self._types = types
        self._client = genai.Client(api_key=api_key)
        self.model_id = model_id

    # Gemini expresses reasoning depth as a thinking level rather than an
    # effort tier, so the config's effort value is mapped onto its nearest
    # equivalent. Keeping the mapping here means the YAML stays
    # provider-neutral.
    _EFFORT_TO_THINKING = {
        "low": "low",
        "medium": "medium",
        "high": "high",
        "xhigh": "high",
        "max": "high",
    }

    def _config(
        self,
        system: str,
        max_output_tokens: int,
        effort: str | None,
        schema: dict[str, Any] | None,
    ):
        kwargs: dict[str, Any] = {
            "system_instruction": system,
            "max_output_tokens": max_output_tokens,
        }
        if effort:
            level = self._EFFORT_TO_THINKING.get(effort)
            if level:
                kwargs["thinking_config"] = self._types.ThinkingConfig(
                    thinking_level=level
                )
        if schema:
            kwargs["response_mime_type"] = "application/json"
            kwargs["response_schema"] = schema
        return self._types.GenerateContentConfig(**kwargs)

    async def _call(
        self,
        system: str,
        user: str,
        *,
        max_output_tokens: int,
        effort: str | None,
        schema: dict[str, Any] | None = None,
    ) -> Completion:
        try:
            res = await self._client.aio.models.generate_content(
                model=self.model_id,
                contents=user,
                config=self._config(system, max_output_tokens, effort, schema),
            )
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(
                f"Gemini call failed: {exc}",
                retryable="429" in str(exc) or "503" in str(exc),
            ) from exc

        text = res.text or ""
        parsed = None
        if schema:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ProviderError(
                    f"structured output was not valid JSON: {exc}"
                ) from exc

        return Completion(
            text=text,
            usage=_usage_from(getattr(res, "usage_metadata", None)),
            model_id=self.model_id,
            parsed=parsed,
        )

    async def complete(
        self,
        system: str,
        user: str,
        *,
        max_output_tokens: int = 2048,
        effort: str | None = None,
        cache_system: bool = False,
    ) -> Completion:
        return await self._call(
            system, user, max_output_tokens=max_output_tokens, effort=effort
        )

    async def complete_structured(
        self,
        system: str,
        user: str,
        schema: dict[str, Any],
        *,
        max_output_tokens: int = 2048,
        effort: str | None = None,
        cache_system: bool = False,
    ) -> Completion:
        return await self._call(
            system,
            user,
            max_output_tokens=max_output_tokens,
            effort=effort,
            schema=schema,
        )

    async def stream(
        self,
        system: str,
        user: str,
        *,
        max_output_tokens: int = 4096,
        effort: str | None = None,
        cache_system: bool = False,
    ):
        stream = await self._client.aio.models.generate_content_stream(
            model=self.model_id,
            contents=user,
            config=self._config(system, max_output_tokens, effort, None),
        )
        usage = Usage()
        async for chunk in stream:
            if chunk.text:
                yield ("token", chunk.text)
            meta = getattr(chunk, "usage_metadata", None)
            if meta is not None:
                usage = _usage_from(meta)
        yield ("usage", usage)

    async def count_tokens(self, system: str, user: str) -> int:
        res = await self._client.aio.models.count_tokens(
            model=self.model_id, contents=f"{system}\n\n{user}"
        )
        return res.total_tokens
