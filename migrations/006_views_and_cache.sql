-- ============================================================================
-- 006  Provenance views, cost reporting, and the answer cache
-- ============================================================================
-- Reporting lives in the database as views rather than in Python as query
-- builders. A view is one definition that the API, the CLI, the eval harness
-- and a human with psql all share; four hand-written aggregations in Python
-- would eventually disagree about something like whether estimates count as
-- spend, and nobody would notice until the numbers stopped reconciling.
-- ============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- v_run_provenance -- the money query
-- ---------------------------------------------------------------------------
-- This view is the direct answer to "record in the database which version we
-- used". Given a query_run id it returns, for every step of the pipeline, the
-- exact prompt text, prompt version, model id, effort and config bundle that
-- produced the answer -- reconstructable years later, from the database alone,
-- with no reference to the working tree.
CREATE OR REPLACE VIEW v_run_provenance AS
SELECT
  qr.id                AS query_run_id,
  qr.tenant_id,
  qr.created_at,
  qr.question,
  qr.outcome,
  qr.total_cost_usd,

  cb.id                AS config_bundle_id,
  cb.bundle_hash,
  cb.label             AS config_label,
  cb.git_sha,

  rs.step_index,
  rs.node,
  rs.status            AS step_status,
  rs.latency_ms        AS step_latency_ms,
  rs.effort,

  pv.role              AS prompt_role,
  pv.semver            AS prompt_version,
  pv.content_hash      AS prompt_hash,
  pv.template          AS prompt_template,
  rs.rendered_hash,

  mv.provider,
  mv.model_id,
  mv.dimensions        AS model_dimensions,

  -- Cost attributed to this individual step.
  COALESCE(cl.step_cost, 0)        AS step_cost_usd,
  COALESCE(cl.step_input_tokens, 0)  AS step_input_tokens,
  COALESCE(cl.step_output_tokens, 0) AS step_output_tokens,
  COALESCE(cl.step_cache_read, 0)    AS step_cache_read_tokens
FROM query_runs qr
JOIN config_bundles cb   ON cb.id = qr.config_bundle_id
LEFT JOIN run_steps rs   ON rs.query_run_id = qr.id
LEFT JOIN prompt_versions pv ON pv.id = rs.prompt_version_id
LEFT JOIN model_versions  mv ON mv.id = rs.model_version_id
LEFT JOIN LATERAL (
  SELECT sum(cost_usd)          AS step_cost,
         sum(input_tokens)      AS step_input_tokens,
         sum(output_tokens)     AS step_output_tokens,
         sum(cache_read_tokens) AS step_cache_read
  FROM cost_ledger
  WHERE run_step_id = rs.id AND is_actual
) cl ON true;

-- The full YAML text of every file in the bundle that produced a run. This is
-- what makes the provenance claim real rather than aspirational: the config is
-- reconstructable even if the repository is gone.
CREATE OR REPLACE VIEW v_run_config_files AS
SELECT
  qr.id      AS query_run_id,
  cfv.name   AS config_file,
  cfv.semver,
  cfv.content_hash,
  cfv.content
FROM query_runs qr
JOIN config_bundle_members cbm ON cbm.bundle_id = qr.config_bundle_id
JOIN config_file_versions cfv  ON cfv.id = cbm.config_file_id;

-- ---------------------------------------------------------------------------
-- Cost reporting
-- ---------------------------------------------------------------------------
-- Percentiles, not just averages. Mean cost per query is dominated by the
-- cheap cache hits and hides the expensive tail that actually drives the bill;
-- p95 is the number worth watching.
CREATE OR REPLACE VIEW v_cost_per_query AS
SELECT
  qr.tenant_id,
  date_trunc('day', qr.created_at)::date AS day,
  qr.config_bundle_id,
  count(*)                                    AS queries,
  count(*) FILTER (WHERE qr.cache_hit <> 'none') AS cache_hits,
  round(avg(qr.total_cost_usd), 8)            AS mean_cost_usd,
  round(percentile_cont(0.5) WITHIN GROUP (ORDER BY qr.total_cost_usd)::numeric, 8) AS p50_cost_usd,
  round(percentile_cont(0.95) WITHIN GROUP (ORDER BY qr.total_cost_usd)::numeric, 8) AS p95_cost_usd,
  round(sum(qr.total_cost_usd), 6)            AS total_cost_usd,
  round(avg(qr.latency_ms))                   AS mean_latency_ms,
  round(percentile_cont(0.95) WITHIN GROUP (ORDER BY qr.latency_ms)::numeric) AS p95_latency_ms
FROM query_runs qr
GROUP BY qr.tenant_id, day, qr.config_bundle_id;

-- Which graph node is actually spending the money. Almost always rerank and
-- generation; this view is how you confirm that rather than assume it.
CREATE OR REPLACE VIEW v_cost_by_node AS
SELECT
  cl.tenant_id,
  rs.node,
  mv.model_id,
  count(*)                          AS calls,
  sum(cl.input_tokens)              AS input_tokens,
  sum(cl.output_tokens)             AS output_tokens,
  sum(cl.cache_read_tokens)         AS cache_read_tokens,
  round(sum(cl.cost_usd), 6)        AS total_cost_usd,
  round(avg(cl.cost_usd), 8)        AS mean_cost_usd,
  -- Share of input tokens served from cache. If this is near zero on the
  -- rerank node, a supposedly stable prefix is being invalidated somewhere.
  CASE WHEN sum(cl.input_tokens + cl.cache_read_tokens) > 0
       THEN round(100.0 * sum(cl.cache_read_tokens)
                  / sum(cl.input_tokens + cl.cache_read_tokens), 1)
       ELSE 0 END                   AS cache_hit_pct
FROM cost_ledger cl
JOIN run_steps rs     ON rs.id = cl.run_step_id
JOIN model_versions mv ON mv.id = cl.model_version_id
WHERE cl.is_actual
GROUP BY cl.tenant_id, rs.node, mv.model_id;

CREATE OR REPLACE VIEW v_cost_by_user AS
SELECT
  cl.tenant_id,
  cl.user_id,
  u.email,
  date_trunc('day', cl.created_at)::date AS day,
  round(sum(cl.cost_usd), 6)  AS spent_usd,
  count(DISTINCT cl.query_run_id) AS queries
FROM cost_ledger cl
LEFT JOIN users u ON u.id = cl.user_id
WHERE cl.is_actual
GROUP BY cl.tenant_id, cl.user_id, u.email, day;

-- Estimate accuracy. The pre-ingestion dollar figure is only worth showing if
-- it is close; this view is how that claim gets checked instead of asserted.
CREATE OR REPLACE VIEW v_ingestion_estimate_accuracy AS
SELECT
  ir.id AS ingestion_run_id,
  ir.tenant_id,
  dv.version_label,
  d.title,
  ir.est_chunk_count,
  ir.actual_chunk_count,
  ir.reused_chunks,
  ir.est_cost_usd,
  ir.actual_cost_usd,
  (ir.actual_cost_usd - ir.est_cost_usd) AS variance_usd,
  CASE WHEN ir.est_cost_usd > 0
       THEN round(100.0 * (ir.actual_cost_usd - ir.est_cost_usd) / ir.est_cost_usd, 2)
       ELSE NULL END AS variance_pct
FROM ingestion_runs ir
JOIN document_versions dv ON dv.id = ir.document_version_id
JOIN documents d          ON d.id = dv.document_id
WHERE ir.status = 'completed';

-- ---------------------------------------------------------------------------
-- Retrieval quality, grouped by config bundle
-- ---------------------------------------------------------------------------
-- "Did config v3 beat v2?" should be a GROUP BY, not an afternoon. Because
-- every run is tagged with its bundle, it is.
CREATE OR REPLACE VIEW v_quality_by_config AS
SELECT
  qr.tenant_id,
  cb.id    AS config_bundle_id,
  cb.label AS config_label,
  cb.bundle_hash,
  count(*) AS runs,
  count(*) FILTER (WHERE qr.outcome = 'answered')             AS answered,
  count(*) FILTER (WHERE qr.outcome LIKE 'refused%')          AS refused,
  count(*) FILTER (WHERE qr.outcome = 'needs_clarification')  AS clarified,
  count(*) FILTER (WHERE qr.degraded)                         AS degraded,
  -- Citation rate: of the chunks placed in the generation context, how many
  -- the model actually used. A persistently low value means the reranker is
  -- passing through chunks that do not earn their tokens.
  round(avg(rr.cited_ratio), 3)      AS mean_cited_ratio,
  round(avg(qr.total_cost_usd), 8)   AS mean_cost_usd,
  round(avg(qr.latency_ms))          AS mean_latency_ms
FROM query_runs qr
JOIN config_bundles cb ON cb.id = qr.config_bundle_id
LEFT JOIN LATERAL (
  SELECT CASE WHEN count(*) FILTER (WHERE used_in_answer) > 0
              THEN count(*) FILTER (WHERE cited)::numeric
                 / count(*) FILTER (WHERE used_in_answer)
              ELSE NULL END AS cited_ratio
  FROM retrieval_results WHERE query_run_id = qr.id
) rr ON true
GROUP BY qr.tenant_id, cb.id, cb.label, cb.bundle_hash;

-- ---------------------------------------------------------------------------
-- Answer cache
-- ---------------------------------------------------------------------------
-- Two tiers. Exact match is free and always safe. Semantic match is cheap and
-- sometimes wrong, so it is gated behind a deliberately high threshold.
--
-- Both are scoped to the config bundle. This is a correctness requirement: an
-- answer produced under a previous prompt version must not be served after the
-- prompt changes, or the change silently fails to take effect for every
-- repeated question -- the most confusing possible way for a prompt fix to
-- appear not to work.
CREATE TABLE answer_cache (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id        uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  config_bundle_id uuid NOT NULL REFERENCES config_bundles(id),
  -- Scoping to the document version too, so re-ingesting a new policy edition
  -- invalidates cached answers derived from the old one.
  document_version_id uuid REFERENCES document_versions(id) ON DELETE CASCADE,

  question_hash    char(64) NOT NULL,
  question         text NOT NULL,
  question_embedding vector(1536),

  answer           text NOT NULL,
  citations        jsonb NOT NULL DEFAULT '[]'::jsonb,
  outcome          text NOT NULL,
  -- Cost of the run that populated this entry, so the UI can show what a cache
  -- hit saved rather than just reporting zero.
  source_cost_usd  numeric(14,8) NOT NULL DEFAULT 0,

  hit_count        int NOT NULL DEFAULT 0,
  created_at       timestamptz NOT NULL DEFAULT now(),
  expires_at       timestamptz,

  UNIQUE (tenant_id, config_bundle_id, question_hash)
);

CREATE INDEX answer_cache_semantic_idx
  ON answer_cache USING hnsw (question_embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);

CREATE INDEX answer_cache_lookup_idx
  ON answer_cache (tenant_id, config_bundle_id, question_hash);

-- Cache probe. Exact match first, then semantic.
--
-- The similarity threshold defaults to 0.97, which is high on purpose. The
-- corpus contains the designed trap "what is the per diem in Tokyo?" versus
-- "what is the per diem in Bengaluru?" -- near-identical sentences with
-- different correct answers (USD 110 vs USD 70). At a common threshold of 0.92
-- those collide and the cache confidently serves the wrong figure. A cache
-- that is wrong is worse than no cache, so this errs toward missing.
CREATE OR REPLACE FUNCTION fn_cache_lookup(
  p_config_bundle_id uuid,
  p_question_hash    char(64),
  p_question_embedding vector(1536) DEFAULT NULL,
  p_document_version_id uuid DEFAULT NULL,
  p_semantic_threshold real DEFAULT 0.97
)
RETURNS TABLE (
  cache_id   uuid,
  answer     text,
  citations  jsonb,
  outcome    text,
  hit_kind   text,
  similarity real,
  saved_usd  numeric
)
LANGUAGE sql STABLE AS $$
  WITH exact AS (
    SELECT ac.id, ac.answer, ac.citations, ac.outcome,
           'exact'::text AS hit_kind, 1.0::real AS similarity, ac.source_cost_usd
    FROM answer_cache ac
    WHERE ac.config_bundle_id = p_config_bundle_id
      AND ac.question_hash    = p_question_hash
      AND (ac.expires_at IS NULL OR ac.expires_at > now())
      AND (p_document_version_id IS NULL
           OR ac.document_version_id = p_document_version_id)
    LIMIT 1
  ),
  semantic AS (
    SELECT ac.id, ac.answer, ac.citations, ac.outcome,
           'semantic'::text AS hit_kind,
           (1.0 - (ac.question_embedding <=> p_question_embedding))::real AS similarity,
           ac.source_cost_usd
    FROM answer_cache ac
    WHERE p_question_embedding IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM exact)
      AND ac.config_bundle_id = p_config_bundle_id
      AND ac.question_embedding IS NOT NULL
      AND (ac.expires_at IS NULL OR ac.expires_at > now())
      AND (p_document_version_id IS NULL
           OR ac.document_version_id = p_document_version_id)
      AND (1.0 - (ac.question_embedding <=> p_question_embedding)) >= p_semantic_threshold
    ORDER BY ac.question_embedding <=> p_question_embedding
    LIMIT 1
  )
  SELECT * FROM exact
  UNION ALL
  SELECT * FROM semantic;
$$;

ALTER TABLE answer_cache ENABLE ROW LEVEL SECURITY;
ALTER TABLE answer_cache FORCE  ROW LEVEL SECURITY;
CREATE POLICY answer_cache_isolation ON answer_cache
  USING (tenant_id = app_current_tenant())
  WITH CHECK (tenant_id = app_current_tenant());

GRANT SELECT, INSERT, UPDATE, DELETE ON answer_cache TO rag_app;
GRANT SELECT ON v_run_provenance, v_run_config_files, v_cost_per_query,
                v_cost_by_node, v_cost_by_user, v_ingestion_estimate_accuracy,
                v_quality_by_config TO rag_app;
GRANT EXECUTE ON FUNCTION fn_cache_lookup(uuid, char, vector, uuid, real) TO rag_app;

COMMIT;
