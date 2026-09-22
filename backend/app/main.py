"""FastAPI application -- deliberately thin.

Every handler in this file does the same four things: validate input, open a
tenant-scoped transaction, call one database function or drive the graph, and
serialise the result. There is no ranking, no cost arithmetic, no tenancy
filtering and no business logic here.

The test to apply when reading it: if a handler contains a calculation whose
result a user would see, it is in the wrong place. Ranking belongs in
``migrations/004``, cost arithmetic in the ``cost_ledger`` generated column and
its trigger, tenancy in the RLS policies, and pipeline control flow in the
LangGraph graph.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.api.deps import Principal, current_principal
from app.config.registry import register_bundle
from app.core.settings import Settings, get_settings
from app.db.pool import close_pool, init_pool, tenant_tx
from app.graph.nodes import Nodes, prompt_hash, question_hash, render
from app.graph.resolver import RoleResolver
from app.graph.state import new_state
from app.ingest.pipeline import Ingestor, section_outline
from app.providers.base import MissingCredentialError, ProviderError


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Register config once, at startup.

    Pinning the bundle here rather than per request means a YAML edit mid-flight
    cannot leave a single run attributed to two different config versions.
    """
    s = get_settings()
    app.state.pool = await init_pool()

    async with app.state.pool.acquire() as conn:
        bundle = await register_bundle(conn, s.config_dir, label=s.app_env)
        resolver = RoleResolver(bundle, s)
        await resolver.register_all(conn)

    app.state.bundle = bundle
    app.state.resolver = resolver
    app.state.settings = s
    yield
    await close_pool()


app = FastAPI(title="Travel Policy RAG", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    conversation_id: UUID | None = None
    document_version_id: UUID | None = None


class EstimateRequest(BaseModel):
    path: str = Field(description="Path to the source document, relative to repo root.")
    slug: str = "travel-policy"
    title: str = "Northwind Global Travel Policy"
    version_label: str = "4.2"


class IngestRequest(EstimateRequest):
    # Ingestion spends money, so the caller must acknowledge the estimate it was
    # shown. Requiring an explicit flag rather than defaulting to true is what
    # makes the estimate a decision point instead of a formality.
    approve: bool = False
    contextualise: bool | None = None


# ---------------------------------------------------------------------------
# Meta
# ---------------------------------------------------------------------------
@app.get("/api/health")
async def health(request: Request, s: Settings = Depends(get_settings)):
    async with request.app.state.pool.acquire() as conn:
        db_ok = await conn.fetchval("SELECT 1") == 1
    return {
        "ok": db_ok,
        "degraded": s.degraded,
        "missing_keys": s.missing_keys(),
        "config_bundle": request.app.state.bundle.bundle_hash[:16],
    }


@app.get("/api/config")
async def config_info(request: Request):
    """What configuration this process is running.

    Backs the provenance panel in the UI: the answer to "which version are we
    on right now", as opposed to "which version produced that answer", which is
    ``/api/runs/{id}/provenance``.
    """
    bundle = request.app.state.bundle
    async with request.app.state.pool.acquire() as conn:
        roles = await conn.fetch(
            """
            SELECT ra.role, mv.provider, mv.model_id, ra.effort
            FROM role_assignments ra
            JOIN model_versions mv ON mv.id = ra.model_version_id
            ORDER BY ra.role
            """
        )
    return {
        "bundle_id": str(bundle.id),
        "bundle_hash": bundle.bundle_hash,
        "files": [
            {"name": f.name, "version": f.semver, "hash": f.hash[:16]}
            for f in sorted(bundle.files.values(), key=lambda f: f.name)
        ],
        "roles": [dict(r) for r in roles],
    }


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------
@app.get("/api/documents")
async def list_documents(p: Principal = Depends(current_principal)):
    async with tenant_tx(p.tenant_id, p.user_id) as conn:
        rows = await conn.fetch(
            """
            SELECT d.id, d.slug, d.title,
                   dv.id AS version_id, dv.version_label, dv.status,
                   dv.byte_size, dv.created_at,
                   (SELECT count(*) FROM chunks c WHERE c.document_version_id = dv.id) AS chunks,
                   (SELECT count(*) FROM chunk_embeddings e
                      JOIN chunks c2 ON c2.id = e.chunk_id
                     WHERE c2.document_version_id = dv.id) AS embedded,
                   ir.est_cost_usd, ir.actual_cost_usd, ir.reused_chunks
            FROM documents d
            JOIN document_versions dv ON dv.document_id = d.id
            LEFT JOIN LATERAL (
                SELECT * FROM ingestion_runs
                WHERE document_version_id = dv.id
                ORDER BY started_at DESC LIMIT 1
            ) ir ON true
            ORDER BY dv.created_at DESC
            """
        )
    return [dict(r) for r in rows]


@app.post("/api/documents/estimate")
async def estimate_document(
    body: EstimateRequest,
    request: Request,
    p: Principal = Depends(current_principal),
    s: Settings = Depends(get_settings),
):
    """Price an ingestion before spending anything.

    Works with no API keys -- the whole point is that the cost is knowable
    before the decision to spend is made.
    """
    path = _resolve_path(body.path, s)
    async with tenant_tx(p.tenant_id, p.user_id) as conn:
        ing = Ingestor(
            conn=conn,
            bundle=request.app.state.bundle,
            resolver=request.app.state.resolver,
            tenant_id=p.tenant_id,
            user_id=p.user_id,
        )
        run_id, dv_id, chunks, est, _ = await ing.prepare(
            path, body.slug, body.title, body.version_label
        )
    return {
        "ingestion_run_id": str(run_id),
        "document_version_id": str(dv_id),
        **est.as_dict(),
    }


@app.post("/api/documents/ingest")
async def ingest_document(
    body: IngestRequest,
    request: Request,
    p: Principal = Depends(current_principal),
    s: Settings = Depends(get_settings),
):
    path = _resolve_path(body.path, s)
    async with tenant_tx(p.tenant_id, p.user_id) as conn:
        ing = Ingestor(
            conn=conn,
            bundle=request.app.state.bundle,
            resolver=request.app.state.resolver,
            tenant_id=p.tenant_id,
            user_id=p.user_id,
        )
        run_id, dv_id, chunks, est, raw = await ing.prepare(
            path, body.slug, body.title, body.version_label
        )

        cap = float(
            request.app.state.bundle.costs.get("budgets", {}).get(
                "per_ingestion_run_usd", 2.00
            )
        )
        auto = float(
            request.app.state.bundle.costs.get("budgets", {}).get(
                "estimate_auto_approve_under_usd", 0.25
            )
        )
        if float(est.total_cost) > cap:
            raise HTTPException(
                402,
                f"Estimated ${est.total_cost:.4f} exceeds the per-run cap of ${cap:.2f}.",
            )
        if float(est.total_cost) > auto and not body.approve:
            raise HTTPException(
                402,
                {
                    "message": f"Estimated ${est.total_cost:.4f}. Re-send with approve=true.",
                    "estimate": est.as_dict(),
                },
            )

        try:
            res = await ing.run(
                run_id, dv_id, chunks, raw, contextualise=body.contextualise
            )
        except MissingCredentialError as exc:
            await conn.execute(
                "UPDATE ingestion_runs SET status='failed', error=$2 WHERE id=$1",
                run_id,
                str(exc),
            )
            raise HTTPException(503, str(exc)) from exc

    return {
        "ingestion_run_id": str(res.ingestion_run_id),
        "document_version_id": str(res.document_version_id),
        "chunk_count": res.chunk_count,
        "embedded": res.embedded_count,
        "reused": res.reused_count,
        "estimated_cost_usd": float(est.total_cost),
        "actual_cost_usd": res.actual_cost,
    }


# ---------------------------------------------------------------------------
# Ask
# ---------------------------------------------------------------------------
@app.post("/api/ask")
async def ask(
    body: AskRequest,
    request: Request,
    p: Principal = Depends(current_principal),
):
    """Answer a question, streaming over SSE.

    SSE rather than WebSocket: the traffic is one-directional (the client sends
    one question and then only listens), SSE reconnects for free, and it is
    plain HTTP so it needs no separate upgrade path. A WebSocket would be
    strictly more machinery for a strictly smaller set of guarantees.

    Native ``EventSource`` cannot issue a POST, so the browser side uses fetch
    with a ReadableStream reader -- see ``web/src/lib/useAskStream.ts``.
    """
    return StreamingResponse(
        _ask_stream(body, request, p),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Without this, nginx and similar proxies buffer the whole response
            # and the stream arrives as one lump at the end.
            "X-Accel-Buffering": "no",
        },
    )


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


async def _ask_stream(body: AskRequest, request: Request, p: Principal):
    bundle = request.app.state.bundle
    resolver = request.app.state.resolver
    qhash = question_hash(body.question)

    try:
        async with tenant_tx(p.tenant_id, p.user_id) as conn:
            doc = await conn.fetchrow(
                """
                SELECT dv.id, d.title, dv.effective_from
                FROM document_versions dv JOIN documents d ON d.id = dv.document_id
                WHERE dv.status = 'ready'
                ORDER BY dv.created_at DESC LIMIT 1
                """
            )
            if doc is None:
                yield _sse("error", {"message": "No document has been ingested yet."})
                return

            outline = await conn.fetchval(
                """
                SELECT string_agg(DISTINCT section_title, E'\n- ')
                FROM chunks WHERE document_version_id = $1
                """,
                doc["id"],
            )
            doc_ctx = {
                "title": doc["title"],
                "outline": "- " + (outline or ""),
                "effective_from": doc["effective_from"],
            }

            run_id = await conn.fetchval(
                """
                INSERT INTO query_runs (tenant_id, user_id, conversation_id,
                                        config_bundle_id, question, question_hash)
                VALUES ($1,$2,$3,$4,$5,$6) RETURNING id
                """,
                str(p.tenant_id),
                str(p.user_id),
                str(body.conversation_id) if body.conversation_id else None,
                str(bundle.id),
                body.question,
                qhash,
            )

            yield _sse(
                "stage",
                {"stage": "started", "run_id": str(run_id),
                 "config_bundle": bundle.bundle_hash[:16]},
            )

            nodes = Nodes(
                conn=conn, bundle=bundle, resolver=resolver, doc_context=doc_ctx
            )
            state = new_state(
                query_run_id=run_id,
                tenant_id=p.tenant_id,
                user_id=p.user_id,
                config_bundle_id=bundle.id,
                question=body.question,
                question_hash=qhash,
                conversation_id=body.conversation_id,
                document_version_id=doc["id"],
            )

            # The graph is driven node by node rather than through
            # ``compiled.astream`` so that stage progress and generation tokens
            # interleave on one stream. A RAG answer takes seconds; showing
            # "routing -> retrieved 20 -> reranking" during that wait is the
            # difference between feeling fast and feeling broken.
            async for event in _drive(nodes, state, conn, p, bundle):
                yield event

    except ProviderError as exc:
        yield _sse("error", {"message": str(exc), "retryable": exc.retryable})
    except Exception as exc:  # noqa: BLE001
        yield _sse("error", {"message": f"{type(exc).__name__}: {exc}"})


async def _drive(nodes: Nodes, state, conn, p: Principal, bundle):
    """Run the pipeline, emitting stage events and streaming the answer."""
    import time

    t0 = time.perf_counter()

    state.update(await nodes.guard(state))
    if state.get("outcome"):
        async for e in _finish(state, conn, t0):
            yield e
        return

    state.update(await nodes.cache_lookup(state))
    if state.get("cache_hit", "none") != "none":
        yield _sse("stage", {"stage": "cache_hit", "kind": state["cache_hit"]})
        yield _sse("token", {"text": state.get("answer", "")})
        async for e in _finish(state, conn, t0):
            yield e
        return

    yield _sse("stage", {"stage": "routing"})
    state.update(await nodes.route(state))
    yield _sse(
        "stage",
        {"stage": "routed", "intent": state.get("intent"),
         "filter": state.get("route_filter")},
    )

    from app.graph.build import route_after_routing

    branch = route_after_routing(state)
    if branch == "refuse":
        state.update(await nodes.refuse(state))
        yield _sse("token", {"text": state["answer"]})
        async for e in _finish(state, conn, t0):
            yield e
        return
    if branch == "clarify":
        state.update(await nodes.clarify(state))
        yield _sse("token", {"text": state["answer"]})
        async for e in _finish(state, conn, t0):
            yield e
        return

    yield _sse("stage", {"stage": "translating"})
    state.update(await nodes.translate(state))
    yield _sse("stage", {"stage": "translated", "queries": state.get("queries", [])})

    yield _sse("stage", {"stage": "retrieving"})
    state.update(await nodes.retrieve(state))
    yield _sse("stage", {"stage": "retrieved", "count": len(state.get("candidates") or [])})

    if not state.get("candidates"):
        state.update(await nodes.refuse(state))
        yield _sse("token", {"text": state["answer"]})
        async for e in _finish(state, conn, t0):
            yield e
        return

    yield _sse("stage", {"stage": "reranking"})
    state.update(await nodes.rerank(state))
    yield _sse("stage", {"stage": "reranked", "kept": len(state.get("reranked") or [])})

    if not state.get("reranked"):
        state.update(await nodes.refuse(state))
        yield _sse("token", {"text": state["answer"]})
        async for e in _finish(state, conn, t0):
            yield e
        return

    # Citations go out BEFORE the tokens so the UI can render the source panel
    # while the answer is still streaming, and a [3] marker is already clickable
    # the moment it appears.
    yield _sse(
        "citations",
        [
            {
                "marker": i,
                "chunk_id": c["chunk_id"],
                "breadcrumb": c.get("breadcrumb", ""),
                "content": c.get("content", ""),
                "rerank_score": c.get("rerank_score"),
                "rerank_reason": c.get("rerank_reason"),
            }
            for i, c in enumerate(state["reranked"], start=1)
        ],
    )

    yield _sse("stage", {"stage": "generating"})
    spec, system, user = nodes.generation_prompt(state)

    from app.graph.instrument import step as step_ctx

    answer_parts: list[str] = []
    async with step_ctx(
        conn,
        node="generate",
        step_index=5,
        tenant_id=p.tenant_id,
        user_id=p.user_id,
        query_run_id=state["query_run_id"],
    ) as rec:
        rec.model_version_id = spec.model_version_id
        rec.prompt_version_id = spec.prompt_version_id
        rec.effort = spec.effort
        rec.rendered_hash = prompt_hash(system + user)

        async for kind, payload in spec.provider.stream(
            system,
            user,
            max_output_tokens=spec.max_output_tokens,
            effort=spec.effort,
            cache_system=bool(spec.prompt.get("cache_system_prefix")),
        ):
            if kind == "token":
                answer_parts.append(payload)
                yield _sse("token", {"text": payload})
            elif kind == "usage":
                rec.record_usage(payload)
        rec.output = {"chars": sum(len(p) for p in answer_parts)}

    state["answer"] = "".join(answer_parts)
    state["outcome"] = "answered"

    state.update(await nodes.verify(state))
    async for e in _finish(state, conn, t0):
        yield e


async def _finish(state, conn, t0):
    """Close the run, report cost, and populate the cache."""
    import time

    latency = int((time.perf_counter() - t0) * 1000)

    row = await conn.fetchrow(
        """
        UPDATE query_runs
           SET answer=$2, outcome=$3, intent=$4, route_filter=$5::jsonb,
               cache_hit=$6, degraded=$7, degraded_reason=$8,
               latency_ms=$9, finished_at=now()
         WHERE id=$1
        RETURNING total_cost_usd
        """,
        state["query_run_id"],
        state.get("answer"),
        state.get("outcome", "answered"),
        state.get("intent"),
        json.dumps(state.get("route_filter") or {}),
        state.get("cache_hit", "none"),
        state.get("degraded", False),
        state.get("degraded_reason"),
        latency,
    )
    cost = float(row["total_cost_usd"] or 0)

    breakdown = await conn.fetch(
        """
        SELECT node, model_id, input_tokens, output_tokens,
               cache_read_tokens, cost_usd
        FROM v_run_cost_breakdown
        WHERE query_run_id = $1
        ORDER BY step_index
        """,
        state["query_run_id"],
    )

    # Cache successful answers only. Caching a refusal would persist a failure
    # to retrieve, and the next identical question would never get a second
    # chance after the corpus or config improved.
    if state.get("outcome") == "answered" and state.get("cache_hit") == "none":
        await conn.execute(
            """
            INSERT INTO answer_cache (tenant_id, config_bundle_id, document_version_id,
                                      question_hash, question, question_embedding,
                                      answer, citations, outcome, source_cost_usd)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9,$10)
            ON CONFLICT (tenant_id, config_bundle_id, question_hash) DO NOTHING
            """,
            state["tenant_id"],
            state["config_bundle_id"],
            state.get("document_version_id"),
            state["question_hash"],
            state["question"],
            state.get("question_embedding"),
            state["answer"],
            json.dumps(state.get("citations") or [], default=str),
            state["outcome"],
            cost,
        )

    yield _sse(
        "cost",
        {
            "total_usd": cost,
            "total_millicents": round(cost * 100000, 2),
            "latency_ms": latency,
            "breakdown": [dict(b) for b in breakdown],
        },
    )
    yield _sse(
        "done",
        {
            "run_id": str(state["query_run_id"]),
            "outcome": state.get("outcome"),
            "degraded": state.get("degraded", False),
            "degraded_reason": state.get("degraded_reason"),
            "cache_hit": state.get("cache_hit", "none"),
        },
    )


# ---------------------------------------------------------------------------
# Provenance and cost
# ---------------------------------------------------------------------------
@app.get("/api/runs/{run_id}/provenance")
async def run_provenance(run_id: UUID, p: Principal = Depends(current_principal)):
    """Everything that produced one answer.

    This endpoint is the project's first requirement made visible: the exact
    prompt text, prompt version, model, effort and config file contents behind
    a given answer. It is a SELECT from a view -- the handler adds nothing.
    """
    async with tenant_tx(p.tenant_id, p.user_id) as conn:
        steps = await conn.fetch(
            "SELECT * FROM v_run_provenance WHERE query_run_id=$1 ORDER BY step_index",
            str(run_id),
        )
        files = await conn.fetch(
            "SELECT * FROM v_run_config_files WHERE query_run_id=$1 ORDER BY config_file",
            str(run_id),
        )
    if not steps:
        raise HTTPException(404, "run not found")
    return {
        "run_id": str(run_id),
        "config_bundle_id": str(steps[0]["config_bundle_id"]),
        "bundle_hash": steps[0]["bundle_hash"],
        "question": steps[0]["question"],
        "outcome": steps[0]["outcome"],
        "total_cost_usd": float(steps[0]["total_cost_usd"] or 0),
        "steps": [dict(s) for s in steps if s["node"]],
        "config_files": [dict(f) for f in files],
    }


@app.get("/api/costs/summary")
async def cost_summary(p: Principal = Depends(current_principal)):
    async with tenant_tx(p.tenant_id, p.user_id) as conn:
        per_query = await conn.fetch(
            "SELECT * FROM v_cost_per_query ORDER BY day DESC LIMIT 30"
        )
        by_node = await conn.fetch(
            "SELECT * FROM v_cost_by_node ORDER BY total_cost_usd DESC"
        )
        ingestion = await conn.fetch("SELECT * FROM v_ingestion_estimate_accuracy")
        budget = await conn.fetchrow("SELECT * FROM tenant_budgets LIMIT 1")
        today = await conn.fetchval(
            """
            SELECT COALESCE(sum(cost_usd),0) FROM cost_ledger
            WHERE is_actual AND created_at >= date_trunc('day', now())
            """
        )
    return {
        "spent_today_usd": float(today or 0),
        "budget": dict(budget) if budget else None,
        "per_query": [dict(r) for r in per_query],
        "by_node": [dict(r) for r in by_node],
        "ingestion": [dict(r) for r in ingestion],
    }


@app.get("/api/runs")
async def list_runs(limit: int = 50, p: Principal = Depends(current_principal)):
    async with tenant_tx(p.tenant_id, p.user_id) as conn:
        rows = await conn.fetch(
            """
            SELECT id, question, outcome, intent, cache_hit, degraded,
                   total_cost_usd, latency_ms, created_at
            FROM query_runs ORDER BY created_at DESC LIMIT $1
            """,
            limit,
        )
    return [dict(r) for r in rows]


def _resolve_path(raw: str, s: Settings) -> Path:
    """Resolve a document path, refusing to escape the repository.

    The API takes a path rather than an upload because the corpus lives in the
    repo for this deployment. Confining it to the repo root keeps that from
    being an arbitrary file read.
    """
    root = Path(__file__).resolve().parents[2]
    path = (root / raw).resolve() if not Path(raw).is_absolute() else Path(raw).resolve()
    if root not in path.parents and path != root:
        raise HTTPException(400, "path must be inside the repository")
    if not path.is_file():
        raise HTTPException(404, f"no such file: {raw}")
    return path
