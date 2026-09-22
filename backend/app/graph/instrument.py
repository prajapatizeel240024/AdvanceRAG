"""Per-node instrumentation.

Every node in the graph must record: which prompt version and model version it
used, how long it took, how many tokens it consumed, and what that cost. That
is eleven nodes' worth of identical bookkeeping, and bookkeeping copy-pasted
eleven times is bookkeeping that will be wrong in at least one of them --
usually the error path, which is exactly where you most want the record.

So it lives here once, as a context manager. A node wraps its work in
``step()`` and the run_step row, the cost_ledger row and the provenance links
are written for it, including when the node raises.

Note what this module does NOT do: compute cost. It writes token counts and the
unit prices in force, and the database's generated column does the arithmetic.
Duplicating that formula in Python would give two sources of truth for the
number the whole cost story rests on.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from app.providers.base import Usage


@dataclass
class StepRecord:
    """Mutable handle a node fills in as it works."""

    node: str
    step_index: int
    prompt_version_id: UUID | None = None
    model_version_id: UUID | None = None
    effort: str | None = None
    rendered_hash: str | None = None
    usage: Usage = field(default_factory=Usage)
    output: dict[str, Any] | None = None
    operation: str = "chat"
    status: str = "ok"
    error: str | None = None

    def record_usage(self, usage: Usage) -> None:
        self.usage = self.usage + usage


@asynccontextmanager
async def step(
    conn,
    *,
    node: str,
    step_index: int,
    tenant_id: UUID | str,
    user_id: UUID | str | None = None,
    query_run_id: UUID | str | None = None,
    ingestion_run_id: UUID | str | None = None,
):
    """Open a run_step, yield a handle, and close it with cost on exit.

    Writes the step row even when the node raises, because a failed step is
    part of the provenance record -- a run that shows a gap where reranking
    should be is far harder to diagnose than one that shows reranking failed.
    """
    rec = StepRecord(node=node, step_index=step_index)
    started = time.perf_counter()
    try:
        yield rec
    except Exception as exc:  # noqa: BLE001 - re-raised below
        rec.status = "error"
        rec.error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        latency_ms = int((time.perf_counter() - started) * 1000)
        step_id = await conn.fetchval(
            """
            INSERT INTO run_steps (
                tenant_id, query_run_id, ingestion_run_id, node, step_index,
                prompt_version_id, model_version_id, effort, rendered_hash,
                status, error, latency_ms, output
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13::jsonb)
            RETURNING id
            """,
            str(tenant_id),
            str(query_run_id) if query_run_id else None,
            str(ingestion_run_id) if ingestion_run_id else None,
            node,
            step_index,
            str(rec.prompt_version_id) if rec.prompt_version_id else None,
            str(rec.model_version_id) if rec.model_version_id else None,
            rec.effort,
            rec.rendered_hash,
            rec.status,
            rec.error,
            latency_ms,
            _json(rec.output),
        )

        if rec.model_version_id and rec.usage.total > 0:
            await _write_cost(
                conn,
                tenant_id=tenant_id,
                user_id=user_id,
                run_step_id=step_id,
                query_run_id=query_run_id,
                ingestion_run_id=ingestion_run_id,
                model_version_id=rec.model_version_id,
                operation=rec.operation,
                usage=rec.usage,
            )


async def _write_cost(
    conn,
    *,
    tenant_id: UUID | str,
    user_id: UUID | str | None,
    run_step_id: UUID,
    query_run_id: UUID | str | None,
    ingestion_run_id: UUID | str | None,
    model_version_id: UUID,
    operation: str,
    usage: Usage,
) -> None:
    """Write one ledger row, resolving the price in force in SQL.

    ``fn_resolve_price`` is used rather than a Python price lookup so the
    estimator, the live path and the reporting views all resolve prices the
    same way. The unit prices are then frozen into the row, so a later price
    change cannot retroactively rewrite what this call cost.
    """
    price = await conn.fetchrow(
        "SELECT * FROM fn_resolve_price($1, CURRENT_DATE, $2)",
        str(model_version_id),
        usage.input_tokens + usage.cache_read_tokens,
    )
    if price is None or price["id"] is None:
        raise RuntimeError(
            f"no price registered for model_version {model_version_id}; "
            "refusing to record an uncosted call"
        )

    await conn.execute(
        """
        INSERT INTO cost_ledger (
            tenant_id, user_id, run_step_id, query_run_id, ingestion_run_id,
            model_version_id, price_id, operation,
            input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
            input_per_1m, output_per_1m, cache_read_per_1m, cache_write_per_1m
        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
        """,
        str(tenant_id),
        str(user_id) if user_id else None,
        run_step_id,
        str(query_run_id) if query_run_id else None,
        str(ingestion_run_id) if ingestion_run_id else None,
        str(model_version_id),
        price["id"],
        operation,
        usage.input_tokens,
        usage.output_tokens,
        usage.cache_read_tokens,
        usage.cache_write_tokens,
        price["input_per_1m"],
        price["output_per_1m"],
        price["cache_read_per_1m"] or 0,
        price["cache_write_per_1m"] or 0,
    )


def _json(value: Any) -> str | None:
    if value is None:
        return None
    import json

    return json.dumps(value, default=str)
