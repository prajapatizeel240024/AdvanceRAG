-- ============================================================================
-- 005  Runs, run steps, and the cost ledger
-- ============================================================================
-- Two requirements land in this file:
--
--   "record in the database which version we used"  -- every run pins the
--   config bundle, and every step pins the prompt version and model version
--   that produced it. Provenance is a foreign key, not a log line.
--
--   "production cost ... should also be in the picture"  -- every billable call
--   writes a ledger row, and the dollar amount is computed by a DATABASE
--   TRIGGER at insert time using the price in force on that date. Cost is never
--   recomputed in Python and never re-derived later, so historical figures
--   cannot drift when prices change.
-- ============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- ingestion_runs -- estimate first, then spend
-- ---------------------------------------------------------------------------
-- The estimate is written BEFORE any embedding call, and the actuals are filled
-- in afterwards. Keeping both on one row makes "was our estimate any good?" a
-- subtraction rather than a research project -- and estimate accuracy is what
-- makes the pre-flight number trustworthy enough to act on.
CREATE TABLE ingestion_runs (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id           uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  document_version_id uuid NOT NULL REFERENCES document_versions(id) ON DELETE CASCADE,
  config_bundle_id    uuid NOT NULL REFERENCES config_bundles(id),
  started_by          uuid REFERENCES users(id),

  status              text NOT NULL DEFAULT 'estimated'
                        CHECK (status IN ('estimated','approved','running','completed','failed','rejected_over_budget')),

  -- Pre-flight estimate
  est_chunk_count       int,
  est_embedding_tokens  bigint,
  est_context_tokens_in bigint,
  est_context_tokens_out bigint,
  est_cost_usd          numeric(14,8),

  -- Post-flight actuals
  actual_chunk_count      int,
  actual_embedded_chunks  int,
  -- Chunks whose content hash already had an embedding, so cost nothing.
  reused_chunks           int DEFAULT 0,
  actual_cost_usd         numeric(14,8),

  started_at          timestamptz NOT NULL DEFAULT now(),
  finished_at         timestamptz,
  error               text
);

CREATE INDEX ingestion_runs_tenant_idx ON ingestion_runs (tenant_id, started_at DESC);

-- ---------------------------------------------------------------------------
-- conversations / query_runs
-- ---------------------------------------------------------------------------
CREATE TABLE conversations (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id  uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  user_id    uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  title      text,
  -- LangGraph's checkpointer keys on this.
  thread_id  text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, thread_id)
);

-- One row per question asked. This is the anchor for provenance: given a
-- query_run id you can reconstruct the entire pipeline that produced its answer.
CREATE TABLE query_runs (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id        uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  user_id          uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  conversation_id  uuid REFERENCES conversations(id) ON DELETE CASCADE,

  -- THE provenance link. Every answer is traceable to the exact set of YAML
  -- file versions in force when it ran.
  config_bundle_id uuid NOT NULL REFERENCES config_bundles(id),

  question         text NOT NULL,
  -- Normalised (lowercased, whitespace-collapsed, punctuation-stripped) hash,
  -- for the exact-match answer cache.
  question_hash    char(64) NOT NULL,

  intent           text,
  route_filter     jsonb,
  answer           text,
  -- 'answered' | 'refused_out_of_scope' | 'refused_no_evidence' | 'needs_clarification'
  outcome          text,
  degraded         boolean NOT NULL DEFAULT false,
  degraded_reason  text,

  cache_hit        text CHECK (cache_hit IN ('none','exact','semantic')),

  latency_ms       int,
  total_cost_usd   numeric(14,8) NOT NULL DEFAULT 0,

  created_at       timestamptz NOT NULL DEFAULT now(),
  finished_at      timestamptz
);

CREATE INDEX query_runs_tenant_idx  ON query_runs (tenant_id, created_at DESC);
CREATE INDEX query_runs_user_idx    ON query_runs (tenant_id, user_id, created_at DESC);
CREATE INDEX query_runs_bundle_idx  ON query_runs (config_bundle_id);
-- Cache probe: same question, same config bundle. Scoping to the bundle is a
-- correctness requirement, not a nicety -- serving a cached answer produced by
-- a previous prompt version after the prompt has changed silently reverts the
-- change for every repeat question.
CREATE INDEX query_runs_cache_idx
  ON query_runs (tenant_id, question_hash, config_bundle_id, created_at DESC)
  WHERE outcome IS NOT NULL;

-- ---------------------------------------------------------------------------
-- run_steps -- one row per graph node execution
-- ---------------------------------------------------------------------------
CREATE TABLE run_steps (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id         uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  query_run_id      uuid REFERENCES query_runs(id) ON DELETE CASCADE,
  ingestion_run_id  uuid REFERENCES ingestion_runs(id) ON DELETE CASCADE,

  node              text NOT NULL,
  step_index        int  NOT NULL,

  -- Per-step provenance: exactly which prompt text and which model ran here.
  prompt_version_id uuid REFERENCES prompt_versions(id),
  model_version_id  uuid REFERENCES model_versions(id),
  effort            text,
  -- Hash of the fully-rendered prompt, so an identical replay is detectable
  -- without storing every prompt body forever.
  rendered_hash     char(64),

  status            text NOT NULL DEFAULT 'ok' CHECK (status IN ('ok','error','skipped')),
  error             text,
  latency_ms        int,
  output            jsonb,

  created_at        timestamptz NOT NULL DEFAULT now(),

  -- A step belongs to exactly one kind of run, never both and never neither.
  CONSTRAINT run_steps_one_parent CHECK (
    (query_run_id IS NOT NULL AND ingestion_run_id IS NULL) OR
    (query_run_id IS NULL AND ingestion_run_id IS NOT NULL)
  )
);

CREATE INDEX run_steps_query_idx     ON run_steps (query_run_id, step_index);
CREATE INDEX run_steps_ingestion_idx ON run_steps (ingestion_run_id, step_index);

-- Which chunks were actually retrieved, at what rank, and whether the reranker
-- kept them. Without this, retrieval quality cannot be measured after the fact
-- and every regression investigation starts from zero.
CREATE TABLE retrieval_results (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id      uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  query_run_id   uuid NOT NULL REFERENCES query_runs(id) ON DELETE CASCADE,
  chunk_id       uuid NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,

  vector_rank    int,
  lexical_rank   int,
  fused_score    real,
  mmr_position   int,
  rerank_score   real,
  rerank_position int,
  -- Survived reranking and was placed in the generation context.
  used_in_answer boolean NOT NULL DEFAULT false,
  -- Actually cited by the model in its answer.
  cited          boolean NOT NULL DEFAULT false,

  UNIQUE (query_run_id, chunk_id)
);

CREATE INDEX retrieval_results_run_idx ON retrieval_results (query_run_id);

-- ---------------------------------------------------------------------------
-- cost_ledger -- one row per billable provider call
-- ---------------------------------------------------------------------------
-- Cost is a GENERATED column. The arithmetic lives in the database because it
-- must be identical for the estimator, the live path, the budget trigger and
-- every reporting view -- four Python call sites would eventually disagree, and
-- the disagreement would show up as a reconciliation mystery months later.
CREATE TABLE cost_ledger (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id         uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  user_id           uuid REFERENCES users(id) ON DELETE SET NULL,
  run_step_id       uuid REFERENCES run_steps(id) ON DELETE CASCADE,
  query_run_id      uuid REFERENCES query_runs(id) ON DELETE CASCADE,
  ingestion_run_id  uuid REFERENCES ingestion_runs(id) ON DELETE CASCADE,

  model_version_id  uuid NOT NULL REFERENCES model_versions(id),
  -- The exact price row used. Frozen at insert, never re-resolved.
  price_id          uuid REFERENCES model_prices(id),
  operation         text NOT NULL
                      CHECK (operation IN ('chat','embedding','count_tokens')),

  input_tokens        bigint NOT NULL DEFAULT 0,
  output_tokens       bigint NOT NULL DEFAULT 0,
  -- Anthropic reports these separately and they are priced differently:
  -- cache reads at 10% of input, cache writes at 125%. Folding them into
  -- input_tokens would overstate cost by roughly 10x on cached calls and hide
  -- the entire benefit of prompt caching.
  cache_read_tokens   bigint NOT NULL DEFAULT 0,
  cache_write_tokens  bigint NOT NULL DEFAULT 0,

  -- Unit prices copied in at insert time (USD per 1M tokens).
  input_per_1m       numeric(12,6) NOT NULL,
  output_per_1m      numeric(12,6) NOT NULL DEFAULT 0,
  cache_read_per_1m  numeric(12,6) NOT NULL DEFAULT 0,
  cache_write_per_1m numeric(12,6) NOT NULL DEFAULT 0,

  cost_usd numeric(16,10) GENERATED ALWAYS AS (
      (input_tokens       * input_per_1m
     + output_tokens      * output_per_1m
     + cache_read_tokens  * cache_read_per_1m
     + cache_write_tokens * cache_write_per_1m) / 1000000.0
  ) STORED,

  -- False for estimates, true for money actually spent. Both live in one table
  -- so estimate-vs-actual is a single query.
  is_actual   boolean NOT NULL DEFAULT true,
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX cost_ledger_tenant_day_idx ON cost_ledger (tenant_id, created_at DESC);
CREATE INDEX cost_ledger_user_day_idx   ON cost_ledger (tenant_id, user_id, created_at DESC);
CREATE INDEX cost_ledger_query_idx      ON cost_ledger (query_run_id);
CREATE INDEX cost_ledger_step_idx       ON cost_ledger (run_step_id);

-- ---------------------------------------------------------------------------
-- Budget enforcement -- in the database, not the API
-- ---------------------------------------------------------------------------
-- An API-layer budget check is advisory: any other code path with a database
-- connection bypasses it, and in a system that will grow to multiple users and
-- background ingestion jobs there will be other code paths. A BEFORE INSERT
-- trigger cannot be bypassed. This is the clearest case in the project for
-- pushing logic down into Postgres.
CREATE TABLE tenant_budgets (
  tenant_id         uuid PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
  per_user_daily_usd    numeric(12,4) NOT NULL DEFAULT 5.00,
  per_user_monthly_usd  numeric(12,4) NOT NULL DEFAULT 50.00,
  per_tenant_monthly_usd numeric(12,4) NOT NULL DEFAULT 500.00,
  on_exceed         text NOT NULL DEFAULT 'reject' CHECK (on_exceed IN ('reject','warn')),
  updated_at        timestamptz NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION fn_enforce_budget() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  b            tenant_budgets%ROWTYPE;
  spent_today  numeric(14,8);
  spent_month  numeric(14,8);
  tenant_month numeric(14,8);
BEGIN
  -- Estimates are not spend; only real calls count against a budget.
  IF NOT NEW.is_actual THEN RETURN NEW; END IF;

  SELECT * INTO b FROM tenant_budgets WHERE tenant_id = NEW.tenant_id;
  IF NOT FOUND THEN RETURN NEW; END IF;

  IF NEW.user_id IS NOT NULL THEN
    SELECT COALESCE(sum(cost_usd), 0) INTO spent_today
    FROM cost_ledger
    WHERE tenant_id = NEW.tenant_id AND user_id = NEW.user_id
      AND is_actual AND created_at >= date_trunc('day', now());

    IF spent_today >= b.per_user_daily_usd THEN
      IF b.on_exceed = 'reject' THEN
        RAISE EXCEPTION
          'Daily budget exceeded: $% of $% already spent today.',
          round(spent_today, 4), b.per_user_daily_usd
          USING ERRCODE = 'check_violation';
      ELSE
        RAISE WARNING 'Daily budget exceeded for user %', NEW.user_id;
      END IF;
    END IF;

    SELECT COALESCE(sum(cost_usd), 0) INTO spent_month
    FROM cost_ledger
    WHERE tenant_id = NEW.tenant_id AND user_id = NEW.user_id
      AND is_actual AND created_at >= date_trunc('month', now());

    IF spent_month >= b.per_user_monthly_usd AND b.on_exceed = 'reject' THEN
      RAISE EXCEPTION
        'Monthly budget exceeded: $% of $%.',
        round(spent_month, 4), b.per_user_monthly_usd
        USING ERRCODE = 'check_violation';
    END IF;
  END IF;

  SELECT COALESCE(sum(cost_usd), 0) INTO tenant_month
  FROM cost_ledger
  WHERE tenant_id = NEW.tenant_id
    AND is_actual AND created_at >= date_trunc('month', now());

  IF tenant_month >= b.per_tenant_monthly_usd AND b.on_exceed = 'reject' THEN
    RAISE EXCEPTION
      'Tenant monthly budget exceeded: $% of $%.',
      round(tenant_month, 4), b.per_tenant_monthly_usd
      USING ERRCODE = 'check_violation';
  END IF;

  RETURN NEW;
END $$;

CREATE TRIGGER cost_ledger_budget_guard
  BEFORE INSERT ON cost_ledger
  FOR EACH ROW EXECUTE FUNCTION fn_enforce_budget();

-- Roll a step's cost up onto its query_run so the UI's cost meter is a single
-- column read rather than an aggregate over the ledger on every render.
CREATE OR REPLACE FUNCTION fn_rollup_query_cost() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.query_run_id IS NOT NULL AND NEW.is_actual THEN
    UPDATE query_runs
       SET total_cost_usd = total_cost_usd + NEW.cost_usd
     WHERE id = NEW.query_run_id;
  END IF;
  IF NEW.ingestion_run_id IS NOT NULL AND NEW.is_actual THEN
    UPDATE ingestion_runs
       SET actual_cost_usd = COALESCE(actual_cost_usd, 0) + NEW.cost_usd
     WHERE id = NEW.ingestion_run_id;
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER cost_ledger_rollup
  AFTER INSERT ON cost_ledger
  FOR EACH ROW EXECUTE FUNCTION fn_rollup_query_cost();

-- ---------------------------------------------------------------------------
-- RLS
-- ---------------------------------------------------------------------------
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['ingestion_runs','conversations','query_runs',
                           'run_steps','retrieval_results','cost_ledger',
                           'tenant_budgets']
  LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE  ROW LEVEL SECURITY', t);
    EXECUTE format(
      'CREATE POLICY %I ON %I USING (tenant_id = app_current_tenant())
                             WITH CHECK (tenant_id = app_current_tenant())',
      t || '_isolation', t);
    EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON %I TO rag_app', t);
  END LOOP;
END
$$;

COMMIT;
