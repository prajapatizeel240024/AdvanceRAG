-- ============================================================================
-- RLS isolation assertions.
-- ============================================================================
-- Tenant isolation is the one property in this system that fails silently and
-- catastrophically: a broken policy does not error, it just returns another
-- tenant's data. So it gets explicit assertions rather than a manual eyeball.
--
-- Run:  psql -d travel_rag -v ON_ERROR_STOP=1 -f scripts/verify_rls.sql
-- Every check RAISEs on failure, so a non-zero exit means isolation is broken.
--
-- SET ROLE rag_app is essential. Run as the owning superuser these assertions
-- all fail, because superusers bypass RLS -- which is precisely the trap this
-- file exists to catch.
-- ============================================================================

\set ON_ERROR_STOP on

DO $$
DECLARE
  t1 uuid := '00000000-0000-0000-0000-0000000000a1';
  t2 uuid := '00000000-0000-0000-0000-0000000000a2';
  u1 uuid;
  n  int;
BEGIN
  -- Arrange: two tenants, created with the bypass still in effect.
  DELETE FROM tenants WHERE id IN (t1, t2);
  INSERT INTO tenants (id, slug, name) VALUES
    (t1, '_rlstest_a', 'RLS Test A'),
    (t2, '_rlstest_b', 'RLS Test B');
  INSERT INTO users (tenant_id, email) VALUES (t1, 'a@test.example')
    RETURNING id INTO u1;
  INSERT INTO users (tenant_id, email) VALUES (t2, 'b@test.example');

  -- Drop to the unprivileged application role for the actual assertions.
  SET LOCAL ROLE rag_app;

  -- 1. A tenant sees exactly its own row, not its neighbour's.
  PERFORM set_config('app.tenant_id', t1::text, true);
  PERFORM set_config('app.user_id',   u1::text, true);
  SELECT count(*) INTO n FROM tenants WHERE slug LIKE '_rlstest_%';
  IF n <> 1 THEN
    RAISE EXCEPTION 'RLS FAIL: tenant A sees % tenant rows, expected 1', n;
  END IF;

  -- 2. Users are isolated too, not just the tenants table.
  SELECT count(*) INTO n FROM users WHERE email LIKE '%@test.example';
  IF n <> 1 THEN
    RAISE EXCEPTION 'RLS FAIL: tenant A sees % user rows, expected 1', n;
  END IF;

  -- 3. WITH CHECK blocks writing a row belonging to another tenant. Without
  --    it a tenant could insert data it is then unable to read back.
  BEGIN
    INSERT INTO users (tenant_id, email) VALUES (t2, 'mallory@test.example');
    RAISE EXCEPTION 'RLS FAIL: cross-tenant INSERT was allowed';
  EXCEPTION WHEN insufficient_privilege THEN
    NULL;  -- expected
  END;

  -- 4. Switching tenant switches the visible row; nothing is cached across.
  PERFORM set_config('app.tenant_id', t2::text, true);
  SELECT count(*) INTO n FROM tenants WHERE slug = '_rlstest_a';
  IF n <> 0 THEN
    RAISE EXCEPTION 'RLS FAIL: tenant B can see tenant A (% rows)', n;
  END IF;

  RESET ROLE;
  DELETE FROM tenants WHERE id IN (t1, t2);
  RAISE NOTICE 'RLS OK: isolation, write-check and tenant switching all verified.';
END
$$;

-- 5. An unset GUC must RAISE rather than quietly return an empty set. A silent
--    empty result is indistinguishable from "no data exists" and hides the bug.
DO $$
DECLARE n int;
BEGIN
  SET LOCAL ROLE rag_app;
  PERFORM set_config('app.tenant_id', '', true);
  BEGIN
    SELECT count(*) INTO n FROM tenants;
    RAISE EXCEPTION 'RLS FAIL: unset app.tenant_id silently returned % rows', n;
  EXCEPTION WHEN sqlstate 'P0001' THEN
    IF sqlerrm LIKE '%RLS FAIL%' THEN RAISE; END IF;
    NULL;  -- the accessor's own "tenant_id is not set" error: expected
  END;
  RESET ROLE;
  RAISE NOTICE 'RLS OK: missing tenant GUC fails loudly.';
END
$$;
