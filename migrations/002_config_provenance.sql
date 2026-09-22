-- ============================================================================
-- 002  Configuration provenance: which YAML version produced this answer
-- ============================================================================
-- This is the schema behind the project's first requirement. The question it
-- must answer, for any answer the system has ever produced, is:
--
--   "Which config, which prompt text, which model, which prices, which
--    retrieval parameters produced this?"
--
-- Storing a version string is not enough -- a string is a claim, and someone
-- will edit a YAML file without bumping it. So we store the full canonicalised
-- text and its sha256. The hash is the identity; the semver is a human label.
--
-- These tables are global rather than tenant-scoped: configuration is a
-- property of the deployment, not of a tenant. They carry no RLS and are
-- readable by every tenant, which is intentional -- a tenant must be able to
-- inspect the provenance of its own answers.
-- ============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- config_file_versions -- one immutable row per (logical file, content hash)
-- ---------------------------------------------------------------------------
CREATE TABLE config_file_versions (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  -- Logical name, e.g. 'models', 'retrieval', 'prompts/rerank'.
  name          text NOT NULL,
  -- Declared semver from the file's own `version:` key.
  semver        text NOT NULL,
  -- sha256 over the CANONICALISED text: keys sorted, LF endings, trailing
  -- whitespace stripped, no document markers. Canonicalisation matters because
  -- otherwise reordering two keys produces a "new" config that is semantically
  -- identical, and the registry fills with noise.
  content_hash  char(64) NOT NULL,
  -- The full text, so a run can be reconstructed years later even if the file
  -- is long gone from the working tree. This is the difference between
  -- provenance and a promise of provenance.
  content       text NOT NULL,
  parsed        jsonb NOT NULL,
  registered_at timestamptz NOT NULL DEFAULT now(),

  UNIQUE (name, content_hash)
);

CREATE INDEX config_file_versions_name_idx ON config_file_versions (name, registered_at DESC);

-- Append-only. A registered config version is a historical fact; mutating one
-- silently rewrites the provenance of every run that points at it.
CREATE OR REPLACE FUNCTION fn_forbid_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION
    'Table % is append-only; % is not permitted. Register a new version instead.',
    TG_TABLE_NAME, TG_OP;
END $$;

CREATE TRIGGER config_file_versions_immutable
  BEFORE UPDATE OR DELETE ON config_file_versions
  FOR EACH ROW EXECUTE FUNCTION fn_forbid_mutation();

-- ---------------------------------------------------------------------------
-- config_bundles -- the complete resolved configuration for a process
-- ---------------------------------------------------------------------------
-- A run does not reference eight separate config files; it references one
-- bundle. The bundle hash is derived from its members' hashes, so any change
-- to any file yields a new bundle id, and "runs grouped by config" -- which is
-- what A/B comparison and regression triage both need -- is a plain GROUP BY.
CREATE TABLE config_bundles (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  -- sha256 over the sorted list of "name:content_hash" pairs.
  bundle_hash  char(64) NOT NULL UNIQUE,
  label        text,
  created_at   timestamptz NOT NULL DEFAULT now(),
  git_sha      text,
  notes        text
);

CREATE TABLE config_bundle_members (
  bundle_id       uuid NOT NULL REFERENCES config_bundles(id) ON DELETE CASCADE,
  config_file_id  uuid NOT NULL REFERENCES config_file_versions(id),
  PRIMARY KEY (bundle_id, config_file_id)
);

CREATE TRIGGER config_bundles_immutable
  BEFORE UPDATE OR DELETE ON config_bundles
  FOR EACH ROW EXECUTE FUNCTION fn_forbid_mutation();

-- ---------------------------------------------------------------------------
-- prompt_versions -- prompts get first-class treatment
-- ---------------------------------------------------------------------------
-- Prompts change more often than any other configuration and cause the largest
-- behavioural swings, so they are addressable independently of the file that
-- carried them. A run step records exactly which prompt text it rendered.
CREATE TABLE prompt_versions (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  -- Graph node this prompt serves: 'routing', 'rerank', 'generation', ...
  role            text NOT NULL,
  semver          text NOT NULL,
  content_hash    char(64) NOT NULL,
  template        text NOT NULL,
  -- Declared variables, checked against the template at registration so a
  -- renamed variable fails at startup rather than rendering "{city}" literally
  -- into a production prompt.
  input_variables text[] NOT NULL DEFAULT '{}',
  config_file_id  uuid REFERENCES config_file_versions(id),
  registered_at   timestamptz NOT NULL DEFAULT now(),

  UNIQUE (role, content_hash)
);

CREATE INDEX prompt_versions_role_idx ON prompt_versions (role, registered_at DESC);

CREATE TRIGGER prompt_versions_immutable
  BEFORE UPDATE OR DELETE ON prompt_versions
  FOR EACH ROW EXECUTE FUNCTION fn_forbid_mutation();

-- ---------------------------------------------------------------------------
-- model_versions -- the resolved role -> model binding
-- ---------------------------------------------------------------------------
-- Captures the directive "everything on Opus 5 today, Gemini for translation
-- and routing later" as data rather than as a code branch. Flipping it is a
-- new row, and every historical run keeps pointing at the binding that was
-- actually in force when it ran.
CREATE TABLE model_versions (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  provider       text NOT NULL CHECK (provider IN ('anthropic', 'google')),
  -- Exact provider model string, e.g. 'claude-opus-5'. Never date-suffixed.
  model_id       text NOT NULL,
  kind           text NOT NULL CHECK (kind IN ('chat', 'embedding')),
  -- Embedding dimensionality actually stored, after any MRL truncation.
  dimensions     int,
  capabilities   jsonb NOT NULL DEFAULT '{}'::jsonb,
  config_file_id uuid REFERENCES config_file_versions(id),
  registered_at  timestamptz NOT NULL DEFAULT now(),

  -- NULLS NOT DISTINCT is load-bearing. `dimensions` is NULL for every chat
  -- model, and under the default NULLS DISTINCT a unique constraint treats
  -- each NULL as unique -- so the constraint silently permits unlimited
  -- duplicate rows for the same model. Observed in practice: registering the
  -- config twice produced three claude-opus-5 rows, which would then split
  -- one model's cost across three model_version_ids and quietly corrupt every
  -- per-model cost total. (Requires PostgreSQL 15+; this project targets 16.)
  UNIQUE NULLS NOT DISTINCT (provider, model_id, kind, dimensions)
);

-- role -> model, versioned. Two rows with the same role and different
-- config_file_id are two different assignments across two config versions.
CREATE TABLE role_assignments (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  config_file_id   uuid NOT NULL REFERENCES config_file_versions(id),
  role             text NOT NULL,
  model_version_id uuid NOT NULL REFERENCES model_versions(id),
  effort           text CHECK (effort IN ('low','medium','high','xhigh','max')),
  max_output_tokens int,
  params           jsonb NOT NULL DEFAULT '{}'::jsonb,

  UNIQUE (config_file_id, role)
);

-- ---------------------------------------------------------------------------
-- model_prices -- versioned, effective-dated, never retroactively applied
-- ---------------------------------------------------------------------------
-- The cost ledger stores a FK to the exact price row it used. Recomputing an
-- old run with today's prices would rewrite history and destroy the ability to
-- see a real cost trend, so prices are looked up once, at insert time, and
-- frozen into the ledger row.
CREATE TABLE model_prices (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  model_version_id  uuid NOT NULL REFERENCES model_versions(id),
  effective_from    date NOT NULL,
  -- USD per 1,000,000 tokens. numeric, never float: float accumulates rounding
  -- error across millions of ledger rows and makes the totals slightly wrong
  -- in a way nobody can reconcile.
  input_per_1m      numeric(12,6) NOT NULL,
  output_per_1m     numeric(12,6) NOT NULL DEFAULT 0,
  cache_read_per_1m numeric(12,6),
  cache_write_per_1m numeric(12,6),
  -- Gemini Pro prices tier on prompt size; null means the flat rate applies.
  tier_max_prompt_tokens int,
  confidence        text NOT NULL DEFAULT 'assumed'
                      CHECK (confidence IN ('verified', 'assumed')),
  source            text,
  config_file_id    uuid REFERENCES config_file_versions(id),

  -- Same NULL trap as above: `tier_max_prompt_tokens` is NULL for every
  -- flat-rate model, which is most of them.
  UNIQUE NULLS NOT DISTINCT (model_version_id, effective_from, tier_max_prompt_tokens)
);

CREATE INDEX model_prices_lookup_idx
  ON model_prices (model_version_id, effective_from DESC);

-- Resolve the price in force for a model on a date, honouring prompt-size
-- tiers. Kept in SQL so the ledger trigger, the estimator and the reporting
-- views cannot drift from one another.
CREATE OR REPLACE FUNCTION fn_resolve_price(
  p_model_version_id uuid,
  p_on_date          date,
  p_prompt_tokens    int DEFAULT 0
) RETURNS model_prices
LANGUAGE sql STABLE AS $$
  SELECT *
  FROM model_prices
  WHERE model_version_id = p_model_version_id
    AND effective_from  <= p_on_date
    AND (tier_max_prompt_tokens IS NULL
         OR p_prompt_tokens <= tier_max_prompt_tokens)
  ORDER BY effective_from DESC,
           -- Prefer the tightest matching tier over the open-ended one.
           tier_max_prompt_tokens ASC NULLS LAST
  LIMIT 1;
$$;

-- Grants mirror the append-only distinction.
--
-- These four are immutable history: a registered config version, bundle or
-- prompt is a fact about a run that already happened, so the app may read and
-- append but never modify. The trigger enforces it; withholding UPDATE means
-- the attempt fails at the privilege layer first, which is a clearer error.
GRANT SELECT, INSERT ON config_file_versions, config_bundles,
                          config_bundle_members, prompt_versions
  TO rag_app;

-- These three are current-state registries rather than history. Re-registering
-- an unchanged config upserts them, so UPDATE is required -- without it,
-- startup fails on the second run with "permission denied for table
-- model_versions".
GRANT SELECT, INSERT, UPDATE ON model_versions, role_assignments, model_prices
  TO rag_app;

COMMIT;
