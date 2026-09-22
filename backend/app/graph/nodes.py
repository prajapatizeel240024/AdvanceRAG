"""Graph nodes.

Each node does one thing and writes one run_step. The retrieval nodes are
deliberately thin: they marshal parameters, call a SQL function, and hand back
rows. All the ranking mathematics lives in ``migrations/004``.

The node ordering encodes the cost strategy. Cheap filters run first -- cache
lookup, then routing -- so that the two most expensive nodes (rerank and
generation, both on Opus 5) are only reached by questions that have earned
them. A chitchat or out-of-scope question never touches either.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from app.graph.instrument import step
from app.graph.state import GraphState, RetrievedChunk
from app.providers.base import ProviderError

CITATION_RE = re.compile(r"\[(\d+)\]")


def normalise_question(q: str) -> str:
    """Normalise for cache keying.

    Lowercased, punctuation stripped, whitespace collapsed -- so "What's the
    per diem in Tokyo?" and "whats the per diem in tokyo" share a cache entry.
    """
    return re.sub(r"[^a-z0-9 ]+", "", q.lower()).strip()


def question_hash(q: str) -> str:
    return hashlib.sha256(normalise_question(q).encode("utf-8")).hexdigest()


def render(template: str, **kwargs: Any) -> str:
    """Render a prompt template.

    ``str.format`` is deliberately avoided: policy prompts contain literal
    braces in JSON examples and table pipes, and format() raises or mangles
    them. Explicit placeholder replacement is predictable.
    """
    out = template
    for key, value in kwargs.items():
        out = out.replace("{" + key + "}", str(value))
    return out


def prompt_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Nodes:
    """Node implementations bound to a request's dependencies.

    Dependencies are injected rather than imported so tests can run the whole
    graph against a stub provider with no network and no API key.
    """

    def __init__(self, *, conn, bundle, resolver, doc_context) -> None:
        self.conn = conn
        self.bundle = bundle
        # Resolves a role name to (provider, model_version_id, prompt_version_id,
        # prompt spec) from the registered config.
        self.resolve = resolver
        self.doc = doc_context

    # -- 1. guard ----------------------------------------------------------

    async def guard(self, state: GraphState) -> dict[str, Any]:
        q = (state.get("question") or "").strip()
        if not q:
            return {"outcome": "refused_no_evidence", "answer": "Please ask a question."}
        if len(q) > 2000:
            return {
                "outcome": "refused_no_evidence",
                "answer": "That question is too long. Please shorten it to under 2000 characters.",
            }
        return {"question": q, "question_hash": question_hash(q)}

    # -- 2. cache lookup ---------------------------------------------------

    async def cache_lookup(self, state: GraphState) -> dict[str, Any]:
        """Probe the answer cache.

        Scoped to the config bundle AND the document version, so neither a
        prompt change nor a policy reissue can serve a stale answer. The
        semantic threshold is high on purpose -- see fn_cache_lookup.

        The question is embedded HERE rather than in ``retrieve``, for a reason
        that is easy to get wrong: the semantic tier needs a query vector, and a
        cache that runs before retrieval has none. Passing NULL makes
        fn_cache_lookup's semantic branch unreachable, so the semantic cache
        silently never fires and only exact-hash hits ever land -- a dead
        feature that still looks implemented.

        This costs nothing extra: the vector is carried forward in state and
        ``retrieve`` reuses it for the original question instead of re-embedding
        it, so the run makes exactly as many embedding calls as before.
        """
        cfg = self.bundle.retrieval.get("cache", {})
        if not cfg.get("enabled", True):
            return {"cache_hit": "none"}

        # Best-effort: with no credentials, or if embedding fails, fall back to
        # the exact-hash tier rather than failing the whole question.
        embedding = None
        if cfg.get("semantic_enabled", True):
            try:
                embed_spec = self.resolve("embedding")
                emb = await embed_spec.provider.embed(
                    [state["question"]], is_query=True
                )
                embedding = emb.vectors[0] if emb.vectors else None
            except Exception:  # noqa: BLE001 - degrade, never fail the question
                embedding = None

        row = await self.conn.fetchrow(
            """
            SELECT * FROM fn_cache_lookup($1, $2, $3, $4, $5) LIMIT 1
            """,
            state["config_bundle_id"],
            state["question_hash"],
            embedding,
            state.get("document_version_id"),
            float(cfg.get("semantic_threshold", 0.97)),
        )
        if not row:
            return {"cache_hit": "none", "question_embedding": embedding}

        await self.conn.execute(
            "UPDATE answer_cache SET hit_count = hit_count + 1 WHERE id = $1",
            row["cache_id"],
        )
        return {
            "cache_hit": row["hit_kind"],
            "question_embedding": embedding,
            "answer": row["answer"],
            "citations": json.loads(row["citations"]) if row["citations"] else [],
            "outcome": row["outcome"],
        }

    # -- 3. routing --------------------------------------------------------

    async def route(self, state: GraphState) -> dict[str, Any]:
        """Classify intent and emit a metadata filter.

        The cheapest node and the highest-leverage: an out_of_scope or chitchat
        verdict short-circuits retrieval, reranking and generation entirely.
        """
        spec = self.resolve("routing")
        async with step(
            self.conn,
            node="route",
            step_index=1,
            tenant_id=state["tenant_id"],
            user_id=state["user_id"],
            query_run_id=state["query_run_id"],
        ) as rec:
            rec.model_version_id = spec.model_version_id
            rec.prompt_version_id = spec.prompt_version_id
            rec.effort = spec.effort

            system = render(
                spec.prompt["system"],
                document_title=self.doc["title"],
                section_outline=self.doc["outline"],
            )
            user = render(spec.prompt["user"], question=state["question"])
            rec.rendered_hash = prompt_hash(system + user)

            res = await spec.provider.complete_structured(
                system,
                user,
                spec.prompt["output_schema"],
                max_output_tokens=spec.max_output_tokens,
                effort=spec.effort,
            )
            rec.record_usage(res.usage)
            parsed = res.parsed or {}
            rec.output = parsed

        topics = parsed.get("topics") or []
        # A single confident topic narrows retrieval usefully. Two or more is
        # ambiguity, and filtering on an ambiguous guess starves retrieval far
        # more often than it helps -- so we only push the filter down when the
        # router committed to one topic.
        route_filter = {"topic": topics[0]} if len(topics) == 1 else {}

        return {
            "intent": parsed.get("intent", "policy_lookup"),
            "needs_retrieval": parsed.get("needs_retrieval", True),
            "route_filter": route_filter,
            "missing_facts": parsed.get("missing_facts") or [],
            "route_reasoning": parsed.get("reasoning", ""),
        }

    # -- 4. query translation ----------------------------------------------

    async def translate(self, state: GraphState) -> dict[str, Any]:
        """Rewrite the question into the policy's vocabulary.

        Expansion breadth is driven by intent: a simple lookup gets one
        rewrite, a multi-hop question gets three. That is a cost decision, so
        it lives in config rather than in code.
        """
        cfg = self.bundle.retrieval.get("translation", {})
        by_intent = cfg.get("max_queries_by_intent", {})
        max_queries = int(by_intent.get(state.get("intent", ""), cfg.get("max_queries", 2)))

        spec = self.resolve("query_translation")
        async with step(
            self.conn,
            node="translate",
            step_index=2,
            tenant_id=state["tenant_id"],
            user_id=state["user_id"],
            query_run_id=state["query_run_id"],
        ) as rec:
            rec.model_version_id = spec.model_version_id
            rec.prompt_version_id = spec.prompt_version_id
            rec.effort = spec.effort

            system = render(
                spec.prompt["system"],
                document_title=self.doc["title"],
                section_outline=self.doc["outline"],
                max_queries=max_queries,
            )
            user = render(
                spec.prompt["user"],
                question=state["question"],
                intent=state.get("intent", "policy_lookup"),
            )
            rec.rendered_hash = prompt_hash(system + user)

            res = await spec.provider.complete_structured(
                system,
                user,
                spec.prompt["output_schema"],
                max_output_tokens=spec.max_output_tokens,
                effort=spec.effort,
            )
            rec.record_usage(res.usage)
            parsed = res.parsed or {}
            rec.output = parsed

        # The original question always stays in the set. A rewrite can drift,
        # and losing the user's actual words is a silent recall failure.
        queries = [state["question"]]
        queries += [q for q in (parsed.get("queries") or []) if q not in queries]
        if parsed.get("broader"):
            queries.append(parsed["broader"])
        queries += [q for q in (parsed.get("sub_questions") or []) if q not in queries]

        return {
            "queries": queries[: max_queries + 2],
            "broader_query": parsed.get("broader"),
            "sub_questions": parsed.get("sub_questions") or [],
            "key_terms": parsed.get("key_terms") or [],
        }

    # -- 5. retrieval ------------------------------------------------------

    async def retrieve(self, state: GraphState) -> dict[str, Any]:
        """Hybrid retrieval, fused and diversified entirely in SQL.

        This node embeds the queries, calls ``fn_hybrid_search`` once per
        query, unions the results, then calls ``fn_mmr_diversify``. It performs
        no ranking arithmetic of its own.
        """
        r = self.bundle.retrieval
        fusion = r.get("fusion", {})
        mmr = r.get("mmr", {})
        thresholds = r.get("thresholds", {})

        embed_spec = self.resolve("embedding")

        async with step(
            self.conn,
            node="retrieve",
            step_index=3,
            tenant_id=state["tenant_id"],
            user_id=state["user_id"],
            query_run_id=state["query_run_id"],
        ) as rec:
            rec.model_version_id = embed_spec.model_version_id
            rec.operation = "embedding"

            queries = state.get("queries") or [state["question"]]

            # queries[0] is always the user's original question (translate()
            # guarantees it), and cache_lookup already embedded exactly that.
            # Reusing the vector keeps the embedding-call count identical to
            # before the cache started embedding, so the semantic tier is free.
            cached_vec = state.get("question_embedding")
            if cached_vec is not None and queries and queries[0] == state["question"]:
                to_embed = queries[1:]
                emb = await embed_spec.provider.embed(to_embed, is_query=True)
                vectors = [cached_vec, *emb.vectors]
            else:
                emb = await embed_spec.provider.embed(queries, is_query=True)
                vectors = emb.vectors
            rec.record_usage(emb.usage)

            # Query-time index breadth. SET LOCAL keeps it scoped to this
            # transaction so a pooled connection cannot leak it into an
            # unrelated request.
            await self.conn.execute(
                f"SET LOCAL hnsw.ef_search = {int(r['vector']['index'].get('ef_search', 100))}"
            )

            seen: dict[str, RetrievedChunk] = {}
            for query_text, vec in zip(queries, vectors):
                rows = await self.conn.fetch(
                    """
                    SELECT * FROM fn_hybrid_search(
                        $1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9, $10, $11)
                    """,
                    embed_spec.model_version_id,
                    query_text,
                    vec,
                    int(fusion.get("candidates_per_retriever", 40)),
                    int(mmr.get("input_candidates", 30)),
                    json.dumps(state.get("route_filter") or {}),
                    state.get("document_version_id"),
                    int(fusion.get("k", 60)),
                    float(fusion.get("weights", {}).get("vector", 1.0)),
                    float(fusion.get("weights", {}).get("lexical", 0.8)),
                    float(thresholds.get("min_vector_similarity", 0.0)),
                )
                for row in rows:
                    cid = str(row["chunk_id"])
                    # Fan-out over several rewrites finds the same chunk more
                    # than once. Keep the best fused score rather than summing,
                    # which would reward chunks that happen to match many
                    # rewrites over chunks that match one rewrite strongly.
                    prev = seen.get(cid)
                    if prev is None or row["fused_score"] > prev["fused_score"]:
                        seen[cid] = RetrievedChunk(
                            chunk_id=cid,
                            fused_score=float(row["fused_score"]),
                            vector_rank=row["vector_rank"],
                            lexical_rank=row["lexical_rank"],
                            vector_similarity=(
                                float(row["vector_similarity"])
                                if row["vector_similarity"] is not None
                                else None
                            ),
                        )

            candidates = sorted(
                seen.values(), key=lambda c: c["fused_score"], reverse=True
            )

            if mmr.get("enabled") and candidates:
                mmr_rows = await self.conn.fetch(
                    "SELECT * FROM fn_mmr_diversify($1, $2, $3, $4, $5, $6)",
                    embed_spec.model_version_id,
                    [c["chunk_id"] for c in candidates],
                    [c["fused_score"] for c in candidates],
                    vectors[0],
                    float(mmr.get("lambda", 0.7)),
                    int(mmr.get("output_candidates", 20)),
                )
                order = {str(m["chunk_id"]): m["rank_position"] for m in mmr_rows}
                candidates = [c for c in candidates if c["chunk_id"] in order]
                for c in candidates:
                    c["mmr_position"] = order[c["chunk_id"]]
                candidates.sort(key=lambda c: c["mmr_position"])

            # Attach text for the reranker.
            if candidates:
                texts = await self.conn.fetch(
                    "SELECT * FROM fn_expand_context($1, $2)",
                    [c["chunk_id"] for c in candidates],
                    int(r.get("expansion", {}).get("max_expanded_tokens", 1200)),
                )
                by_id = {str(t["chunk_id"]): t for t in texts}
                for c in candidates:
                    t = by_id.get(c["chunk_id"])
                    if t:
                        c["content"] = t["content"]
                        c["expanded"] = t["expanded"]
                        c["breadcrumb"] = t["breadcrumb"]
                        c["section_path"] = list(t["section_path"] or [])

            rec.output = {"candidate_count": len(candidates)}

        return {"candidates": candidates}

    # -- 6. rerank ---------------------------------------------------------

    async def rerank(self, state: GraphState) -> dict[str, Any]:
        """Listwise reranking on Opus 5.

        One call ranks every candidate together, because relevance among policy
        passages is comparative -- deciding whether the base rule or its
        exception governs a case is impossible with either in isolation.
        """
        r = self.bundle.retrieval
        cfg = r.get("rerank", {})
        thresholds = r.get("thresholds", {})
        candidates = state.get("candidates") or []

        if not cfg.get("enabled", True) or not candidates:
            return {"reranked": candidates[: int(cfg.get("output_candidates", 6))]}

        spec = self.resolve("rerank")
        top = candidates[: int(cfg.get("input_candidates", 20))]

        async with step(
            self.conn,
            node="rerank",
            step_index=4,
            tenant_id=state["tenant_id"],
            user_id=state["user_id"],
            query_run_id=state["query_run_id"],
        ) as rec:
            rec.model_version_id = spec.model_version_id
            rec.prompt_version_id = spec.prompt_version_id
            rec.effort = spec.effort

            listing = "\n\n".join(
                f"[{i}] {c.get('breadcrumb','')}\n{c.get('expanded') or c.get('content','')}"
                for i, c in enumerate(top, start=1)
            )
            # The system block is byte-stable across queries so the prefix
            # caches; only the user block varies. Interpolating anything
            # per-query into system would drop the hit rate to zero.
            system = spec.prompt["system"]
            user = render(
                spec.prompt["user"], question=state["question"], candidates=listing
            )
            rec.rendered_hash = prompt_hash(system + user)

            try:
                res = await spec.provider.complete_structured(
                    system,
                    user,
                    spec.prompt["output_schema"],
                    max_output_tokens=spec.max_output_tokens,
                    effort=spec.effort,
                    cache_system=bool(spec.prompt.get("cache_system_prefix")),
                )
            except ProviderError as exc:
                # Degrade rather than fail: fusion order is a worse ranking but
                # a usable one, and a marked-degraded answer beats an error.
                rec.status = "error"
                rec.error = str(exc)
                return {
                    "reranked": top[: int(cfg.get("output_candidates", 6))],
                    "degraded": True,
                    "degraded_reason": f"rerank unavailable ({exc}); using fusion order",
                }

            rec.record_usage(res.usage)
            rankings = (res.parsed or {}).get("rankings") or []
            rec.output = {"ranked": len(rankings)}

        scores = {
            int(rk["id"]): (float(rk.get("relevance", 0)), rk.get("reason", ""))
            for rk in rankings
            if isinstance(rk.get("id"), int) and 1 <= int(rk["id"]) <= len(top)
        }
        for i, c in enumerate(top, start=1):
            score, reason = scores.get(i, (0.0, ""))
            c["rerank_score"] = score
            c["rerank_reason"] = reason

        min_rel = float(thresholds.get("min_rerank_relevance", 0.55))
        kept = [c for c in top if (c.get("rerank_score") or 0) >= min_rel]
        kept.sort(key=lambda c: c.get("rerank_score") or 0, reverse=True)
        kept = kept[: int(cfg.get("output_candidates", 6))]
        for pos, c in enumerate(kept, start=1):
            c["rerank_position"] = pos

        return {"reranked": kept}

    # -- 7. generation -----------------------------------------------------

    def generation_prompt(self, state: GraphState) -> tuple[Any, str, str]:
        spec = self.resolve("generation")
        blocks = state.get("reranked") or []
        context = "\n\n".join(
            f"[{i}] {c.get('breadcrumb','')}\n{c.get('expanded') or c.get('content','')}"
            for i, c in enumerate(blocks, start=1)
        )
        system = spec.prompt["system"]
        user = render(
            spec.prompt["user"],
            question=state["question"],
            context=context,
            document_title=self.doc["title"],
            effective_date=self.doc.get("effective_from", "current"),
        )
        return spec, system, user

    def extract_citations(
        self, answer: str, blocks: list[RetrievedChunk]
    ) -> tuple[list[dict[str, Any]], float, list[int]]:
        """Map [n] markers back to chunk ids.

        Also returns citation coverage and any invented marker numbers.
        Checking this structurally -- a regex over markers -- is free and
        deterministic. An LLM groundedness judge would roughly double the
        per-query cost to answer a question that simple arithmetic already
        answers.
        """
        used = [int(m) for m in CITATION_RE.findall(answer)]
        valid = [n for n in used if 1 <= n <= len(blocks)]
        invalid = sorted({n for n in used if n not in valid})

        citations: list[dict[str, Any]] = []
        for n in sorted(set(valid)):
            c = blocks[n - 1]
            citations.append(
                {
                    "marker": n,
                    "chunk_id": c["chunk_id"],
                    "breadcrumb": c.get("breadcrumb", ""),
                    "content": c.get("content", ""),
                    "rerank_score": c.get("rerank_score"),
                }
            )

        sentences = [s for s in re.split(r"(?<=[.!?])\s+", answer.strip()) if s]
        cited = [s for s in sentences if CITATION_RE.search(s)]
        coverage = len(cited) / len(sentences) if sentences else 0.0
        return citations, coverage, invalid

    async def generate(self, state: GraphState) -> dict[str, Any]:
        """Produce the grounded, cited answer.

        The non-streaming path. The API's streaming endpoint drives the same
        prompt through ``provider.stream`` directly so tokens reach the browser
        as they are produced; this exists for the eval harness and for tests,
        where a single complete answer is what is wanted.
        """
        spec, system, user = self.generation_prompt(state)

        async with step(
            self.conn,
            node="generate",
            step_index=5,
            tenant_id=state["tenant_id"],
            user_id=state["user_id"],
            query_run_id=state["query_run_id"],
        ) as rec:
            rec.model_version_id = spec.model_version_id
            rec.prompt_version_id = spec.prompt_version_id
            rec.effort = spec.effort
            rec.rendered_hash = prompt_hash(system + user)

            res = await spec.provider.complete(
                system,
                user,
                max_output_tokens=spec.max_output_tokens,
                effort=spec.effort,
                cache_system=bool(spec.prompt.get("cache_system_prefix")),
            )
            rec.record_usage(res.usage)
            rec.output = {"chars": len(res.text)}

        return {"answer": res.text, "outcome": "answered"}

    # -- 8. verify ---------------------------------------------------------

    async def verify(self, state: GraphState) -> dict[str, Any]:
        """Structural groundedness check, and persist what was retrieved.

        Deliberately not an LLM call. The failure this catches -- a citation
        marker pointing at a chunk that was never retrieved -- is exactly
        detectable by set membership, and the cheap check is also the reliable
        one. Low citation coverage is reported rather than corrected: rewriting
        the answer would cost another generation, and a visibly under-cited
        answer is more honest than a silently patched one.
        """
        blocks = state.get("reranked") or []
        answer = state.get("answer") or ""
        citations, coverage, invalid = self.extract_citations(answer, blocks)

        guards = self.bundle.retrieval.get("guards", {})
        min_cov = float(guards.get("min_citation_coverage", 0.6))

        degraded = state.get("degraded", False)
        reason = state.get("degraded_reason")

        if invalid and guards.get("reject_uncited_chunk_ids", True):
            degraded = True
            reason = (
                f"answer cited excerpt numbers that were not retrieved: {invalid}"
            )
        elif coverage < min_cov and guards.get("require_citations", True):
            degraded = True
            reason = f"citation coverage {coverage:.0%} below the {min_cov:.0%} floor"

        # Persist which chunks were retrieved, ranked and actually cited. This
        # is what makes retrieval quality measurable after the fact instead of
        # anecdotal.
        cited_ids = {c["chunk_id"] for c in citations}
        for c in state.get("candidates") or []:
            await self.conn.execute(
                """
                INSERT INTO retrieval_results (
                    tenant_id, query_run_id, chunk_id, vector_rank, lexical_rank,
                    fused_score, mmr_position, rerank_score, rerank_position,
                    used_in_answer, cited)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                ON CONFLICT (query_run_id, chunk_id) DO NOTHING
                """,
                state["tenant_id"],
                state["query_run_id"],
                c["chunk_id"],
                c.get("vector_rank"),
                c.get("lexical_rank"),
                c.get("fused_score"),
                c.get("mmr_position"),
                c.get("rerank_score"),
                c.get("rerank_position"),
                any(b["chunk_id"] == c["chunk_id"] for b in blocks),
                c["chunk_id"] in cited_ids,
            )

        return {
            "citations": citations,
            "degraded": degraded,
            "degraded_reason": reason,
        }

    # -- 9. refuse ---------------------------------------------------------

    async def refuse(self, state: GraphState) -> dict[str, Any]:
        """The honest "not covered" path.

        Reached when routing says chitchat, or when retrieval and reranking
        found nothing above threshold. Costs nothing: no model call is made.
        Deciding this structurally, rather than asking the model whether it
        knows, is what stops the system inventing a per diem.
        """
        intent = state.get("intent", "")

        if intent == "chitchat":
            return {
                "outcome": "refused_out_of_scope",
                "answer": (
                    "I answer questions about the Northwind Global travel policy — "
                    "booking, cabin class, per diems, hotel caps, expenses and approvals. "
                    "What would you like to know?"
                ),
                "citations": [],
            }

        # Retrieval ran but nothing cleared the bar. Say so plainly rather than
        # reasoning over weak evidence.
        return {
            "outcome": "refused_no_evidence",
            "answer": (
                "The travel policy does not appear to address this. I could not find "
                "any passage that answers it with enough confidence to quote.\n\n"
                "If this concerns relocation, visas and immigration, client "
                "entertainment or vehicle insurance, those are governed by separate "
                "policies that I do not have access to."
            ),
            "citations": [],
        }

    # -- 10. clarify -------------------------------------------------------

    async def clarify(self, state: GraphState) -> dict[str, Any]:
        """Ask for the missing fact instead of guessing.

        Several rules turn on facts users routinely omit: cabin class needs
        both grade band and flight duration, per diems need the city. Picking a
        likely case and answering it confidently is the failure mode that makes
        a policy assistant untrustworthy, so the graph has a dedicated exit for
        it. No model call.
        """
        missing = state.get("missing_facts") or []
        bullets = "\n".join(f"- {m}" for m in missing)
        return {
            "outcome": "needs_clarification",
            "answer": (
                "I need a bit more detail before I can answer that accurately, "
                "because the policy's answer depends on it:\n\n"
                f"{bullets}\n\n"
                "Tell me those and I'll give you the exact entitlement."
            ),
            "citations": [],
        }
