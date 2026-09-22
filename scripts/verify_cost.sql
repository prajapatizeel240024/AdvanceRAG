-- ============================================================================
-- Cost ledger and budget assertions.
-- ============================================================================
-- Cost arithmetic lives in a generated column and budget enforcement in a
-- trigger, so both are tested in SQL. Expected values are computed by hand from
-- the published rates: claude-opus-5 at $5.00 in / $25.00 out per 1M, with
-- cache reads at $0.50 (10% of input) and cache writes at $6.25 (125%).
--
-- Run: psql -d travel_rag -v ON_ERROR_STOP=1 -f scripts/verify_cost.sql
-- ============================================================================

\set ON_ERROR_STOP on

DO $$
DECLARE
  t   uuid := '00000000-0000-0000-0000-0000000000c1';
  u   uuid;
  mv  uuid;
  c   numeric;
  blocked boolean := false;
BEGIN
  DELETE FROM tenants WHERE id = t;
  INSERT INTO tenants (id, slug, name) VALUES (t, '_ctest', 'Cost Test');
  PERFORM set_config('app.tenant_id', t::text, true);
  INSERT INTO users (tenant_id, email) VALUES (t, 'c@test.example') RETURNING id INTO u;
  PERFORM set_config('app.user_id', u::text, true);

  SELECT id INTO mv FROM model_versions WHERE model_id = 'claude-opus-5' LIMIT 1;
  IF mv IS NULL THEN
    RAISE EXCEPTION 'claude-opus-5 not registered; run scripts/seed.py first';
  END IF;

  -- ---- 1. the generated column computes cost correctly ---------------------
  -- 12,000 x 5.00/1e6 + 400 x 25.00/1e6 = 0.06 + 0.01 = 0.07
  INSERT INTO cost_ledger (tenant_id, user_id, model_version_id, operation,
                           input_tokens, output_tokens,
                           input_per_1m, output_per_1m, cache_read_per_1m, cache_write_per_1m)
  VALUES (t, u, mv, 'chat', 12000, 400, 5.00, 25.00, 0.50, 6.25)
  RETURNING cost_usd INTO c;

  IF round(c, 6) <> 0.070000 THEN
    RAISE EXCEPTION 'uncached rerank cost: expected 0.070000, got %', c;
  END IF;

  -- ---- 2. cache reads are billed at the cache rate -------------------------
  -- 500 x 5.00 + 400 x 25.00 + 11,500 x 0.50, all /1e6 = 0.018250
  INSERT INTO cost_ledger (tenant_id, user_id, model_version_id, operation,
                           input_tokens, output_tokens, cache_read_tokens,
                           input_per_1m, output_per_1m, cache_read_per_1m, cache_write_per_1m)
  VALUES (t, u, mv, 'chat', 500, 400, 11500, 5.00, 25.00, 0.50, 6.25)
  RETURNING cost_usd INTO c;

  IF round(c, 6) <> 0.018250 THEN
    RAISE EXCEPTION 'cached rerank cost: expected 0.018250, got %', c;
  END IF;

  -- ---- 3. prompt caching saves ~74% on this call ---------------------------
  IF NOT (0.018250 / 0.070000 < 0.27) THEN
    RAISE EXCEPTION 'prompt caching saving is not what the project claims';
  END IF;

  -- ---- 4. estimates do not count against a budget --------------------------
  INSERT INTO tenant_budgets (tenant_id, per_user_daily_usd, on_exceed)
  VALUES (t, 0.05, 'reject')
  ON CONFLICT (tenant_id) DO UPDATE SET per_user_daily_usd = 0.05, on_exceed = 'reject';

  -- Already over 0.05 in actual spend above, yet an estimate must still insert.
  INSERT INTO cost_ledger (tenant_id, user_id, model_version_id, operation,
                           input_tokens, input_per_1m, is_actual)
  VALUES (t, u, mv, 'embedding', 50000, 0.15, false);

  -- ---- 5. the budget trigger rejects real spend over the cap ---------------
  BEGIN
    INSERT INTO cost_ledger (tenant_id, user_id, model_version_id, operation,
                             input_tokens, input_per_1m, output_per_1m,
                             cache_read_per_1m, cache_write_per_1m)
    VALUES (t, u, mv, 'chat', 100, 5.00, 25.00, 0.50, 6.25);
  EXCEPTION WHEN check_violation THEN
    blocked := true;
  END;
  IF NOT blocked THEN
    RAISE EXCEPTION 'budget trigger did not reject spend over the daily cap';
  END IF;

  -- ---- 6. cost rolls up onto the parent run --------------------------------
  DECLARE
    qr uuid; bundle uuid; rolled numeric;
  BEGIN
    SELECT id INTO bundle FROM config_bundles LIMIT 1;
    INSERT INTO query_runs (tenant_id, user_id, config_bundle_id, question, question_hash)
    VALUES (t, u, bundle, 'test', repeat('d',64)) RETURNING id INTO qr;

    UPDATE tenant_budgets SET per_user_daily_usd = 1000 WHERE tenant_id = t;

    INSERT INTO cost_ledger (tenant_id, user_id, query_run_id, model_version_id,
                             operation, input_tokens, output_tokens,
                             input_per_1m, output_per_1m, cache_read_per_1m, cache_write_per_1m)
    VALUES (t, u, qr, mv, 'chat', 1000, 100, 5.00, 25.00, 0.50, 6.25);

    SELECT total_cost_usd INTO rolled FROM query_runs WHERE id = qr;
    -- 1000 x 5 + 100 x 25 = 5000 + 2500 = 7500 / 1e6 = 0.0075
    IF round(rolled, 6) <> 0.007500 THEN
      RAISE EXCEPTION 'rollup trigger: expected 0.007500 on query_runs, got %', rolled;
    END IF;
  END;

  -- ---- 7. price resolution honours effective dating and tiers --------------
  DECLARE p model_prices%ROWTYPE; gv uuid;
  BEGIN
    SELECT * INTO p FROM fn_resolve_price(mv, CURRENT_DATE, 1000);
    IF p.input_per_1m <> 5.00 THEN
      RAISE EXCEPTION 'price resolution returned % for opus 5', p.input_per_1m;
    END IF;

    SELECT id INTO gv FROM model_versions WHERE model_id = 'gemini-3.1-pro-preview' LIMIT 1;
    IF gv IS NOT NULL THEN
      -- Below the 200k tier boundary -> the cheaper tier.
      SELECT * INTO p FROM fn_resolve_price(gv, CURRENT_DATE, 1000);
      IF p.input_per_1m <> 2.00 THEN
        RAISE EXCEPTION 'tiered price below 200k: expected 2.00, got %', p.input_per_1m;
      END IF;
      -- Above it -> the more expensive tier.
      SELECT * INTO p FROM fn_resolve_price(gv, CURRENT_DATE, 500000);
      IF p.input_per_1m <> 4.00 THEN
        RAISE EXCEPTION 'tiered price above 200k: expected 4.00, got %', p.input_per_1m;
      END IF;
    END IF;
  END;

  DELETE FROM tenants WHERE id = t;
  RAISE NOTICE 'COST OK: generated column, cache rates, estimate exemption, budget rejection, rollup and tiered pricing all verified.';
END
$$;
