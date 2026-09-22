import type { Citation } from '../lib/types'

/**
 * The passages the answer was built from.
 *
 * Showing the exact source text is what makes the answer checkable. A user who
 * doubts a per-diem figure can read the table the model read, in the policy's
 * own words, without the breadcrumb or contextual summary the retrieval layer
 * added — those are our scaffolding, not the policy's text.
 *
 * The reranker's score and one-clause reason are shown too, because "why was
 * this passage chosen" is the first question when an answer looks wrong.
 */
export function SourcesPanel({ citations }: { citations: Citation[] }) {
  if (!citations.length) {
    return (
      <div
        className="rounded-xl border p-5 text-center text-xs"
        style={{ borderColor: 'var(--border)', background: 'var(--surface)', color: 'var(--text-dim)' }}
      >
        Sources appear here once an answer is generated. Click a citation marker in
        the answer to jump to the passage it came from.
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-2">
      {citations.map((c) => (
        <div
          key={c.chunk_id}
          id={`source-${c.marker}`}
          className="rounded-xl border p-3"
          style={{ borderColor: 'var(--border)', background: 'var(--surface)' }}
        >
          <div className="mb-2 flex items-start gap-2">
            <span
              className="mt-0.5 grid h-5 min-w-5 shrink-0 place-items-center rounded px-1 text-[11px] font-bold"
              style={{ background: 'var(--accent-soft)', color: 'var(--accent)' }}
            >
              {c.marker}
            </span>
            <div className="min-w-0 flex-1">
              <div className="truncate text-[11px] font-medium" title={c.breadcrumb}>
                {c.breadcrumb.split(' > ').slice(1).join(' › ') || c.breadcrumb}
              </div>
              {c.rerank_score != null && (
                <div className="mt-0.5 flex items-center gap-1.5">
                  <div
                    className="h-1 w-16 overflow-hidden rounded-full"
                    style={{ background: 'var(--surface-2)' }}
                  >
                    <div
                      className="h-full rounded-full"
                      style={{
                        width: `${Math.round(c.rerank_score * 100)}%`,
                        background: 'var(--accent)',
                      }}
                    />
                  </div>
                  <span className="text-[10px]" style={{ color: 'var(--text-dim)' }}>
                    {c.rerank_score.toFixed(2)} relevance
                  </span>
                </div>
              )}
            </div>
          </div>

          {c.rerank_reason && (
            <div
              className="mb-2 text-[11px] italic"
              style={{ color: 'var(--text-dim)' }}
            >
              {c.rerank_reason}
            </div>
          )}

          <pre
            className="max-h-56 overflow-auto rounded-lg p-2.5 font-mono text-[11px] leading-relaxed whitespace-pre-wrap"
            style={{ background: 'var(--surface-2)', color: 'var(--text)' }}
          >
            {c.content}
          </pre>
        </div>
      ))}
    </div>
  )
}
