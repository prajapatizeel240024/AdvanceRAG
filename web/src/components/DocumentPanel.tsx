import { useState } from 'react'
import type { DocumentRow, EstimateResponse } from '../lib/types'

/**
 * Ingestion with the price shown first.
 *
 * The estimate is a separate, free call that makes no paid requests, so the
 * cost of ingesting a document is known before the decision to ingest it — the
 * literal requirement. Ingestion is only offered once an estimate has been
 * seen, which is why "Estimate" and "Ingest" are two buttons rather than one.
 *
 * After a run, estimated and actual sit side by side. A forecast nobody checks
 * is a forecast nobody should act on.
 */
export function DocumentPanel({
  docs,
  onChange,
}: {
  docs: DocumentRow[]
  onChange: () => void
}) {
  const [path, setPath] = useState('corpus/travel-policy-v1.md')
  const [est, setEst] = useState<EstimateResponse | null>(null)
  const [busy, setBusy] = useState<'estimate' | 'ingest' | null>(null)
  const [msg, setMsg] = useState<string | null>(null)

  async function estimate() {
    setBusy('estimate')
    setMsg(null)
    try {
      const r = await fetch('/api/documents/estimate', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ path }),
      })
      const j = await r.json()
      if (!r.ok) throw new Error(j.detail ?? 'estimate failed')
      setEst(j)
    } catch (e) {
      setMsg(String(e))
    } finally {
      setBusy(null)
      onChange()
    }
  }

  async function ingest() {
    setBusy('ingest')
    setMsg(null)
    try {
      const r = await fetch('/api/documents/ingest', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ path, approve: true }),
      })
      const j = await r.json()
      if (!r.ok) throw new Error(typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail))
      setMsg(
        `Ingested ${j.chunk_count} chunks (${j.embedded} embedded, ${j.reused} reused) — ` +
          `estimated $${Number(j.estimated_cost_usd).toFixed(6)}, actual $${Number(j.actual_cost_usd).toFixed(6)}`,
      )
    } catch (e) {
      setMsg(String(e))
    } finally {
      setBusy(null)
      onChange()
    }
  }

  return (
    <div className="flex flex-col gap-3">
      <div
        className="rounded-xl border p-3"
        style={{ borderColor: 'var(--border)', background: 'var(--surface)' }}
      >
        <div className="mb-2 text-[11px] font-semibold uppercase tracking-wide" style={{ color: 'var(--text-dim)' }}>
          Ingest a document
        </div>
        <input
          value={path}
          onChange={(e) => setPath(e.target.value)}
          className="w-full rounded-lg border px-2 py-1.5 font-mono text-[11px]"
          style={{ borderColor: 'var(--border)', background: 'var(--surface-2)', color: 'var(--text)' }}
        />
        <div className="mt-2 flex gap-2">
          <button
            onClick={estimate}
            disabled={busy !== null}
            className="rounded-lg border px-3 py-1.5 text-xs disabled:opacity-40"
            style={{ borderColor: 'var(--border)' }}
          >
            {busy === 'estimate' ? 'Estimating…' : 'Estimate cost'}
          </button>
          <button
            onClick={ingest}
            disabled={busy !== null || !est}
            title={!est ? 'Estimate first' : undefined}
            className="rounded-lg px-3 py-1.5 text-xs font-medium text-white disabled:opacity-40"
            style={{ background: 'var(--accent)' }}
          >
            {busy === 'ingest' ? 'Ingesting…' : 'Ingest'}
          </button>
        </div>

        {est && (
          <div
            className="mt-3 rounded-lg p-2.5 text-[11px]"
            style={{ background: 'var(--surface-2)' }}
          >
            <div className="mb-1.5 flex items-baseline gap-2">
              <span className="font-mono text-base font-semibold tabular-nums">
                ${Number(est.total_cost_usd).toFixed(6)}
              </span>
              <span style={{ color: 'var(--text-dim)' }}>
                {est.chunk_count} chunks
                {est.reused_chunk_count > 0 && ` · ${est.reused_chunk_count} reused free`}
              </span>
              {est.token_confidence === 'estimated' && (
                <span className="ml-auto" style={{ color: 'var(--warn)' }}>
                  approximate
                </span>
              )}
            </div>
            <table className="w-full">
              <tbody className="font-mono tabular-nums">
                {est.line_items.map((li, i) => (
                  <tr key={i}>
                    <td className="py-0.5 font-sans">{li.label}</td>
                    <td className="py-0.5 text-right" style={{ color: 'var(--text-dim)' }}>
                      {li.tokens.toLocaleString()} tok
                    </td>
                    <td className="py-0.5 pl-2 text-right">${li.cost_usd.toFixed(6)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {est.reuse_saving_usd > 0 && (
              <div className="mt-1" style={{ color: 'var(--ok)' }}>
                Content-hash reuse saves ${est.reuse_saving_usd.toFixed(6)} on this run.
              </div>
            )}
            {est.warnings.map((w, i) => (
              <div key={i} className="mt-1" style={{ color: 'var(--warn)' }}>
                ⚠ {w}
              </div>
            ))}
          </div>
        )}

        {msg && (
          <div className="mt-2 text-[11px]" style={{ color: 'var(--text-dim)' }}>
            {msg}
          </div>
        )}
      </div>

      {docs.map((d) => (
        <div
          key={d.version_id}
          className="rounded-xl border p-3"
          style={{ borderColor: 'var(--border)', background: 'var(--surface)' }}
        >
          <div className="flex items-center gap-2">
            <span className="text-xs font-medium">{d.title}</span>
            <span className="font-mono text-[10px]" style={{ color: 'var(--text-dim)' }}>
              v{d.version_label}
            </span>
            <span
              className="ml-auto rounded px-1.5 py-0.5 text-[10px] font-medium"
              style={
                d.status === 'ready'
                  ? { background: 'color-mix(in srgb, var(--ok) 16%, transparent)', color: 'var(--ok)' }
                  : { background: 'var(--surface-2)', color: 'var(--text-dim)' }
              }
            >
              {d.status}
            </span>
          </div>
          <div className="mt-1.5 flex flex-wrap gap-x-4 gap-y-1 font-mono text-[10px]" style={{ color: 'var(--text-dim)' }}>
            <span>{d.chunks} chunks</span>
            <span>{d.embedded} embedded</span>
            {d.reused_chunks ? <span style={{ color: 'var(--ok)' }}>{d.reused_chunks} reused</span> : null}
            {d.est_cost_usd != null && <span>est ${Number(d.est_cost_usd).toFixed(6)}</span>}
            {d.actual_cost_usd != null && <span>actual ${Number(d.actual_cost_usd).toFixed(6)}</span>}
          </div>
        </div>
      ))}
    </div>
  )
}
