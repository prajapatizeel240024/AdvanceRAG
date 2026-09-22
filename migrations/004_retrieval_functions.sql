-- ============================================================================
-- 004  Retrieval: vector, lexical, RRF fusion, MMR, context expansion
-- ============================================================================
-- This file is the project's central claim made concrete. Every piece of
-- ranking mathematics -- similarity, rank fusion, diversification, thresholds,
-- context stitching -- lives here, in SQL. The Python retrieve node builds a
-- parameter tuple, issues one SELECT, and receives finished results. It does
-- not sort, score, merge or deduplicate anything.
--
-- Beyond dogma, there is a concrete reason: fusing two ranked lists in Python
-- means shipping 80 candidate rows with their text and 1536-float vectors over
-- the wire to reorder them and throw most away. Doing it in SQL ships 20 rows,
-- once, already ranked.
--
-- All functions are STABLE and SECURITY INVOKER: they execute as the calling
-- role, so RLS applies inside them. A SECURITY DEFINER function here would
-- quietly become a cross-tenant read primitive.
-- ============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- fn_vector_search
-- ---------------------------------------------------------------------------
-- Metadata filters are applied in the same statement as the ANN scan so
-- pgvector's iterative scan can do its job. The alternative -- fetch k by
-- vector, then filter in an outer query -- is the classic filtered-ANN bug:
-- with a selective filter, most of the k nearest are discarded and you silently
-- get 3 results where you asked for 20, exactly when the router has usefully
-- narrowed the search.
CREATE OR REPLACE FUNCTION fn_vector_search(
  p_model_version_id uuid,
  p_query_embedding  vector(1536),
  p_k                int     DEFAULT 40,
  p_filter           jsonb   DEFAULT '{}'::jsonb,
  p_document_version_id uuid DEFAULT NULL,
  p_min_similarity   real    DEFAULT 0.0
)
RETURNS TABLE (
  chunk_id   uuid,
  similarity real,
  rank       int
)
LANGUAGE sql STABLE AS $$
  WITH scored AS (
    SELECT
      c.id AS chunk_id,
      -- `<=>` is cosine DISTANCE (0 = identical). Similarity is 1 - distance.
      -- Conflating the two silently inverts the ranking, which is the kind of
      -- bug that still returns plausible-looking results.
      (1.0 - (ce.embedding <=> p_query_embedding))::real AS similarity
    FROM chunk_embeddings ce
    JOIN chunks c ON c.id = ce.chunk_id
    WHERE ce.model_version_id = p_model_version_id
      AND (p_document_version_id IS NULL
           OR c.document_version_id = p_document_version_id)
      -- jsonb containment: the router emits {"topic":"per_diem"} and only
      -- chunks whose metadata contains that pair survive. GIN-indexed.
      AND (p_filter = '{}'::jsonb OR c.metadata @> p_filter)
    ORDER BY ce.embedding <=> p_query_embedding
    LIMIT p_k
  )
  SELECT
    s.chunk_id,
    s.similarity,
    ROW_NUMBER() OVER (ORDER BY s.similarity DESC)::int AS rank
  FROM scored s
  WHERE s.similarity >= p_min_similarity;
$$;

-- ---------------------------------------------------------------------------
-- fn_lexical_search
-- ---------------------------------------------------------------------------
-- Indispensable for this corpus. Policy questions turn on exact tokens that
-- embeddings blur together: "Band 4" vs "Band 5", "Tier 2" vs "Tier 3",
-- "section 13.1", "USD 350". Vector search reliably confuses these; full-text
-- search does not.
--
-- websearch_to_tsquery is chosen over plainto_tsquery because it tolerates the
-- quoting and OR syntax people actually type, and never raises on odd input --
-- to_tsquery does, and a malformed user question should not 500.
CREATE OR REPLACE FUNCTION fn_lexical_search(
  p_query            text,
  p_k                int   DEFAULT 40,
  p_filter           jsonb DEFAULT '{}'::jsonb,
  p_document_version_id uuid DEFAULT NULL
)
RETURNS TABLE (
  chunk_id uuid,
  score    real,
  rank     int
)
LANGUAGE sql STABLE AS $$
  WITH q AS (
    SELECT websearch_to_tsquery('english', p_query) AS tsq
  ),
  scored AS (
    SELECT
      c.id AS chunk_id,
      -- Normalisation 32 maps rank into (0,1) as rank/(rank+1), which keeps
      -- the value bounded. RRF only uses ordering, but a bounded score is far
      -- easier to reason about when debugging.
      ts_rank_cd(c.tsv, q.tsq, 32)::real AS score
    FROM chunks c, q
    WHERE c.tsv @@ q.tsq
      AND (p_document_version_id IS NULL
           OR c.document_version_id = p_document_version_id)
      AND (p_filter = '{}'::jsonb OR c.metadata @> p_filter)
    ORDER BY ts_rank_cd(c.tsv, q.tsq, 32) DESC
    LIMIT p_k
  )
  SELECT
    s.chunk_id,
    s.score,
    ROW_NUMBER() OVER (ORDER BY s.score DESC)::int AS rank
  FROM scored s;
$$;

-- ---------------------------------------------------------------------------
-- fn_hybrid_search -- Reciprocal Rank Fusion
-- ---------------------------------------------------------------------------
-- RRF is used instead of normalising and adding the two scores because cosine
-- similarity and ts_rank_cd are not on comparable scales, and any mapping
-- between them is an arbitrary choice that quietly biases every result. RRF
-- consumes only RANK, which sidesteps the problem entirely:
--
--     score(d) = SUM over retrievers of  weight_r / (k + rank_r(d))
--
-- k = 60 is the constant from the original RRF paper; it damps the difference
-- between ranks 1 and 2 so a single retriever cannot dominate the fusion.
--
-- FULL OUTER JOIN is essential: a chunk found by only one arm must still be
-- scored, contributing its single term. An INNER JOIN would silently restrict
-- results to chunks both arms happened to find, which is the opposite of what
-- hybrid retrieval is for.
CREATE OR REPLACE FUNCTION fn_hybrid_search(
  p_model_version_id uuid,
  p_query_text       text,
  p_query_embedding  vector(1536),
  p_k                int    DEFAULT 40,
  p_limit            int    DEFAULT 30,
  p_filter           jsonb  DEFAULT '{}'::jsonb,
  p_document_version_id uuid DEFAULT NULL,
  p_rrf_k            int    DEFAULT 60,
  p_w_vector         real   DEFAULT 1.0,
  p_w_lexical        real   DEFAULT 0.8,
  p_min_similarity   real   DEFAULT 0.0
)
RETURNS TABLE (
  chunk_id        uuid,
  fused_score     real,
  vector_rank     int,
  lexical_rank    int,
  vector_similarity real
)
LANGUAGE sql STABLE AS $$
  WITH v AS (
    SELECT * FROM fn_vector_search(
      p_model_version_id, p_query_embedding, p_k, p_filter,
      p_document_version_id, p_min_similarity)
  ),
  l AS (
    SELECT * FROM fn_lexical_search(
      p_query_text, p_k, p_filter, p_document_version_id)
  )
  SELECT
    COALESCE(v.chunk_id, l.chunk_id) AS chunk_id,
    (COALESCE(p_w_vector  / (p_rrf_k + v.rank), 0)
   + COALESCE(p_w_lexical / (p_rrf_k + l.rank), 0))::real AS fused_score,
    v.rank       AS vector_rank,
    l.rank       AS lexical_rank,
    v.similarity AS vector_similarity
  FROM v
  FULL OUTER JOIN l ON l.chunk_id = v.chunk_id
  ORDER BY fused_score DESC
  LIMIT p_limit;
$$;

-- ---------------------------------------------------------------------------
-- fn_mmr_diversify -- Maximal Marginal Relevance
-- ---------------------------------------------------------------------------
-- Without diversification, "what is the per diem in Tokyo?" returns five
-- overlapping chunks from section 7.2 and never surfaces the city-tier
-- definition in section 6.1 that the answer actually depends on. Redundant
-- context is not merely wasteful; it crowds out the missing piece.
--
--     MMR = argmax [ lambda * rel(d) - (1 - lambda) * max_sim(d, selected) ]
--
-- Implemented as an explicit greedy loop rather than a recursive CTE. MMR is
-- inherently sequential -- each pick depends on everything picked before it --
-- and a recursive CTE expressing that is unreadable and no faster. With ~30
-- candidates the loop runs in single-digit milliseconds.
CREATE OR REPLACE FUNCTION fn_mmr_diversify(
  p_model_version_id uuid,
  p_chunk_ids        uuid[],
  p_scores           real[],
  p_query_embedding  vector(1536),
  p_lambda           real DEFAULT 0.7,
  p_limit            int  DEFAULT 20
)
RETURNS TABLE (
  chunk_id  uuid,
  mmr_score real,
  rank_position int
)
LANGUAGE plpgsql STABLE AS $$
DECLARE
  selected      uuid[] := '{}';
  best_id       uuid;
  best_val      real;
  cand_id       uuid;
  cand_rel      real;
  cand_penalty  real;
  cand_val      real;
  i             int;
  pos           int := 0;
  max_rel       real;
  min_rel       real;
BEGIN
  -- Normalise relevance to 0..1 so lambda trades off two comparable
  -- quantities. Raw RRF scores cluster near 0.03, so without this the
  -- diversity term dominates for every value of lambda and MMR degenerates
  -- into "return the most different chunks", ignoring relevance entirely.
  SELECT max(s), min(s) INTO max_rel, min_rel FROM unnest(p_scores) s;
  IF max_rel IS NULL THEN RETURN; END IF;
  IF max_rel = min_rel THEN max_rel := min_rel + 1; END IF;

  WHILE pos < p_limit AND array_length(selected, 1) IS DISTINCT FROM array_length(p_chunk_ids, 1) LOOP
    best_id  := NULL;
    best_val := NULL;

    FOR i IN 1 .. array_length(p_chunk_ids, 1) LOOP
      cand_id := p_chunk_ids[i];
      CONTINUE WHEN cand_id = ANY(selected);

      cand_rel := (p_scores[i] - min_rel) / (max_rel - min_rel);

      IF array_length(selected, 1) IS NULL THEN
        cand_penalty := 0;
      ELSE
        -- Greatest cosine similarity between this candidate and anything
        -- already chosen.
        SELECT COALESCE(max(1.0 - (a.embedding <=> b.embedding)), 0)::real
          INTO cand_penalty
        FROM chunk_embeddings a, chunk_embeddings b
        WHERE a.chunk_id = cand_id
          AND a.model_version_id = p_model_version_id
          AND b.chunk_id = ANY(selected)
          AND b.model_version_id = p_model_version_id;
      END IF;

      cand_val := p_lambda * cand_rel - (1 - p_lambda) * cand_penalty;

      IF best_val IS NULL OR cand_val > best_val THEN
        best_val := cand_val;
        best_id  := cand_id;
      END IF;
    END LOOP;

    EXIT WHEN best_id IS NULL;

    selected := selected || best_id;
    pos := pos + 1;
    chunk_id  := best_id;
    mmr_score := best_val;
    rank_position := pos;
    RETURN NEXT;
  END LOOP;
END $$;

-- ---------------------------------------------------------------------------
-- fn_expand_context -- retrieve small, read large
-- ---------------------------------------------------------------------------
-- Small chunks embed precisely; large chunks answer completely. We match on the
-- precise chunk and hand the model the enclosing section, so a hit on a single
-- per-diem table row arrives with its column headers and city tier attached
-- rather than as four naked numbers.
CREATE OR REPLACE FUNCTION fn_expand_context(
  p_chunk_ids        uuid[],
  p_max_tokens       int DEFAULT 1200
)
RETURNS TABLE (
  chunk_id       uuid,
  content        text,
  expanded       text,
  breadcrumb     text,
  section_path   text[],
  token_count    int,
  was_expanded   boolean
)
LANGUAGE sql STABLE AS $$
  WITH hit AS (
    SELECT c.*, ord.n AS input_order
    FROM unnest(p_chunk_ids) WITH ORDINALITY AS ord(cid, n)
    JOIN chunks c ON c.id = ord.cid
  ),
  -- Siblings sharing the same section path, which is how a table split across
  -- several chunks gets reassembled.
  sect AS (
    SELECT
      h.id AS hit_id,
      string_agg(s.content, E'\n\n' ORDER BY s.ordinal) AS section_text,
      sum(s.token_count) AS section_tokens
    FROM hit h
    JOIN chunks s
      ON s.document_version_id = h.document_version_id
     AND s.section_path = h.section_path
    GROUP BY h.id
  )
  SELECT
    h.id,
    h.content,
    -- Expand only when the whole section still fits the budget; otherwise the
    -- expansion would push the genuinely relevant chunk out of the context
    -- window, which is worse than not expanding at all.
    CASE WHEN sect.section_tokens <= p_max_tokens
         THEN sect.section_text ELSE h.content END,
    h.breadcrumb,
    h.section_path,
    h.token_count,
    (sect.section_tokens <= p_max_tokens AND sect.section_tokens > h.token_count)
  FROM hit h
  LEFT JOIN sect ON sect.hit_id = h.id
  ORDER BY h.input_order;
$$;

GRANT EXECUTE ON FUNCTION
  fn_vector_search(uuid, vector, int, jsonb, uuid, real),
  fn_lexical_search(text, int, jsonb, uuid),
  fn_hybrid_search(uuid, text, vector, int, int, jsonb, uuid, int, real, real, real),
  fn_mmr_diversify(uuid, uuid[], real[], vector, real, int),
  fn_expand_context(uuid[], int)
  TO rag_app;

COMMIT;
