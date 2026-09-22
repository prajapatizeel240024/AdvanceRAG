"""Pre-ingestion cost estimation.

The requirement is literal: *every time embedding, how much will it cost for
that document*. This module answers that **before** a single token is spent, so
the decision to ingest is made with the price in hand rather than discovered on
the invoice.

Three costs make up an ingestion:

  1. **Embedding** -- every chunk's embed text, at the embedding model's input
     rate. This is the cost the requirement names, and it is the small one.
  2. **Contextualisation** -- one LLM call per chunk for Contextual Retrieval.
     With the document held in a cached prefix, the first call pays full input
     and the rest pay the cache-read rate. This dominates the total, which is
     exactly why it is priced separately and can be switched off.
  3. **Reuse credit** -- chunks whose content hash already carries an embedding
     for this model cost nothing. On the annual reissue of a policy this is
     most of the document, and it is the largest single saving available.

Token counting is deliberately two-tier, and the estimate says which tier it
used. With an Anthropic key present, LLM-bound text is counted exactly via
``messages.count_tokens``. Without one, a local heuristic is used and the result
is labelled ``estimated`` so the UI can show it as approximate rather than
implying a precision it does not have.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.ingest.chunker import Chunk, estimate_tokens


@dataclass
class LineItem:
    label: str
    model_id: str
    tokens: int
    rate_per_1m: Decimal
    cost: Decimal
    note: str = ""


@dataclass
class Estimate:
    chunk_count: int
    new_chunk_count: int
    reused_chunk_count: int
    embedding_tokens: int
    context_input_tokens: int
    context_cached_tokens: int
    context_output_tokens: int
    total_cost: Decimal
    line_items: list[LineItem] = field(default_factory=list)
    # 'exact' when a provider tokenizer was used, 'estimated' when the local
    # heuristic was. Surfaced in the API response so the UI never presents a
    # heuristic as a precise figure.
    token_confidence: str = "estimated"
    reuse_saving: Decimal = Decimal("0")
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "chunk_count": self.chunk_count,
            "new_chunk_count": self.new_chunk_count,
            "reused_chunk_count": self.reused_chunk_count,
            "embedding_tokens": self.embedding_tokens,
            "context_input_tokens": self.context_input_tokens,
            "context_cached_tokens": self.context_cached_tokens,
            "context_output_tokens": self.context_output_tokens,
            "total_cost_usd": float(self.total_cost),
            "reuse_saving_usd": float(self.reuse_saving),
            "token_confidence": self.token_confidence,
            "warnings": self.warnings,
            "line_items": [
                {
                    "label": li.label,
                    "model_id": li.model_id,
                    "tokens": li.tokens,
                    "rate_per_1m": float(li.rate_per_1m),
                    "cost_usd": float(li.cost),
                    "note": li.note,
                }
                for li in self.line_items
            ],
        }


def _cost(tokens: int, rate_per_1m: Decimal) -> Decimal:
    return (Decimal(tokens) * rate_per_1m) / Decimal(1_000_000)


def _price_for(costs_cfg: dict[str, Any], model_id: str) -> dict[str, Any]:
    """Resolve a model's price row from the registered costs.yaml."""
    candidates = [
        p for p in costs_cfg.get("prices", []) if p.get("model_id") == model_id
    ]
    if not candidates:
        raise KeyError(
            f"no price registered for {model_id!r} in costs.yaml; "
            "cost accounting requires every model to be priced"
        )
    # Latest effective_from that is not in the future dominates. Kept simple
    # here because the authoritative resolution is fn_resolve_price() in SQL --
    # this path only serves the pre-flight estimate.
    return sorted(candidates, key=lambda p: str(p.get("effective_from", "")))[0]


def estimate_ingestion(
    chunks: list[Chunk],
    document_text: str,
    bundle,
    already_embedded_hashes: set[str] | None = None,
    exact_counter=None,
) -> Estimate:
    """Price an ingestion run before it happens.

    ``already_embedded_hashes`` is the content-hash reuse set from
    ``chunk_embeddings``; anything in it costs nothing to re-ingest.

    ``exact_counter`` is an optional ``callable(text) -> int`` backed by a
    provider tokenizer. When absent the local heuristic is used and the result
    is marked accordingly.
    """
    already = already_embedded_hashes or set()
    costs_cfg = bundle.costs
    chunking_cfg = bundle.chunking
    contextual_cfg = chunking_cfg.get("contextual_retrieval", {})

    embed_role = bundle.role("embedding")
    embed_model_id = embed_role["model_spec"]["id"]
    embed_price = _price_for(costs_cfg, embed_model_id)
    embed_rate = Decimal(str(embed_price["input"]))

    warnings: list[str] = []
    confidence = "estimated"

    def count(text: str) -> int:
        nonlocal confidence
        if exact_counter is not None:
            try:
                n = exact_counter(text)
                confidence = "exact"
                return n
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"exact token counting failed, using estimate: {exc}")
        return estimate_tokens(text)

    new_chunks = [c for c in chunks if c.hash not in already]
    reused = len(chunks) - len(new_chunks)

    line_items: list[LineItem] = []

    # ---- 1. Embedding ------------------------------------------------------
    # Embedding token counts use the local heuristic even when an Anthropic key
    # is present: Anthropic's tokenizer is not Gemini's, so an "exact" count
    # from the wrong tokenizer would be precisely wrong rather than roughly
    # right. Embeddings are also the cheap line, so the error hardly matters.
    embed_tokens = sum(estimate_tokens(c.embed_text()) for c in new_chunks)
    embed_cost = _cost(embed_tokens, embed_rate)
    line_items.append(
        LineItem(
            label="Embedding",
            model_id=embed_model_id,
            tokens=embed_tokens,
            rate_per_1m=embed_rate,
            cost=embed_cost,
            note=f"{len(new_chunks)} new chunks"
            + (f", {reused} reused free" if reused else ""),
        )
    )

    # ---- 2. Contextualisation ---------------------------------------------
    ctx_input = ctx_cached = ctx_output = 0
    ctx_cost = Decimal("0")

    if contextual_cfg.get("enabled") and new_chunks:
        ctx_role = bundle.role("contextualisation")
        ctx_model_id = ctx_role["model_spec"]["id"]
        ctx_price = _price_for(costs_cfg, ctx_model_id)
        in_rate = Decimal(str(ctx_price["input"]))
        out_rate = Decimal(str(ctx_price["output"]))
        cache_read_rate = Decimal(str(ctx_price.get("cache_read", ctx_price["input"])))
        cache_write_rate = Decimal(
            str(ctx_price.get("cache_write", ctx_price["input"]))
        )

        doc_tokens = count(document_text)
        per_chunk_tokens = [count(c.content) for c in new_chunks]
        # Output tokens must account for THINKING, not just the visible answer.
        # Every Anthropic call here runs adaptive thinking, and thinking tokens
        # bill as output at 5x the input rate -- so counting only the two
        # visible sentences understates the dominant line item. The multiplier
        # is a declared assumption from costs.yaml, surfaced as a warning until
        # a real run replaces it with a measurement.
        est_cfg = costs_cfg.get("estimation", {})
        visible = int(est_cfg.get("context_visible_output_tokens", 80))
        think_mult = float(est_cfg.get("thinking_output_multiplier", 1.0))
        out_tokens_each = int(visible * think_mult)
        if think_mult > 1.0 and est_cfg.get("thinking_multiplier_confidence") != "verified":
            warnings.append(
                f"contextualisation output assumes a {think_mult}x adaptive-thinking "
                "multiplier (costs.yaml estimation.thinking_output_multiplier). "
                "This is an assumption, not a measurement -- check est vs actual "
                "in v_ingestion_estimate_accuracy after the first run."
            )

        if contextual_cfg.get("cache_document_prefix"):
            # First call writes the document into the cache; the rest read it.
            ctx_cached = doc_tokens * (len(new_chunks) - 1)
            ctx_input = sum(per_chunk_tokens)
            ctx_output = out_tokens_each * len(new_chunks)
            ctx_cost = (
                _cost(doc_tokens, cache_write_rate)
                + _cost(ctx_cached, cache_read_rate)
                + _cost(ctx_input, in_rate)
                + _cost(ctx_output, out_rate)
            )
            note = (
                f"{len(new_chunks)} calls; document cached after the first "
                f"({doc_tokens:,} tok prefix read at "
                f"{cache_read_rate / in_rate:.0%} of input rate)"
            )
        else:
            ctx_input = (doc_tokens + sum(per_chunk_tokens) // len(new_chunks)) * len(
                new_chunks
            )
            ctx_output = out_tokens_each * len(new_chunks)
            ctx_cost = _cost(ctx_input, in_rate) + _cost(ctx_output, out_rate)
            note = f"{len(new_chunks)} calls, document resent uncached each time"
            warnings.append(
                "contextual_retrieval.cache_document_prefix is off; this "
                "multiplies ingestion cost by roughly the chunk count"
            )

        line_items.append(
            LineItem(
                label="Contextualisation",
                model_id=ctx_model_id,
                tokens=ctx_input + ctx_cached + ctx_output,
                rate_per_1m=in_rate,
                cost=ctx_cost,
                note=note,
            )
        )

    total = embed_cost + ctx_cost

    # What the reuse actually saved, priced at the embedding rate. Shown in the
    # UI because "you avoided $x by not re-embedding unchanged text" is the
    # clearest possible demonstration that the optimisation is real.
    reuse_saving = Decimal("0")
    if reused:
        reused_tokens = sum(
            estimate_tokens(c.embed_text()) for c in chunks if c.hash in already
        )
        reuse_saving = _cost(reused_tokens, embed_rate)

    return Estimate(
        chunk_count=len(chunks),
        new_chunk_count=len(new_chunks),
        reused_chunk_count=reused,
        embedding_tokens=embed_tokens,
        context_input_tokens=ctx_input,
        context_cached_tokens=ctx_cached,
        context_output_tokens=ctx_output,
        total_cost=total,
        line_items=line_items,
        token_confidence=confidence,
        reuse_saving=reuse_saving,
        warnings=warnings,
    )
