-- ============================================================================
-- Retrieval SQL assertions.
-- ============================================================================
-- The ranking mathematics lives in SQL, so it is tested in SQL. These use
-- synthetic vectors rather than real embeddings, which makes the expected
-- ordering exactly computable by hand -- with real embeddings the assertions
-- would only be checking that the functions run, not that they rank correctly.
--
-- Run: psql -d travel_rag -v ON_ERROR_STOP=1 -f scripts/verify_retrieval.sql
-- ============================================================================

\set ON_ERROR_STOP on

DO $$
DECLARE
  t        uuid := '00000000-0000-0000-0000-0000000000b1';
  doc      uuid;
  dv       uuid;
  mv       uuid;
  c1 uuid; c2 uuid; c3 uuid; c4 uuid;
  q        vector(1536);
  n        int;
  first_id uuid;
  fs_vec   real;
  fs_both  real;
BEGIN
  -- ---- arrange ------------------------------------------------------------
  DELETE FROM tenants WHERE id = t;
  INSERT INTO tenants (id, slug, name) VALUES (t, '_rtest', 'Retrieval Test');
  PERFORM set_config('app.tenant_id', t::text, true);

  SELECT id INTO mv FROM model_versions WHERE kind = 'embedding' LIMIT 1;
  IF mv IS NULL THEN
    RAISE EXCEPTION 'no embedding model registered; run scripts/seed.py first';
  END IF;

  INSERT INTO documents (tenant_id, slug, title)
    VALUES (t, '_rtest-doc', 'Test Policy') RETURNING id INTO doc;
  INSERT INTO document_versions (tenant_id, document_id, version_label,
                                 content_hash, status)
    VALUES (t, doc, 'v1', repeat('b', 64), 'ready') RETURNING id INTO dv;

  -- Four chunks. c1 is the target: it is both lexically and semantically
  -- closest. c4 is a near-duplicate of c1, which is what MMR must suppress.
  INSERT INTO chunks (tenant_id, document_version_id, ordinal, content_hash,
                      content, embed_text, breadcrumb, section_path,
                      section_title, token_count, metadata)
  VALUES
    (t, dv, 0, repeat('1',64), 'Per diem Tier 1 Tokyo is USD 110 per day.',
     'x', 'Doc > 7.2 Per diem', ARRAY['7','7.2'], '7.2 Per diem', 12,
     '{"topic":"per_diem"}'::jsonb),
    (t, dv, 1, repeat('2',64), 'Cabin class depends on flight duration and grade band.',
     'x', 'Doc > 4.2 Cabin', ARRAY['4','4.2'], '4.2 Cabin', 12,
     '{"topic":"air_travel"}'::jsonb),
    (t, dv, 2, repeat('3',64), 'Hotel nightly rate caps apply by city tier.',
     'x', 'Doc > 6.1 Hotel', ARRAY['6','6.1'], '6.1 Hotel', 12,
     '{"topic":"accommodation"}'::jsonb),
    (t, dv, 3, repeat('4',64), 'Per diem Tier 1 Tokyo is USD 110 daily allowance.',
     'x', 'Doc > 7.2 Per diem', ARRAY['7','7.2'], '7.2 Per diem', 12,
     '{"topic":"per_diem"}'::jsonb);

  SELECT id INTO c1 FROM chunks WHERE document_version_id=dv AND ordinal=0;
  SELECT id INTO c2 FROM chunks WHERE document_version_id=dv AND ordinal=1;
  SELECT id INTO c3 FROM chunks WHERE document_version_id=dv AND ordinal=2;
  SELECT id INTO c4 FROM chunks WHERE document_version_id=dv AND ordinal=3;

  -- Synthetic unit vectors along distinct axes, so cosine similarity to the
  -- query is exactly known: c1 = 1.0, c4 = ~0.99, c2/c3 much lower.
  q := ('[' || 1.0 || repeat(',0', 1535) || ']')::vector;

  INSERT INTO chunk_embeddings (tenant_id, chunk_id, model_version_id, content_hash, embedding) VALUES
    (t, c1, mv, repeat('1',64), ('[' || 1.0 || repeat(',0',1535) || ']')::vector),
    (t, c2, mv, repeat('2',64), ('[0,1' || repeat(',0',1534) || ']')::vector),
    (t, c3, mv, repeat('3',64), ('[0,0,1' || repeat(',0',1533) || ']')::vector),
    (t, c4, mv, repeat('4',64), ('[0.995,0.0999' || repeat(',0',1534) || ']')::vector);

  -- ---- 1. vector search orders by similarity -------------------------------
  SELECT chunk_id INTO first_id
  FROM fn_vector_search(mv, q, 10, '{}'::jsonb, dv, 0.0) ORDER BY rank LIMIT 1;
  IF first_id <> c1 THEN
    RAISE EXCEPTION 'vector search: expected c1 first, got %', first_id;
  END IF;

  -- ---- 2. similarity is 1 - distance, not distance -------------------------
  -- Getting this backwards inverts the ranking while still returning results,
  -- which is exactly the kind of bug that survives casual testing.
  DECLARE sim real;
  BEGIN
    SELECT similarity INTO sim
    FROM fn_vector_search(mv, q, 10, '{}'::jsonb, dv, 0.0) WHERE chunk_id = c1;
    IF sim < 0.99 THEN
      RAISE EXCEPTION 'expected similarity ~1.0 for the identical vector, got %', sim;
    END IF;
  END;

  -- ---- 3. min_similarity actually filters ----------------------------------
  SELECT count(*) INTO n
  FROM fn_vector_search(mv, q, 10, '{}'::jsonb, dv, 0.9);
  IF n <> 2 THEN
    RAISE EXCEPTION 'min_similarity 0.9 should keep only c1 and c4, kept %', n;
  END IF;

  -- ---- 4. metadata filter narrows to the routed topic ----------------------
  SELECT count(*) INTO n
  FROM fn_vector_search(mv, q, 10, '{"topic":"accommodation"}'::jsonb, dv, 0.0);
  IF n <> 1 THEN
    RAISE EXCEPTION 'topic filter should match exactly 1 chunk, matched %', n;
  END IF;

  -- ---- 5. lexical search finds exact tokens vectors blur -------------------
  SELECT count(*) INTO n FROM fn_lexical_search('Tokyo per diem', 10, '{}'::jsonb, dv);
  IF n < 1 THEN
    RAISE EXCEPTION 'lexical search found nothing for an exact phrase';
  END IF;

  -- ---- 6. RRF keeps chunks found by only ONE arm ---------------------------
  -- The FULL OUTER JOIN is what makes hybrid retrieval hybrid. An INNER JOIN
  -- would silently restrict results to the intersection of the two arms.
  SELECT count(*) INTO n
  FROM fn_hybrid_search(mv, 'nonexistentlexicalterm', q, 10, 10, '{}'::jsonb, dv);
  IF n < 3 THEN
    RAISE EXCEPTION
      'hybrid search dropped vector-only hits (got % rows); FULL OUTER JOIN broken', n;
  END IF;

  -- ---- 7. matching both arms outranks matching one -------------------------
  SELECT fused_score INTO fs_both
  FROM fn_hybrid_search(mv, 'Per diem Tier 1 Tokyo', q, 10, 10, '{}'::jsonb, dv)
  WHERE chunk_id = c1;
  SELECT fused_score INTO fs_vec
  FROM fn_hybrid_search(mv, 'nonexistentlexicalterm', q, 10, 10, '{}'::jsonb, dv)
  WHERE chunk_id = c1;
  IF fs_both <= fs_vec THEN
    RAISE EXCEPTION
      'RRF: a chunk matching both arms (%) should outscore vector-only (%)',
      fs_both, fs_vec;
  END IF;

  -- ---- 8. MMR suppresses the near-duplicate --------------------------------
  -- c1 and c4 are ~0.99 similar. With lambda favouring diversity, c4 must not
  -- take the second slot ahead of genuinely different material.
  DECLARE second uuid;
  BEGIN
    SELECT chunk_id INTO second
    FROM fn_mmr_diversify(mv, ARRAY[c1,c4,c2,c3],
                          ARRAY[0.9,0.89,0.5,0.4]::real[], q, 0.3, 2)
    WHERE rank_position = 2;
    IF second = c4 THEN
      RAISE EXCEPTION 'MMR picked the near-duplicate c4 second; diversification is not working';
    END IF;
  END;

  -- ---- 9. MMR returns at most the requested number -------------------------
  SELECT count(*) INTO n
  FROM fn_mmr_diversify(mv, ARRAY[c1,c2,c3,c4], ARRAY[0.9,0.8,0.7,0.6]::real[], q, 0.7, 2);
  IF n <> 2 THEN
    RAISE EXCEPTION 'MMR limit ignored: asked for 2, got %', n;
  END IF;

  -- ---- 10. context expansion stitches sibling chunks -----------------------
  -- c1 and c4 share section path 7.2, so expanding either should return both.
  DECLARE expanded text; was_exp boolean;
  BEGIN
    SELECT e.expanded, e.was_expanded INTO expanded, was_exp
    FROM fn_expand_context(ARRAY[c1], 5000) e;
    IF NOT was_exp THEN
      RAISE EXCEPTION 'context expansion did not widen c1 to its section';
    END IF;
    IF position('daily allowance' in expanded) = 0 THEN
      RAISE EXCEPTION 'expansion did not include the sibling chunk from the same section';
    END IF;
  END;

  -- ---- cleanup -------------------------------------------------------------
  DELETE FROM tenants WHERE id = t;
  RAISE NOTICE 'RETRIEVAL OK: vector, similarity sign, thresholds, filters, RRF fusion, MMR diversity and context expansion all verified.';
END
$$;
