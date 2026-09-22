# Configuration

Everything that shapes an answer lives here, in versioned YAML. Nothing in the
codebase names a model, a threshold or a prompt — it asks the registry.

The test for whether a value belongs in this directory rather than in `.env`:
**if changing it should change an answer, it belongs here.** A value in `.env`
leaves no trace in the provenance record; a value here is hashed and recorded
against every run that used it.

## Files

| File | What it controls |
|---|---|
| `models.yaml` | Model catalogue and the role→model assignment. This is the file you edit to move translation and routing onto Gemini. |
| `costs.yaml` | Price book, effective-dated, with per-user and per-tenant budgets. |
| `retrieval.yaml` | Index parameters, RRF weights, MMR, expansion, and the thresholds that make honest refusal reachable. |
| `chunking.yaml` | Parsing, structure-aware chunking, contextual retrieval, metadata extraction. |
| `prompts/*.yaml` | Five prompts. Each is registered and hashed independently of the file that carries it, because prompts change most often and cause the largest behavioural swings. |

## How a version gets recorded

On startup:

1. Each file is parsed and **canonicalised** — rendered to JSON with sorted keys
   and no incidental whitespace. Without this, reordering two keys produces a
   "new" configuration that is semantically identical, and the registry fills
   with noise that hides real changes.
2. The canonical form is hashed with sha256 and upserted into
   `config_file_versions`, keyed by `(name, content_hash)`. An unchanged file
   resolves to its existing row and registers nothing new.
3. The member hashes combine into a **bundle hash**, upserted into
   `config_bundles`.
4. That bundle id is pinned for the process lifetime, and every `query_run` and
   `ingestion_run` stores it.

Both a semver and a hash are kept. The `version:` field is a claim a human
makes, and humans edit files without bumping it — so the hash is the identity
and the semver is the human-readable label.

`config_file_versions`, `config_bundles` and `prompt_versions` are **append-only**,
enforced by a trigger. A registered config version is a historical fact about a
run that already happened; mutating one silently rewrites the provenance of
every answer pointing at it.

## The provenance query

Given any `query_run_id`, this returns everything that produced that answer:

```sql
SELECT step_index, node, model_id, effort,
       prompt_role, prompt_version, left(prompt_hash, 12) AS prompt_hash,
       step_input_tokens, step_cache_read_tokens, step_output_tokens,
       step_cost_usd
FROM v_run_provenance
WHERE query_run_id = '…'
ORDER BY step_index;
```

And the full YAML that was in force, reconstructable even if the working tree
has long since moved on:

```sql
SELECT config_file, semver, left(content_hash, 12), content
FROM v_run_config_files
WHERE query_run_id = '…';
```

Exposed over HTTP as `GET /api/runs/{id}/provenance`, and rendered by the
Provenance tab in the UI.

## Comparing two configurations

Because every run is tagged with its bundle, "did v2 beat v1" is a `GROUP BY`:

```sql
SELECT config_label, runs, answered, refused, mean_cited_ratio,
       mean_cost_usd, mean_latency_ms
FROM v_quality_by_config
ORDER BY runs DESC;
```

## Switching to the Gemini target state

Today every role resolves to `claude-opus-5`. To move query translation and
routing onto Gemini while reranking stays on Opus 5, edit `models.yaml`:

```yaml
version: 2.0.0        # bump — the hash catches drift, the semver tells humans

roles:
  query_translation:
    model: gemini_flash     # was: opus5
  routing:
    model: gemini_flash     # was: opus5
  rerank:
    model: opus5            # unchanged, per the project directive
```

Restart. No code changes, no redeploy of anything but config. A new bundle is
registered, new runs point at it, and every historical run keeps pointing at the
binding that was actually in force when it ran.

Verified: the two bundles resolve to different hashes and different role
assignments, and both `models.yaml` versions sit immutably in
`config_file_versions`.
