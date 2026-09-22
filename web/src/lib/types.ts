// Wire types for the /api/ask SSE stream.
//
// This discriminated union is the contract between the FastAPI stream and the
// UI. It is written by hand rather than generated because the SSE event shapes
// are not part of the OpenAPI schema -- FastAPI describes the *response* as a
// stream, not the events inside it -- so this file and `_sse(...)` in
// backend/app/main.py are the two halves that must agree.

export type StageName =
  | 'started'
  | 'cache_hit'
  | 'routing'
  | 'routed'
  | 'translating'
  | 'translated'
  | 'retrieving'
  | 'retrieved'
  | 'reranking'
  | 'reranked'
  | 'generating'

export interface StageEvent {
  type: 'stage'
  stage: StageName
  run_id?: string
  config_bundle?: string
  intent?: string
  filter?: Record<string, unknown>
  queries?: string[]
  count?: number
  kept?: number
  kind?: string
}

export interface TokenEvent {
  type: 'token'
  text: string
}

export interface Citation {
  marker: number
  chunk_id: string
  breadcrumb: string
  content: string
  rerank_score: number | null
  rerank_reason?: string | null
}

export interface CitationsEvent {
  type: 'citations'
  citations: Citation[]
}

export interface CostLine {
  node: string
  model_id: string
  input_tokens: number
  output_tokens: number
  cache_read_tokens: number
  cost_usd: number
}

export interface CostEvent {
  type: 'cost'
  total_usd: number
  total_millicents: number
  latency_ms: number
  breakdown: CostLine[]
}

export type Outcome =
  | 'answered'
  | 'refused_out_of_scope'
  | 'refused_no_evidence'
  | 'needs_clarification'

export interface DoneEvent {
  type: 'done'
  run_id: string
  outcome: Outcome
  degraded: boolean
  degraded_reason: string | null
  cache_hit: 'none' | 'exact' | 'semantic'
}

export interface ErrorEvent {
  type: 'error'
  message: string
  retryable?: boolean
}

export type AskEvent =
  | StageEvent
  | TokenEvent
  | CitationsEvent
  | CostEvent
  | DoneEvent
  | ErrorEvent

// ---- REST types ----------------------------------------------------------

export interface Health {
  ok: boolean
  degraded: boolean
  missing_keys: string[]
  config_bundle: string
}

export interface ConfigFile {
  name: string
  version: string
  hash: string
}

export interface RoleAssignment {
  role: string
  provider: string
  model_id: string
  effort: string | null
}

export interface ConfigInfo {
  bundle_id: string
  bundle_hash: string
  files: ConfigFile[]
  roles: RoleAssignment[]
}

export interface DocumentRow {
  id: string
  slug: string
  title: string
  version_id: string
  version_label: string
  status: string
  byte_size: number
  chunks: number
  embedded: number
  est_cost_usd: number | null
  actual_cost_usd: number | null
  reused_chunks: number | null
}

export interface EstimateLineItem {
  label: string
  model_id: string
  tokens: number
  rate_per_1m: number
  cost_usd: number
  note: string
}

export interface EstimateResponse {
  ingestion_run_id: string
  document_version_id: string
  chunk_count: number
  new_chunk_count: number
  reused_chunk_count: number
  embedding_tokens: number
  total_cost_usd: number
  reuse_saving_usd: number
  token_confidence: 'exact' | 'estimated'
  warnings: string[]
  line_items: EstimateLineItem[]
}

export interface ProvenanceStep {
  step_index: number
  node: string
  step_status: string
  step_latency_ms: number | null
  effort: string | null
  prompt_role: string | null
  prompt_version: string | null
  prompt_hash: string | null
  prompt_template: string | null
  provider: string | null
  model_id: string | null
  step_cost_usd: number
  step_input_tokens: number
  step_output_tokens: number
  step_cache_read_tokens: number
}

export interface Provenance {
  run_id: string
  config_bundle_id: string
  bundle_hash: string
  question: string
  outcome: string
  total_cost_usd: number
  steps: ProvenanceStep[]
  config_files: { config_file: string; semver: string; content_hash: string; content: string }[]
}
