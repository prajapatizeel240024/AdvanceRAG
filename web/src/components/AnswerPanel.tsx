import type { AskState } from '../lib/useAskStream'

/**
 * The answer surface.
 *
 * Two things here matter more than they look. First, the stage line: a RAG
 * answer takes seconds, and naming what is happening ("ranking passages with
 * Opus 5") is the difference between feeling fast and feeling broken. Second,
 * `[n]` markers are rendered as clickable chips rather than left as literal
 * text, because a citation you cannot open is decoration.
 */

function renderWithCitations(text: string) {
  const out: React.ReactNode[] = []
  const re = /\[(\d+)\]/g
  let last = 0
  let m: RegExpExecArray | null
  let key = 0

  while ((m = re.exec(text)) !== null) {
    if (m.index > last) out.push(text.slice(last, m.index))
    const n = m[1]
    out.push(
      <button
        key={`c${key++}`}
        className="cite"
        onClick={() => {
          const el = document.getElementById(`source-${n}`)
          if (el) {
            el.scrollIntoView({ behavior: 'smooth', block: 'center' })
            el.classList.remove('cite-target')
            // Force a reflow so the animation restarts when the same citation
            // is clicked twice in a row.
            void el.offsetWidth
            el.classList.add('cite-target')
          }
        }}
        title={`Jump to source ${n}`}
      >
        {n}
      </button>,
    )
    last = m.index + m[0].length
  }
  if (last < text.length) out.push(text.slice(last))
  return out
}

const OUTCOME_LABEL: Record<string, { text: string; tone: string }> = {
  answered: { text: 'Answered', tone: 'var(--ok)' },
  refused_out_of_scope: { text: 'Not covered by this policy', tone: 'var(--warn)' },
  refused_no_evidence: { text: 'No supporting passage found', tone: 'var(--warn)' },
  needs_clarification: { text: 'Needs more detail', tone: 'var(--accent)' },
}

export function AnswerPanel({ ask }: { ask: AskState }) {
  const hasContent = ask.answer || ask.running || ask.error

  if (!hasContent) {
    return (
      <div
        className="rounded-xl border p-8 text-center text-sm"
        style={{ borderColor: 'var(--border)', background: 'var(--surface)', color: 'var(--text-dim)' }}
      >
        Ask a question to get a grounded answer with citations back to the policy text.
      </div>
    )
  }

  const outcome = ask.outcome ? OUTCOME_LABEL[ask.outcome] : null

  return (
    <div
      className="rounded-xl border"
      style={{ borderColor: 'var(--border)', background: 'var(--surface)' }}
    >
      {(ask.running || ask.stageDetail) && (
        <div
          className="flex items-center gap-2 border-b px-4 py-2 text-xs"
          style={{ borderColor: 'var(--border)', color: 'var(--text-dim)' }}
        >
          {ask.running && (
            <span
              className="inline-block h-1.5 w-1.5 animate-pulse rounded-full"
              style={{ background: 'var(--accent)' }}
            />
          )}
          <span>{ask.stageDetail || 'Working'}</span>
          {ask.cacheHit !== 'none' && (
            <span
              className="ml-auto rounded px-1.5 py-0.5 text-[10px] font-medium"
              style={{ background: 'color-mix(in srgb, var(--ok) 16%, transparent)', color: 'var(--ok)' }}
            >
              {ask.cacheHit} cache hit — no model call
            </span>
          )}
        </div>
      )}

      <div className="px-5 py-4">
        {ask.error ? (
          <div className="text-sm" style={{ color: 'var(--danger)' }}>
            {ask.error}
          </div>
        ) : (
          <div
            className={`whitespace-pre-wrap text-[15px] leading-relaxed ${ask.running ? 'caret' : ''}`}
          >
            {renderWithCitations(ask.answer)}
          </div>
        )}
      </div>

      {(outcome || ask.degraded) && (
        <div
          className="flex flex-wrap items-center gap-3 border-t px-5 py-2 text-[11px]"
          style={{ borderColor: 'var(--border)' }}
        >
          {outcome && (
            <span style={{ color: outcome.tone }} className="font-medium">
              {outcome.text}
            </span>
          )}
          {/* Degradation is surfaced, never hidden. An answer produced without
              reranking, or with weak citation coverage, is still useful — but
              the reader is entitled to know. */}
          {ask.degraded && ask.degradedReason && (
            <span style={{ color: 'var(--warn)' }}>⚠ {ask.degradedReason}</span>
          )}
          {ask.runId && (
            <span className="ml-auto font-mono" style={{ color: 'var(--text-dim)' }}>
              run {ask.runId.slice(0, 8)}
            </span>
          )}
        </div>
      )}
    </div>
  )
}
