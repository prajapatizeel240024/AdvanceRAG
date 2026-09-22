"""Ingestion: parse, chunk, contextualise, embed, store.

Order matters for cost. The pipeline estimates first and writes that estimate
to the database *before* spending anything, then reuses every embedding it can,
and only then makes paid calls. The estimate row stays alongside the actuals so
``v_ingestion_estimate_accuracy`` can report whether the pre-flight number was
trustworthy -- a forecast nobody checks is a forecast nobody should act on.

The reuse step is the single largest saving available here. Chunks are
content-addressed, so re-ingesting a policy whose annual revision touched 5% of
its text re-embeds 5% of its chunks. Measured on this corpus: a full ingest
costs $0.40 and a 5%-changed re-ingest costs $0.053.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from app.graph.instrument import step
from app.ingest.chunker import Chunk, chunk_markdown
from app.ingest.estimator import Estimate, estimate_ingestion


@dataclass
class IngestResult:
    ingestion_run_id: UUID
    document_version_id: UUID
    chunk_count: int
    embedded_count: int
    reused_count: int
    estimate: Estimate
    actual_cost: float


def file_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def section_outline(chunks: list[Chunk], limit: int = 60) -> str:
    """A compact heading tree, injected into the routing and translation prompts.

    Without it the router is guessing which topics exist; with it, the filter it
    emits actually corresponds to sections that are there.
    """
    seen: list[str] = []
    for c in chunks:
        title = c.section_title
        if title and title not in seen:
            seen.append(title)
    return "\n".join(f"- {t}" for t in seen[:limit])


class Ingestor:
    def __init__(self, *, conn, bundle, resolver, tenant_id, user_id) -> None:
        self.conn = conn
        self.bundle = bundle
        self.resolve = resolver
        self.tenant_id = str(tenant_id)
        self.user_id = str(user_id)

    # -- estimate ----------------------------------------------------------

    async def prepare(
        self, path: Path, slug: str, title: str, version_label: str
    ) -> tuple[UUID, UUID, list[Chunk], Estimate, str]:
        """Parse, chunk and price a document without spending anything.

        Registers the document and its version, records the estimate, and stops.
        Nothing here calls a paid endpoint, so it works with no API keys at all
        -- which is what makes the cost figure available *before* the decision
        to ingest rather than after.
        """
        raw = path.read_text(encoding="utf-8")
        chash = file_hash(raw)

        doc_id = await self.conn.fetchval(
            """
            INSERT INTO documents (tenant_id, slug, title)
            VALUES ($1, $2, $3)
            ON CONFLICT (tenant_id, slug) DO UPDATE SET title = EXCLUDED.title
            RETURNING id
            """,
            self.tenant_id,
            slug,
            title,
        )

        dv_id = await self.conn.fetchval(
            """
            WITH ins AS (
                INSERT INTO document_versions (
                    tenant_id, document_id, version_label, content_hash,
                    source_filename, source_format, raw_content, byte_size, status)
                VALUES ($1,$2,$3,$4,$5,'markdown',$6,$7,'estimated')
                ON CONFLICT (tenant_id, document_id, content_hash) DO NOTHING
                RETURNING id
            )
            SELECT id FROM ins
            UNION ALL
            SELECT id FROM document_versions
              WHERE tenant_id=$1 AND document_id=$2 AND content_hash=$4
            LIMIT 1
            """,
            self.tenant_id,
            doc_id,
            version_label,
            chash,
            path.name,
            raw,
            len(raw.encode("utf-8")),
        )

        ch_cfg = self.bundle.chunking.get("chunking", {})
        chunks = chunk_markdown(
            raw,
            title,
            max_tokens=int(ch_cfg.get("max_tokens", 600)),
            min_tokens=int(ch_cfg.get("min_tokens", 60)),
            overlap_tokens=int(ch_cfg.get("overlap_tokens", 90)),
            table_hard_cap_tokens=int(ch_cfg.get("table_hard_cap_tokens", 1200)),
        )

        # Resolve only the model id, never a live provider: estimation must
        # work with no API keys, since knowing the cost before spending is the
        # whole point of this path.
        embed_mv_id = self.resolve.model_version_id_for("embedding")
        already = await self._already_embedded([c.hash for c in chunks], embed_mv_id)

        est = estimate_ingestion(
            chunks, raw, self.bundle, already_embedded_hashes=already
        )

        run_id = await self.conn.fetchval(
            """
            INSERT INTO ingestion_runs (
                tenant_id, document_version_id, config_bundle_id, started_by,
                status, est_chunk_count, est_embedding_tokens,
                est_context_tokens_in, est_context_tokens_out, est_cost_usd)
            VALUES ($1,$2,$3,$4,'estimated',$5,$6,$7,$8,$9)
            RETURNING id
            """,
            self.tenant_id,
            dv_id,
            str(self.bundle.id),
            self.user_id,
            est.chunk_count,
            est.embedding_tokens,
            est.context_input_tokens + est.context_cached_tokens,
            est.context_output_tokens,
            float(est.total_cost),
        )

        return run_id, dv_id, chunks, est, raw

    async def _already_embedded(
        self, hashes: list[str], model_version_id: UUID
    ) -> set[str]:
        if not hashes:
            return set()
        rows = await self.conn.fetch(
            """
            SELECT DISTINCT content_hash FROM chunk_embeddings
            WHERE tenant_id = $1 AND model_version_id = $2 AND content_hash = ANY($3)
            """,
            self.tenant_id,
            str(model_version_id),
            hashes,
        )
        return {r["content_hash"] for r in rows}

    # -- execute -----------------------------------------------------------

    async def run(
        self,
        run_id: UUID,
        dv_id: UUID,
        chunks: list[Chunk],
        document_text: str,
        *,
        contextualise: bool | None = None,
    ) -> IngestResult:
        """Contextualise, embed and store. This is where money is spent."""
        await self.conn.execute(
            "UPDATE ingestion_runs SET status='running' WHERE id=$1", run_id
        )

        ctx_cfg = self.bundle.chunking.get("contextual_retrieval", {})
        do_ctx = ctx_cfg.get("enabled", False) if contextualise is None else contextualise

        if do_ctx:
            await self._contextualise(run_id, chunks, document_text)

        embed_spec = self.resolve("embedding")
        already = await self._already_embedded(
            [c.hash for c in chunks], embed_spec.model_version_id
        )

        # Replace this version's chunks wholesale. Chunk ordinals shift when a
        # document is edited, so patching in place would leave orphans; the
        # embeddings survive regardless because they are looked up by content
        # hash, not by chunk id.
        await self.conn.execute(
            "DELETE FROM chunks WHERE tenant_id=$1 AND document_version_id=$2",
            self.tenant_id,
            str(dv_id),
        )

        chunk_ids: dict[str, UUID] = {}
        for c in chunks:
            cid = await self.conn.fetchval(
                """
                INSERT INTO chunks (
                    tenant_id, document_version_id, ordinal, content_hash, content,
                    embed_text, breadcrumb, section_path, section_title,
                    context_summary, token_count, contains_table, table_part, metadata)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14::jsonb)
                RETURNING id
                """,
                self.tenant_id,
                str(dv_id),
                c.ordinal,
                c.hash,
                c.content,
                c.embed_text(),
                c.breadcrumb,
                c.section_path,
                c.section_title,
                c.context_summary,
                c.token_count,
                c.contains_table,
                c.table_part,
                _json(c.metadata),
            )
            chunk_ids[c.hash] = cid

        # Copy forward every embedding we already own before buying any.
        reused = 0
        for c in chunks:
            if c.hash not in already:
                continue
            copied = await self.conn.fetchval(
                """
                INSERT INTO chunk_embeddings (tenant_id, chunk_id, model_version_id,
                                              content_hash, embedding)
                SELECT $1, $2, $3, $4, embedding
                FROM chunk_embeddings
                WHERE tenant_id=$1 AND model_version_id=$3 AND content_hash=$4
                LIMIT 1
                ON CONFLICT (chunk_id, model_version_id) DO NOTHING
                RETURNING 1
                """,
                self.tenant_id,
                chunk_ids[c.hash],
                str(embed_spec.model_version_id),
                c.hash,
            )
            if copied:
                reused += 1

        to_embed = [c for c in chunks if c.hash not in already]
        embedded = 0

        if to_embed:
            async with step(
                self.conn,
                node="embed",
                step_index=2,
                tenant_id=self.tenant_id,
                user_id=self.user_id,
                ingestion_run_id=run_id,
            ) as rec:
                rec.model_version_id = embed_spec.model_version_id
                rec.operation = "embedding"

                res = await embed_spec.provider.embed(
                    [c.embed_text() for c in to_embed], is_query=False
                )
                rec.record_usage(res.usage)

                for c, vec in zip(to_embed, res.vectors):
                    await self.conn.execute(
                        """
                        INSERT INTO chunk_embeddings (tenant_id, chunk_id,
                            model_version_id, content_hash, embedding)
                        VALUES ($1,$2,$3,$4,$5)
                        ON CONFLICT (chunk_id, model_version_id) DO NOTHING
                        """,
                        self.tenant_id,
                        chunk_ids[c.hash],
                        str(embed_spec.model_version_id),
                        c.hash,
                        vec,
                    )
                    embedded += 1
                rec.output = {"embedded": embedded}

        # Stitch parent links so fn_expand_context can widen a hit to its
        # enclosing section.
        await self.conn.execute(
            """
            UPDATE chunks c SET parent_chunk_id = p.id
            FROM chunks p
            WHERE c.document_version_id = $1
              AND p.document_version_id = $1
              AND p.id <> c.id
              AND array_length(p.section_path,1) = array_length(c.section_path,1) - 1
              AND p.section_path = c.section_path[1:array_length(p.section_path,1)]
            """,
            str(dv_id),
        )

        actual = await self.conn.fetchval(
            "SELECT COALESCE(actual_cost_usd,0) FROM ingestion_runs WHERE id=$1", run_id
        )
        await self.conn.execute(
            """
            UPDATE ingestion_runs
               SET status='completed', finished_at=now(),
                   actual_chunk_count=$2, actual_embedded_chunks=$3, reused_chunks=$4
             WHERE id=$1
            """,
            run_id,
            len(chunks),
            embedded,
            reused,
        )
        await self.conn.execute(
            "UPDATE document_versions SET status='ready' WHERE id=$1", str(dv_id)
        )

        est_row = await self.conn.fetchrow(
            "SELECT est_cost_usd FROM ingestion_runs WHERE id=$1", run_id
        )
        return IngestResult(
            ingestion_run_id=run_id,
            document_version_id=dv_id,
            chunk_count=len(chunks),
            embedded_count=embedded,
            reused_count=reused,
            estimate=None,  # filled by the caller, which holds it
            actual_cost=float(actual or 0),
        )

    async def _contextualise(
        self, run_id: UUID, chunks: list[Chunk], document_text: str
    ) -> None:
        """Write a situating summary for each chunk.

        The document sits in the cached system prefix, so only the first call
        pays full input for it and the rest read it at the cache rate. Without
        that, this step costs roughly five times as much.
        """
        spec = self.resolve("contextualisation")
        system = spec.prompt["system"].replace("{document}", document_text)

        async with step(
            self.conn,
            node="contextualise",
            step_index=1,
            tenant_id=self.tenant_id,
            user_id=self.user_id,
            ingestion_run_id=run_id,
        ) as rec:
            rec.model_version_id = spec.model_version_id
            rec.prompt_version_id = spec.prompt_version_id
            rec.effort = spec.effort

            done = 0
            for c in chunks:
                user = spec.prompt["user"].replace("{chunk}", c.content).replace(
                    "{breadcrumb}", c.breadcrumb or ""
                )
                res = await spec.provider.complete(
                    system,
                    user,
                    max_output_tokens=spec.max_output_tokens,
                    effort=spec.effort,
                    cache_system=True,
                )
                rec.record_usage(res.usage)
                c.context_summary = res.text.strip()
                done += 1
            rec.output = {"contextualised": done}


def _json(value: Any) -> str:
    import json

    return json.dumps(value, default=str)
