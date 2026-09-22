-- ============================================================================
-- 003  Documents, versions, chunks and embeddings
-- ============================================================================
-- Content-addressing is the organising idea. Documents and chunks are keyed by
-- the sha256 of their normalised content, which buys three things at once:
--
--   * re-ingesting an unchanged file is a no-op instead of a duplicate,
--   * re-ingesting a lightly-edited policy re-embeds only the chunks that
--     actually changed -- the annual reissue of a travel policy typically
--     touches under 5% of its text,
--   * embeddings are reusable across document versions, which is the single
--     largest ingestion cost saving available.
-- ============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- documents / document_versions
-- ---------------------------------------------------------------------------
-- A document is the stable logical thing ("Northwind Travel Policy"). A
-- document_version is one concrete file (v4.1, v4.2). Questions can be answered
-- against the version in force on a given date, so a query about a March trip
-- is not answered from the April reissue.
CREATE TABLE documents (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id   uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  slug        text NOT NULL,
  title       text NOT NULL,
  doc_type    text NOT NULL DEFAULT 'policy',
  created_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, slug)
);

CREATE TABLE document_versions (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id       uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  document_id     uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  version_label   text NOT NULL,
  -- sha256 of the normalised source bytes. Re-uploading the identical file is
  -- detected here and short-circuits before a single token is embedded.
  content_hash    char(64) NOT NULL,
  source_filename text,
  source_format   text NOT NULL DEFAULT 'markdown'
                    CHECK (source_format IN ('markdown','pdf','docx','txt')),
  raw_content     text,
  byte_size       bigint,
  -- Effective-dating: which policy governed a given travel date.
  effective_from  date,
  effective_to    date,
  status          text NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','estimated','ingesting','ready','failed')),
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, document_id, content_hash)
);

CREATE INDEX document_versions_doc_idx ON document_versions (tenant_id, document_id, created_at DESC);

-- ---------------------------------------------------------------------------
-- chunks
-- ---------------------------------------------------------------------------
CREATE TABLE chunks (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id           uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  document_version_id uuid NOT NULL REFERENCES document_versions(id) ON DELETE CASCADE,

  ordinal             int  NOT NULL,
  -- sha256 over (normalised text + breadcrumb). The reuse key: if this hash
  -- already carries an embedding for the same model and dimensionality, the
  -- embedding is copied rather than repurchased.
  content_hash        char(64) NOT NULL,

  -- The clean original, shown in citations.
  content             text NOT NULL,
  -- What actually gets embedded: breadcrumb + contextual summary + content.
  -- Kept separate so a citation shows the policy's words, not our scaffolding.
  embed_text          text NOT NULL,

  breadcrumb          text,
  section_path        text[] NOT NULL DEFAULT '{}',
  section_title       text,
  -- The LLM-written situating sentence from Contextual Retrieval. Null when
  -- the feature is disabled in chunking.yaml.
  context_summary     text,

  token_count         int NOT NULL,
  contains_table      boolean NOT NULL DEFAULT false,
  -- Structure-aware chunking marks pieces of a split-by-row table so the
  -- expansion step can re-stitch them.
  table_part          int,

  -- Routing filters are applied against this. GIN-indexed below.
  metadata            jsonb NOT NULL DEFAULT '{}'::jsonb,

  -- Parent/child for "retrieve small, read large": match the precise chunk,
  -- hand the model the enclosing section.
  parent_chunk_id     uuid REFERENCES chunks(id) ON DELETE SET NULL,

  -- Lexical retrieval arm. A generated column keeps the tsvector in lockstep
  -- with the text -- an application-maintained column drifts the moment one
  -- code path forgets to update it.
  tsv                 tsvector GENERATED ALWAYS AS (
                        setweight(to_tsvector('english', coalesce(breadcrumb,'')), 'A') ||
                        setweight(to_tsvector('english', content), 'B')
                      ) STORED,

  created_at          timestamptz NOT NULL DEFAULT now(),
  UNIQUE (document_version_id, ordinal)
);

CREATE INDEX chunks_tsv_idx       ON chunks USING gin (tsv);
CREATE INDEX chunks_metadata_idx  ON chunks USING gin (metadata jsonb_path_ops);
CREATE INDEX chunks_trgm_idx      ON chunks USING gin (content gin_trgm_ops);
CREATE INDEX chunks_hash_idx      ON chunks (content_hash);
CREATE INDEX chunks_dv_idx        ON chunks (tenant_id, document_version_id);
CREATE INDEX chunks_parent_idx    ON chunks (parent_chunk_id) WHERE parent_chunk_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- embeddings
-- ---------------------------------------------------------------------------
-- Separate from chunks because one chunk may hold embeddings from several
-- models -- during a model migration both must coexist, and a query must be
-- able to say which model's vector space it is searching.
--
-- Dimensionality note, verified on this machine (PG 16.14 / pgvector 0.8.6):
--   vector(3072)  + hnsw -> ERROR: column cannot have more than 2000 dimensions
--   halfvec(3072) + hnsw -> OK
--   vector(1536)  + hnsw -> OK
-- gemini-embedding-001 emits 3072 natively, so we MRL-truncate to 1536 and
-- re-normalise. Switching to halfvec(3072) is a column-type change here plus
-- halfvec_cosine_ops on the index; nothing else in the system moves.
CREATE TABLE chunk_embeddings (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id        uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  chunk_id         uuid NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
  model_version_id uuid NOT NULL REFERENCES model_versions(id),
  content_hash     char(64) NOT NULL,
  embedding        vector(1536) NOT NULL,
  created_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (chunk_id, model_version_id)
);

-- The reuse lookup: "has this exact content already been embedded by this
-- exact model?" Hit -> copy the vector, spend nothing.
CREATE INDEX chunk_embeddings_reuse_idx
  ON chunk_embeddings (tenant_id, model_version_id, content_hash);

CREATE INDEX chunk_embeddings_hnsw_idx
  ON chunk_embeddings USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);

CREATE INDEX chunk_embeddings_tenant_idx ON chunk_embeddings (tenant_id);

-- ---------------------------------------------------------------------------
-- RLS
-- ---------------------------------------------------------------------------
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['documents','document_versions','chunks','chunk_embeddings']
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
