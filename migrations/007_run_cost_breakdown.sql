-- ============================================================================
-- 007  Per-run cost breakdown view
-- ============================================================================
-- Added after `tests/test_api_thinness.py` caught an aggregation living in a
-- route handler: the /ask stream was computing its per-node cost breakdown with
-- an inline GROUP BY over cost_ledger.
--
-- That is precisely the drift the thin-API rule exists to prevent. The same
-- aggregation is wanted by the SSE cost event, the provenance endpoint, the
-- eval harness and anyone with psql -- four copies that would eventually
-- disagree about something like whether estimates count as spend.
--
-- A separate migration rather than an edit to 006, because migrations that have
-- been applied are history. Rewriting one means two databases claiming the same
-- version number with different schemas.
-- ============================================================================

BEGIN;

CREATE OR REPLACE VIEW v_run_cost_breakdown AS
SELECT
  cl.query_run_id,
  cl.tenant_id,
  rs.step_index,
  rs.node,
  mv.model_id,
  sum(cl.input_tokens)       AS input_tokens,
  sum(cl.output_tokens)      AS output_tokens,
  sum(cl.cache_read_tokens)  AS cache_read_tokens,
  sum(cl.cache_write_tokens) AS cache_write_tokens,
  sum(cl.cost_usd)           AS cost_usd
FROM cost_ledger cl
JOIN run_steps rs      ON rs.id = cl.run_step_id
JOIN model_versions mv ON mv.id = cl.model_version_id
WHERE cl.is_actual
GROUP BY cl.query_run_id, cl.tenant_id, rs.step_index, rs.node, mv.model_id;

-- The view is defined over RLS-protected base tables and is not SECURITY
-- DEFINER, so it executes as the caller and inherits their row visibility. No
-- tenant predicate is needed here, and adding one would hide whether the policy
-- is actually doing its job.
GRANT SELECT ON v_run_cost_breakdown TO rag_app;

COMMIT;
