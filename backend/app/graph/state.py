"""Graph state.

One object threaded through every node. Three groups of fields:

  * the question and its derived forms,
  * retrieval results as they are progressively narrowed,
  * bookkeeping -- run id, config bundle, accumulated cost -- which exists
    because every node must be able to write its own provenance and cost row
    without reaching outside the graph for context.

``total_cost`` is accumulated here for the live UI meter, but the database is
authoritative: the ledger trigger computes each row's cost and rolls it up onto
``query_runs``. This field is a mirror for streaming, never the source of truth.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict
from uuid import UUID


class RetrievedChunk(TypedDict, total=False):
    chunk_id: str
    content: str
    expanded: str
    breadcrumb: str
    section_path: list[str]
    fused_score: float
    vector_rank: int | None
    lexical_rank: int | None
    vector_similarity: float | None
    mmr_position: int | None
    rerank_score: float | None
    rerank_position: int | None
    rerank_reason: str | None


class GraphState(TypedDict, total=False):
    # ---- identity and provenance -----------------------------------------
    query_run_id: str
    tenant_id: str
    user_id: str
    conversation_id: str | None
    config_bundle_id: str
    document_version_id: str | None

    # ---- input ------------------------------------------------------------
    question: str
    question_hash: str
    question_embedding: list[float] | None

    # ---- routing ----------------------------------------------------------
    intent: str
    needs_retrieval: bool
    route_filter: dict[str, Any]
    missing_facts: list[str]
    route_reasoning: str

    # ---- translation ------------------------------------------------------
    queries: list[str]
    broader_query: str | None
    sub_questions: list[str]
    key_terms: list[str]

    # ---- retrieval --------------------------------------------------------
    candidates: list[RetrievedChunk]
    reranked: list[RetrievedChunk]
    context_blocks: list[RetrievedChunk]

    # ---- output -----------------------------------------------------------
    answer: str
    citations: list[dict[str, Any]]
    outcome: str  # answered | refused_out_of_scope | refused_no_evidence | needs_clarification
    cache_hit: str  # none | exact | semantic

    # ---- bookkeeping ------------------------------------------------------
    # Nodes append; the reducer concatenates. Without operator.add, parallel
    # or retried nodes would overwrite one another's step records and the
    # provenance trail would have holes in it.
    steps: Annotated[list[dict[str, Any]], operator.add]
    total_cost: float
    degraded: bool
    degraded_reason: str | None
    errors: Annotated[list[str], operator.add]


def new_state(
    *,
    query_run_id: UUID | str,
    tenant_id: UUID | str,
    user_id: UUID | str,
    config_bundle_id: UUID | str,
    question: str,
    question_hash: str,
    conversation_id: UUID | str | None = None,
    document_version_id: UUID | str | None = None,
) -> GraphState:
    return GraphState(
        query_run_id=str(query_run_id),
        tenant_id=str(tenant_id),
        user_id=str(user_id),
        conversation_id=str(conversation_id) if conversation_id else None,
        config_bundle_id=str(config_bundle_id),
        document_version_id=str(document_version_id) if document_version_id else None,
        question=question,
        question_hash=question_hash,
        queries=[],
        sub_questions=[],
        key_terms=[],
        candidates=[],
        reranked=[],
        context_blocks=[],
        citations=[],
        steps=[],
        errors=[],
        total_cost=0.0,
        cache_hit="none",
        degraded=False,
        route_filter={},
        missing_facts=[],
    )
