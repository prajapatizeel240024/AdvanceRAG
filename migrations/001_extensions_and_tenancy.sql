-- ============================================================================
-- 001  Extensions, application role, tenancy, and Row-Level Security
-- ============================================================================
-- Today there is one user with one document. This migration nevertheless
-- installs full multi-tenancy, because retrofitting tenant isolation onto a
-- live schema means backfilling a column onto every table, rewriting every
-- query, and hoping nothing was missed. Doing it now costs one column and one
-- policy per table and is invisible to the single-tenant case.
--
-- Isolation is enforced by RLS in the database rather than by WHERE clauses in
-- Python. A forgotten WHERE clause in one endpoint leaks another tenant's data;
-- a forgotten WHERE clause under RLS returns nothing. The failure mode of the
-- database-enforced design is "no data", which is safe.
-- ============================================================================

BEGIN;

CREATE EXTENSION IF NOT EXISTS vector;      -- pgvector 0.8.6, built against PG 16
CREATE EXTENSION IF NOT EXISTS pg_trgm;     -- fuzzy lexical matching
CREATE EXTENSION IF NOT EXISTS pgcrypto;    -- gen_random_uuid(), digest()

-- ---------------------------------------------------------------------------
-- Application role
-- ---------------------------------------------------------------------------
-- The API connects as rag_app, which is deliberately NOT a superuser and does
-- NOT own the tables.
--
-- This is not a stylistic preference. A superuser -- and any role carrying
-- BYPASSRLS -- ignores row-level security entirely, and FORCE ROW LEVEL
-- SECURITY does not change that. On a default Homebrew install the desktop user
-- IS a superuser, so connecting with it makes every policy below inert while
-- appearing to work. Verified on this machine: as the superuser, both tenants
-- were visible and a cross-tenant INSERT succeeded.
--
-- scripts/verify_rls.sql asserts the isolation holds and is part of `make test`,
-- so a regression here fails loudly instead of leaking quietly.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rag_app') THEN
    CREATE ROLE rag_app LOGIN;
  ELSE
    ALTER ROLE rag_app LOGIN;
  END IF;
  -- Never let the application role inherit a bypass.
  EXECUTE 'ALTER ROLE rag_app NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE';

  -- Roles are CLUSTER-wide, so rag_app may carry settings from anything else
  -- that ever used that name on this machine -- including a stale
  -- `search_path` pointing at schemas this database does not have, which makes
  -- every table "not exist" despite being right there. RESET ALL then set the
  -- values we actually want, so the role's behaviour is a property of this
  -- migration rather than of the cluster's history.
  EXECUTE 'ALTER ROLE rag_app RESET ALL';
  EXECUTE 'ALTER ROLE rag_app SET search_path = public';
  -- A runaway query should not hold a pooled connection open forever. Ingestion
  -- runs raise this per-transaction; 60s is ample for any request-path query.
  EXECUTE 'ALTER ROLE rag_app SET statement_timeout = ''60s''';
  EXECUTE 'ALTER ROLE rag_app SET idle_in_transaction_session_timeout = ''60s''';
END
$$;

-- Let the migration owner assume the app role for tests without a separate
-- connection. `SET ROLE rag_app` then exercises the real policy path.
DO $$
BEGIN
  EXECUTE format('GRANT rag_app TO %I', current_user);
EXCEPTION WHEN duplicate_object OR invalid_grant_operation THEN
  NULL;
END
$$;

-- ---------------------------------------------------------------------------
-- Session context
-- ---------------------------------------------------------------------------
-- The current tenant and user are carried in session GUCs, set per transaction
-- by the API. These accessors are STABLE (not IMMUTABLE) so the planner may
-- cache them within a statement but never across transactions.
--
-- They raise rather than return NULL on a missing GUC. A NULL tenant under an
-- `=` comparison makes every policy evaluate to NULL, which reads as false and
-- returns an empty set -- safe, but indistinguishable from "no rows exist" and
-- therefore maddening to debug. Failing loudly turns a silent empty result into
-- an obvious error.
CREATE OR REPLACE FUNCTION app_current_tenant() RETURNS uuid
LANGUAGE plpgsql STABLE AS $$
DECLARE v text;
BEGIN
  v := current_setting('app.tenant_id', true);
  IF v IS NULL OR v = '' THEN
    RAISE EXCEPTION 'app.tenant_id is not set for this session'
      USING HINT = 'The API must SET LOCAL app.tenant_id inside the request transaction.';
  END IF;
  RETURN v::uuid;
END $$;

CREATE OR REPLACE FUNCTION app_current_user() RETURNS uuid
LANGUAGE plpgsql STABLE AS $$
DECLARE v text;
BEGIN
  v := current_setting('app.user_id', true);
  IF v IS NULL OR v = '' THEN
    RAISE EXCEPTION 'app.user_id is not set for this session'
      USING HINT = 'The API must SET LOCAL app.user_id inside the request transaction.';
  END IF;
  RETURN v::uuid;
END $$;

-- ---------------------------------------------------------------------------
-- Tenants and users
-- ---------------------------------------------------------------------------
CREATE TABLE tenants (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  slug        text NOT NULL UNIQUE,
  name        text NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now(),
  settings    jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE users (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id    uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  email        text NOT NULL,
  display_name text,
  role         text NOT NULL DEFAULT 'member'
                 CHECK (role IN ('member', 'admin', 'service')),
  created_at   timestamptz NOT NULL DEFAULT now()
);

-- Case-insensitive uniqueness without pulling in the citext extension, which
-- is a non-core module and one more thing to install on a fresh machine.
CREATE UNIQUE INDEX users_tenant_email_key ON users (tenant_id, lower(email));
CREATE INDEX users_tenant_idx ON users (tenant_id);

-- ---------------------------------------------------------------------------
-- Row-Level Security
-- ---------------------------------------------------------------------------
-- FORCE is the load-bearing keyword. Plain ENABLE exempts the table owner, and
-- in a local dev setup the owner is exactly who connects -- so policies would
-- appear to work while actually being bypassed, and the isolation bug would
-- surface only in production under a different role.
ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenants FORCE ROW LEVEL SECURITY;
ALTER TABLE users   ENABLE ROW LEVEL SECURITY;
ALTER TABLE users   FORCE ROW LEVEL SECURITY;

CREATE POLICY tenants_isolation ON tenants
  USING (id = app_current_tenant())
  WITH CHECK (id = app_current_tenant());

CREATE POLICY users_isolation ON users
  USING (tenant_id = app_current_tenant())
  WITH CHECK (tenant_id = app_current_tenant());

-- Both USING and WITH CHECK are specified deliberately. USING filters what is
-- readable; WITH CHECK constrains what may be written. With USING alone, a
-- tenant could INSERT a row carrying another tenant's id -- writing data it
-- would then be unable to see.

-- ---------------------------------------------------------------------------
-- Bootstrap principal lookup
-- ---------------------------------------------------------------------------
-- There is one genuine chicken-and-egg problem in an RLS design: to set
-- app.tenant_id the application must first discover which tenant it is, and
-- discovering that means reading `tenants` -- which RLS forbids until
-- app.tenant_id is set.
--
-- This function is the single sanctioned escape, and it is deliberately narrow:
-- it takes a tenant slug, returns exactly one row of identity, and touches
-- nothing else. SECURITY DEFINER makes it run as the owner, so it sees past
-- RLS; that is precisely the privilege escalation being granted, and the reason
-- it must not be widened. A general-purpose SECURITY DEFINER helper over these
-- tables would be a cross-tenant read primitive.
--
-- `SET search_path` is mandatory on a SECURITY DEFINER function: without it a
-- caller can prepend a schema of their own and have the function resolve
-- `tenants` to a table they control, executing it with owner privileges.
CREATE OR REPLACE FUNCTION fn_bootstrap_principal(p_tenant_slug text)
RETURNS TABLE (tenant_id uuid, tenant_slug text, user_id uuid, email text)
LANGUAGE sql
SECURITY DEFINER
SET search_path = public, pg_temp
STABLE AS $$
  SELECT t.id, t.slug, u.id, u.email
  FROM tenants t
  JOIN users u ON u.tenant_id = t.id
  WHERE t.slug = p_tenant_slug
  ORDER BY u.created_at
  LIMIT 1;
$$;

REVOKE ALL ON FUNCTION fn_bootstrap_principal(text) FROM PUBLIC;

GRANT USAGE ON SCHEMA public TO rag_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON tenants, users TO rag_app;
GRANT EXECUTE ON FUNCTION fn_bootstrap_principal(text) TO rag_app;

COMMIT;
