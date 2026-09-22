# Travel Policy RAG

A grounded question-answering system over a corporate travel policy. Ask
"what's the per diem in Tokyo?" and get an answer with citations back to the
exact clause — or an honest "the policy doesn't cover this".

Python + LangGraph backend, React frontend, and a PostgreSQL database that owns
the retrieval mathematics, the cost accounting and the tenant isolation rather
than delegating them to application code.

---

## The one-minute version

```bash
make setup      # venv + deps (needs uv, pnpm, Postgres 16)
make db-up      # start Postgres
make db-reset   # apply migrations
make seed       # default tenant + register the YAML config
make estimate   # price ingesting the corpus — spends nothing, needs no API keys
make ingest     # embed and store (needs GEMINI_API_KEY)
make dev        # API on :8000, UI on :5173
```

`make estimate` works with no credentials at all. That is deliberate: knowing
what a document costs is only useful *before* you pay for it.

---

## What this is built around

Four decisions shape everything else.

### 1. The database is the system, not the storage

The brief asked for logic in the database and thin API layers, and this is
where that pays off rather than being an aesthetic preference.

| Concern | Where it lives | Why there |
|---|---|---|
| Vector + lexical retrieval, RRF fusion, MMR | `migrations/004` — SQL functions | Fusing in Python means shipping 80 rows with their 1536-float vectors over the wire to reorder them and discard most. In SQL it ships 20 rows, already ranked. |
| Cost arithmetic | `cost_ledger.cost_usd`, a generated column | The estimator, the live path, the budget trigger and five reporting views all need this number. Four Python call sites would eventually disagree, and the disagreement would surface months later as an unreconcilable total. |
| Budget enforcement | `BEFORE INSERT` trigger | An API-layer check is advisory — anything else holding a connection bypasses it. A trigger cannot be bypassed. |
| Tenant isolation | RLS policies on all 14 tenant tables | A forgotten `WHERE tenant_id` leaks another tenant's data. A forgotten `WHERE` under RLS returns nothing. The failure mode of the database-enforced design is "no data", which is safe. |
| Provenance | Foreign keys from `run_steps` to `prompt_versions` / `model_versions` | A log line is a claim. A foreign key is a fact. |

What stays in Python: LLM calls, graph control flow, HTTP. That is the line.

`backend/app/main.py` is 9 endpoints and no business logic — the `/ask` handler
opens a transaction, drives the graph and serialises events.

### 2. Configuration is versioned data, and every run records which version it used

Nothing in the codebase names a model. Call sites ask for a *role* —
`"rerank"`, `"routing"` — and `config/models.yaml` resolves it.

On startup every YAML file under `config/` is canonicalised (sorted keys, LF,
stripped trailing whitespace), hashed with sha256, and registered into
`config_file_versions`. Their combined hash forms a `config_bundle`, and every
`query_run` and `ingestion_run` points at it.

Why hash as well as declare a version: the `version:` field is a claim a human
makes, and humans edit files without bumping it. The hash is the identity.

The payoff is `GET /api/runs/{id}/provenance`, which returns — for any answer
the system has ever produced — the exact prompt text, prompt version, model id,
effort setting and full YAML of every config file involved. Reconstructable from
the database alone, years later, after the working tree has moved on.

Switching the system from all-Opus-5 to the Gemini-mixed target state is an edit
to one YAML file. Every historical run keeps pointing at the binding that was
actually in force when it ran.

### 3. Cost is measured, not estimated after the fact

Every billable call writes a `cost_ledger` row carrying the token counts *and*
the unit prices in force at that moment. Prices are versioned and
effective-dated, so a price change cannot retroactively rewrite what last
month's runs cost.

**Ingestion** (this corpus: 71 chunks, ~5,600 tokens):

| | Cost | Note |
|---|---|---|
| Embedding the whole document | **$0.00099** | gemini-embedding-001 at $0.15/1M |
| Contextualising it (Opus 5, cached prefix) | **$0.68364** | 71 calls, document cached after the first |
| **Full ingestion** | **$0.68463** | |
| Re-ingesting after a 5% edit | **~$0.05** | 67 of 71 chunks reused by content hash — **~87% cheaper** |

**Per query**, from the token sizes of the actual prompts:

| Node | Cold | Share |
|---|---|---|
| route | $0.01350 | 10.0% |
| translate | $0.01400 | 10.4% |
| rerank | $0.07000 | 51.9% |
| generate | $0.03725 | 27.6% |
| **Total (cold)** | **$0.13475** | |
| Total, instruction prefixes cached | **$0.06768** | **−49.8%** |
| …plus Gemini Flash on route + translate | **$0.04431** | **−34.5%** again |

Two findings worth stating plainly.

**Embedding is not the cost driver.** It is a tenth of a cent — 0.14% of
ingestion. Contextualisation costs 690× more. Optimising the embedding step is
optimising the wrong thing, which is exactly where a system without a cost
ledger would send you.

**Rerank and generate are 79.6% of per-query cost.** So moving translation and
routing to Gemini Flash is worth ~34% off the warm price — real, but not the
order-of-magnitude win "switch to a cheaper model" usually implies. Prompt
caching is the bigger single lever, and the out-of-scope short-circuit is
bigger still: a refused question costs $0.0135, **10% of a full query**.

Caveat, stated because it materially moves the number: every Anthropic call
runs adaptive thinking, and thinking tokens bill as output. The ingestion
estimate applies a **3× output multiplier that is an assumption, not a
measurement** (`costs.yaml → estimation.thinking_output_multiplier`), and the
estimator emits a warning saying so. The first real ingestion replaces it —
compare `est_cost_usd` against `actual_cost_usd` in
`v_ingestion_estimate_accuracy`. The per-query table above uses observed prompt
sizes but assumed output sizes, so treat it as a model, not a measurement.

### 4. The failure mode being engineered against is confident wrongness

A RAG system over a travel policy doesn't fail loudly. It states a plausible
per-diem figure that is wrong, and someone books against it.

- **Structure-aware chunking.** Split the per-diem table at an arbitrary
  600-token boundary and one chunk holds `13 | 18 | 29 | 10` with no headers and
  no city tier. Still retrievable, still confidently wrong. Tables are atomic;
  oversized ones split by row with the header repeated.
- **Hybrid retrieval.** Policy questions turn on exact tokens embeddings blur —
  "Band 4" vs "Band 5", "Tier 2" vs "Tier 3". Vector search alone confuses them.
- **Refusal is structural, not a model judgement.** If nothing clears
  `min_rerank_relevance`, the graph routes to the refusal node. Generation is
  never asked whether it has enough evidence.
- **Clarification instead of guessing.** Cabin class depends on grade band *and*
  flight duration. Asked without both, the system asks rather than picking a
  likely case.
- **Citation checking is a regex, not a second model.** Verifying every factual
  sentence cites a real retrieved chunk is free and deterministic. An LLM
  groundedness judge would roughly double per-query cost to answer a question
  arithmetic already answers.
- **The semantic cache threshold is 0.97, deliberately high.** "Per diem in
  Tokyo" and "per diem in Bengaluru" are near-identical sentences with different
  answers ($110 vs $70). At a typical 0.92 they collide and the cache serves the
  wrong figure. A cache that is wrong is worse than no cache.

---

## Architecture

```
  Browser (React + Vite)
      │  POST /api/ask  →  SSE: stage · token · citations · cost · done
      ▼
  FastAPI  ── thin: validate, set tenant GUC, drive graph, serialise
      │
      ▼
  LangGraph
      guard → cache_lookup ─hit────────────────────────────────────→ END
                   │ miss
                 route ─chitchat/out_of_scope──→ refuse ───────────→ END
                   │   └─underspecified────────→ clarify ──────────→ END
                 translate → retrieve → rerank → generate → verify → END
                   │            │         │          │         │
                   │            │         │          │         └ regex citation check
                   │            │         │          └ Opus 5, streaming, cited
                   │            │         └ Opus 5 listwise, cached prefix
                   │            └ SQL: hybrid → RRF → MMR → expand
                   └ Opus 5 (→ Gemini Flash in the target state)
      │
      ▼
  PostgreSQL 16 + pgvector 0.8.6
      22 tables · 8 views · 11 functions · 14 RLS policies
```

The graph shape is a cost decision as much as a correctness one. Every early
exit skips the two Opus 5 calls that dominate spend, and it skips them exactly
where they would add least: chitchat, out-of-scope questions, and questions
where retrieval found nothing worth reasoning over.

---

## Layout

```
config/                     Versioned YAML — the only place behaviour is configured
  models.yaml               Model catalogue + role→model assignment (the Gemini switch)
  costs.yaml                Price book, effective-dated; budgets
  retrieval.yaml            Index params, RRF weights, MMR, thresholds, refusal floors
  chunking.yaml             Parsing, structure-aware chunking, metadata extraction
  prompts/*.yaml            Five prompts, each versioned and hashed independently

migrations/                 Plain numbered SQL, applied in order
  001_extensions_and_tenancy.sql   pgvector, app role, RLS, bootstrap principal
  002_config_provenance.sql        Config/prompt/model/price registries (append-only)
  003_documents_and_chunks.sql     Content-addressed documents, chunks, embeddings
  004_retrieval_functions.sql      Vector, lexical, RRF, MMR, context expansion
  005_runs_and_cost.sql            Runs, steps, cost ledger, budget trigger
  006_views_and_cache.sql          Provenance + cost views, two-tier answer cache
  007_run_cost_breakdown.sql       Per-run cost view (added when a test caught an
                                   aggregation living in a route handler)

backend/app/
  config/registry.py        Canonicalise → hash → register → pin a config bundle
  providers/                Anthropic + Gemini behind one interface, one Usage shape
  ingest/chunker.py         Structure-aware chunking; tables stay whole
  ingest/estimator.py       Price an ingestion before spending
  ingest/pipeline.py        Estimate → reuse → contextualise → embed → store
  graph/                    State, nodes, resolver, per-node instrumentation
  main.py                   9 endpoints, no business logic

web/src/
  lib/useAskStream.ts       SSE over fetch + ReadableStream (EventSource can't POST)
  components/               Answer, Sources, CostMeter, Provenance, Documents

corpus/
  travel-policy-v1.md       14-section synthetic policy, ~3,500 words
  eval-set.yaml             30 questions across 9 categories, with expected citations

scripts/
  migrate.sh                Migration runner: tracks applied files by sha256 in
                            schema_migrations, so re-running is a no-op and
                            editing an applied migration is an error, not a
                            silent divergence. --reset rebuilds, --status reports.
  seed.py                   Default tenant + config registration
  ingest.py                 CLI ingestion (--estimate-only spends nothing)
  verify_rls.sql            Tenant isolation assertions
  verify_retrieval.sql      Ranking assertions on synthetic vectors
  verify_cost.sql           Cost arithmetic and budget assertions
```

---

## Testing

```bash
make test
```

- **33 Python tests** — chunker behaviour, cost arithmetic, and an AST pass over
  the API layer that fails if ranking, cost maths, tenant filtering or an
  aggregation leaks back into a route handler. That last one is not decoration:
  it caught an inline `GROUP BY` in the `/ask` stream, which became
  `v_run_cost_breakdown` in migration 007.
- **`verify_rls.sql`** — isolation, cross-tenant write rejection, tenant switching,
  and that a missing tenant GUC fails loudly rather than returning an empty set.
- **`verify_retrieval.sql`** — 10 assertions on synthetic unit vectors, so the
  expected ordering is computable by hand: similarity sign, threshold filtering,
  metadata filters, RRF keeping single-arm hits, both-arms outranking one, MMR
  suppressing a 0.99-similar duplicate, and sibling stitching.
- **`verify_cost.sql`** — the generated column, cache-rate billing, estimates
  exempt from budgets, the budget trigger rejecting overspend, rollup, and
  tiered price resolution.

The retrieval and cost tests use synthetic inputs precisely so they assert
*correct ranking* and *correct arithmetic*, not merely that the functions run.

---

## Things worth knowing

**Superusers bypass RLS.** `FORCE ROW LEVEL SECURITY` does not apply to them,
and on a default Homebrew install the desktop user *is* a superuser. The app
connects as `rag_app`, which is `NOSUPERUSER NOBYPASSRLS`. `verify_rls.sql`
`SET ROLE`s to it — run as the owner, every assertion in it fails, which is the
trap it exists to catch.

**`NULL` defeats unique constraints.** `UNIQUE (provider, model_id, kind,
dimensions)` permitted unlimited duplicate rows for chat models, because
`dimensions` is NULL for all of them and `NULL != NULL`. Three registrations
produced three `claude-opus-5` rows, which would have split one model's cost
across three ids. Fixed with PG15+'s `UNIQUE NULLS NOT DISTINCT`.

**pgvector's HNSW index rejects `vector` columns above 2000 dimensions.**
`gemini-embedding-001` emits 3072 natively. Verified on this machine:

```
vector(3072)  + hnsw → ERROR: column cannot have more than 2000 dimensions
halfvec(3072) + hnsw → OK
vector(1536)  + hnsw → OK
```

We store `vector(1536)` via Matryoshka truncation, re-normalising afterwards —
truncating an MRL embedding leaves it non-unit-length, and cosine distance on a
non-unit vector is quietly not the cosine of the angle. `halfvec(3072)` is the
documented alternative.

**Gemini embeddings are asymmetric.** Documents use `RETRIEVAL_DOCUMENT`,
queries use `RETRIEVAL_QUERY`. Using one for both produces valid-looking vectors
and measurably worse retrieval, with no error anywhere.

**A cache that never fires still looks implemented.** The semantic tier probes
before retrieval runs, so it had no query vector and `fn_cache_lookup`'s
semantic branch was unreachable — exact-hash hits worked, semantic ones silently
never did. The fix embeds the question in the cache node and `retrieve` reuses
that vector for the original query, so the tier works at zero extra embedding
calls.

**HyDE was considered and rejected.** It embeds a hypothetical answer, which on
a corpus this numbers-dense means inventing plausible policy text full of
invented figures and pulling retrieval toward passages resembling the
hallucination. The failure mode it introduces is the one this system works
hardest to eliminate. Multi-query expansion and step-back prompting are used
instead.

---

## Degraded mode

With no API keys the system still runs: migrations, config registration and
provenance, document parsing, structure-aware chunking, and full ingestion cost
estimation. Only live embedding and live answering need credentials. The UI
shows a banner rather than failing opaquely.

Set them in `.env`:

```
ANTHROPIC_API_KEY=sk-ant-...
GEMINI_API_KEY=...
```

---

## Scaling past one user and one document

The schema is multi-tenant throughout already — `tenant_id` and an RLS policy on
all 14 tenant tables, verified isolating. Adding a second tenant is an INSERT.

What actually changes:

- `backend/app/api/deps.py::current_principal` is the only function that needs
  rewriting for real authentication. Everything downstream already takes
  `(tenant_id, user_id)` and every query already runs under RLS.
- Per-user and per-tenant budgets already exist in `tenant_budgets` and are
  enforced by a trigger.
- Multiple documents need the router's logical routing extended to pick a
  corpus; `chunks.metadata` and the routing prompt's topic enum are the seam.
- Contextualisation should move to the Batch API (half price) once ingestion is
  more than one document at a time.
