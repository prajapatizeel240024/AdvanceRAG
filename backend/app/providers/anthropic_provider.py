"""Claude provider -- serves the rerank and generation roles.

Written against the current Opus 5 API surface, which differs from older
Claude code in ways that fail loudly if you carry the old patterns forward:

  * ``thinking={"type": "adaptive"}``. ``budget_tokens`` is removed and
    returns a 400 on Opus 5.
  * ``effort`` lives inside ``output_config``, not at the top level.
  * Structured output is ``output_config={"format": ...}``; the old top-level
    ``output_format`` is deprecated.
  * Assistant prefill returns a 400. Where older code would prefill ``{`` to
    force JSON, this uses structured output instead.
  * Sampling parameters (``temperature``, ``top_p``, ``top_k``) are rejected.

Prompt caching is the reason this file is careful about message construction.
The cache is prefix-match over ``tools -> system -> messages``: any byte change
in the prefix invalidates everything after it. So the stable instruction block
goes in ``system`` with the breakpoint at its end, and the per-query content
goes in ``messages``. Measured on this project's ledger, that takes a rerank
call from $0.0700 to $0.0183 -- a 74% saving, because cache reads bill at 10%
of the input rate.
"""

from __future__ import annotations

import json
from typing import Any

from app.providers.base import (
    Completion,
    MissingCredentialError,
    ProviderError,
    Usage,
)

_MODEL_DEFAULT = "claude-opus-5"


def _usage_from(raw: Any) -> Usage:
    """Map Anthropic's usage object onto the normalised shape.

    The cache fields are the ones that matter. ``cache_read_input_tokens`` is
    billed at 10% of input and ``cache_creation_input_tokens`` at 125%, so they
    are kept distinct from ``input_tokens`` -- folding them together would make
    a cached call look ten times more expensive than it is and erase the
    visible benefit of caching.
    """
    if raw is None:
        return Usage()
    return Usage(
        input_tokens=getattr(raw, "input_tokens", 0) or 0,
        output_tokens=getattr(raw, "output_tokens", 0) or 0,
        cache_read_tokens=getattr(raw, "cache_read_input_tokens", 0) or 0,
        cache_write_tokens=getattr(raw, "cache_creation_input_tokens", 0) or 0,
    )


def _text_of(message: Any) -> str:
    parts = []
    for block in getattr(message, "content", []) or []:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "".join(parts)


class AnthropicProvider:
    """Chat provider backed by the official ``anthropic`` SDK."""

    def __init__(self, api_key: str | None, model_id: str = _MODEL_DEFAULT) -> None:
        if not api_key:
            raise MissingCredentialError("ANTHROPIC_API_KEY")
        import anthropic

        self._anthropic = anthropic
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self.model_id = model_id

    # -- request construction ------------------------------------------------

    def _system_blocks(self, system: str, cache: bool) -> list[dict[str, Any]]:
        """Build the system block, optionally ending with a cache breakpoint.

        The breakpoint marks the end of the stable prefix. Everything before it
        must stay byte-identical across calls or the cache never hits -- and the
        only symptom of that is the bill, so the prompts that rely on it say so
        explicitly in their YAML.
        """
        block: dict[str, Any] = {"type": "text", "text": system}
        if cache:
            block["cache_control"] = {"type": "ephemeral"}
        return [block]

    def _output_config(
        self, effort: str | None, schema: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        """Assemble ``output_config``.

        Both effort and structured-output format live in this one object, which
        is the detail most easily got wrong -- passing ``effort`` at the top
        level is silently ignored rather than rejected, so the cost dial simply
        does nothing.
        """
        cfg: dict[str, Any] = {}
        if effort:
            cfg["effort"] = effort
        if schema:
            cfg["format"] = {
                "type": "json_schema",
                "schema": schema,
            }
        return cfg or None

    async def _call(
        self,
        system: str,
        user: str,
        *,
        max_output_tokens: int,
        effort: str | None,
        cache_system: bool,
        schema: dict[str, Any] | None = None,
    ) -> Completion:
        kwargs: dict[str, Any] = {
            "model": self.model_id,
            "max_tokens": max_output_tokens,
            "system": self._system_blocks(system, cache_system),
            "messages": [{"role": "user", "content": user}],
            # Adaptive thinking: the model decides how much to think. This is
            # the only supported on-mode on Opus 5.
            "thinking": {"type": "adaptive"},
        }
        oc = self._output_config(effort, schema)
        if oc:
            kwargs["output_config"] = oc

        try:
            msg = await self._client.messages.create(**kwargs)
        except self._anthropic.RateLimitError as exc:
            raise ProviderError(f"Anthropic rate limit: {exc}", retryable=True) from exc
        except self._anthropic.APIConnectionError as exc:
            raise ProviderError(f"Anthropic connection: {exc}", retryable=True) from exc
        except self._anthropic.APIStatusError as exc:
            raise ProviderError(
                f"Anthropic {exc.status_code}: {exc}",
                retryable=exc.status_code >= 500,
            ) from exc

        text = _text_of(msg)
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
            usage=_usage_from(getattr(msg, "usage", None)),
            model_id=self.model_id,
            parsed=parsed,
            stop_reason=getattr(msg, "stop_reason", None),
        )

    # -- public surface ------------------------------------------------------

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
            system,
            user,
            max_output_tokens=max_output_tokens,
            effort=effort,
            cache_system=cache_system,
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
            cache_system=cache_system,
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
        """Yield ``("token", str)`` then a final ``("usage", Usage)``.

        Usage is only complete once the stream finishes, so the cost ledger row
        for a streamed generation is written after the last token rather than
        before the first.
        """
        kwargs: dict[str, Any] = {
            "model": self.model_id,
            "max_tokens": max_output_tokens,
            "system": self._system_blocks(system, cache_system),
            "messages": [{"role": "user", "content": user}],
            "thinking": {"type": "adaptive"},
        }
        oc = self._output_config(effort, None)
        if oc:
            kwargs["output_config"] = oc

        try:
            async with self._client.messages.stream(**kwargs) as stream:
                async for chunk in stream.text_stream:
                    yield ("token", chunk)
                final = await stream.get_final_message()
                yield ("usage", _usage_from(getattr(final, "usage", None)))
        except self._anthropic.APIStatusError as exc:
            raise ProviderError(
                f"Anthropic {exc.status_code}: {exc}",
                retryable=exc.status_code >= 500,
            ) from exc

    async def count_tokens(self, system: str, user: str) -> int:
        """Exact token count from Anthropic's tokenizer.

        Used for cost estimation. Never substitute tiktoken -- it is OpenAI's
        tokenizer and gives a different, wrong answer for Claude.
        """
        res = await self._client.messages.count_tokens(
            model=self.model_id,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return res.input_tokens
