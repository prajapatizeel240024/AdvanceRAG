import { useEffect, useState } from 'react'
import type { ConfigInfo, Provenance } from '../lib/types'

/**
 * "Which version produced this answer?"
 *
 * This panel is the project's first requirement made visible. For any answer,
 * it shows the config bundle, and per pipeline step the exact prompt version,
 * prompt hash, model and effort that produced it — read straight from
 * `v_run_provenance`, with the full prompt text expandable.
 *
 * Nothing here is reconstructed from application memory. It is all foreign keys
 * resolved in the database, which is what makes it still true a year from now
 * after the YAML on disk has moved on.
 */
export function ProvenancePanel({
  runId,
  config,
}: {
  runId: string | null
  config: ConfigInfo | null
}) {
  const [prov, setProv] = useState<Provenance | null>(null)
  const [openStep, setOpenStep] = useState<number | null>(null)

  useEffect(() => {
    if (!runId) {
      setProv(null)
      return
    }
    fetch(`/api/runs/${runId}/provenance`)
      .then((r) => (r.ok ? r.json() : null))
      .then(setProv)
      .catch(() => setProv(null))
  }, [runId])

  return (
    <div className="flex flex-col gap-3">
      {/* Current process config — "what are we running right now", as distinct
          from "what produced that answer" below. */}
      {config && (
        <div
          className="rounded-xl border p-3"
          style={{ borderColor: 'var(--border)', background: 'var(--surface)' }}
        >
          <div className="mb-2 text-[11px] font-semibold uppercase tracking-wide" style={{ color: 'var(--text-dim)' }}>
            Active configuration
          </div>
          <div className="mb-2 font-mono text-[11px]" style={{ color: 'var(--text-dim)' }}>
            bundle {config.bundle_hash.slice(0, 24)}…
          </div>
          <table className="w-full text-[11px]">
            <tbody>
              {config.roles.map((r) => (
                <tr key={r.role} className="border-t" style={{ borderColor: 'var(--border)' }}>
                  <td className="py-1 pr-2">{r.role}</td>
                  <td className="py-1 font-mono" style={{ color: 'var(--text-dim)' }}>
                    {r.model_id}
                  </td>
                  <td className="py-1 text-right" style={{ color: 'var(--text-dim)' }}>
                    {r.effort ?? '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="mt-2 flex flex-wrap gap-1">
            {config.files.map((f) => (
              <span
                key={f.name}
                className="rounded border px-1.5 py-0.5 font-mono text-[10px]"
                style={{ borderColor: 'var(--border)', color: 'var(--text-dim)' }}
                title={`${f.name} v${f.version} — ${f.hash}`}
              >
                {f.name} v{f.version}
              </span>
            ))}
          </div>
        </div>
      )}

      {!runId && (
        <div
          className="rounded-xl border p-5 text-center text-xs"
          style={{ borderColor: 'var(--border)', background: 'var(--surface)', color: 'var(--text-dim)' }}
        >
          Ask a question to see the exact prompts, models and config versions that
          produced its answer.
        </div>
      )}

      {prov && (
        <div
          className="rounded-xl border"
          style={{ borderColor: 'var(--border)', background: 'var(--surface)' }}
        >
          <div className="border-b px-3 py-2" style={{ borderColor: 'var(--border)' }}>
            <div className="text-[11px] font-semibold uppercase tracking-wide" style={{ color: 'var(--text-dim)' }}>
              Run provenance
            </div>
            <div className="mt-0.5 font-mono text-[10px]" style={{ color: 'var(--text-dim)' }}>
              {prov.run_id}
            </div>
          </div>

          {prov.steps.map((s) => (
            <div key={s.step_index} className="border-b last:border-b-0" style={{ borderColor: 'var(--border)' }}>
              <button
                onClick={() => setOpenStep(openStep === s.step_index ? null : s.step_index)}
                className="flex w-full items-center gap-2 px-3 py-2 text-left"
              >
                <span
                  className="grid h-5 w-5 place-items-center rounded text-[10px] font-bold"
                  style={{
                    background: s.step_status === 'ok' ? 'var(--accent-soft)' : 'color-mix(in srgb, var(--danger) 18%, transparent)',
                    color: s.step_status === 'ok' ? 'var(--accent)' : 'var(--danger)',
                  }}
                >
                  {s.step_index}
                </span>
                <span className="text-xs font-medium">{s.node}</span>
                <span className="font-mono text-[10px]" style={{ color: 'var(--text-dim)' }}>
                  {s.model_id}
                  {s.effort ? ` · ${s.effort}` : ''}
                </span>
                <span className="ml-auto font-mono text-[10px] tabular-nums" style={{ color: 'var(--text-dim)' }}>
                  {s.prompt_version ? `p${s.prompt_version} ` : ''}
                  {(Number(s.step_cost_usd) * 100000).toFixed(2)}mc
                  {s.step_latency_ms != null ? ` · ${s.step_latency_ms}ms` : ''}
                </span>
              </button>

              {openStep === s.step_index && (
                <div className="px-3 pb-3">
                  <div className="mb-1.5 flex flex-wrap gap-2 font-mono text-[10px]" style={{ color: 'var(--text-dim)' }}>
                    {s.prompt_hash && <span>prompt {s.prompt_hash.slice(0, 12)}</span>}
                    <span>in {Number(s.step_input_tokens).toLocaleString()}</span>
                    {Number(s.step_cache_read_tokens) > 0 && (
                      <span style={{ color: 'var(--ok)' }}>
                        cached {Number(s.step_cache_read_tokens).toLocaleString()}
                      </span>
                    )}
                    <span>out {Number(s.step_output_tokens).toLocaleString()}</span>
                  </div>
                  {s.prompt_template && (
                    <pre
                      className="max-h-64 overflow-auto rounded-lg p-2 font-mono text-[10px] leading-relaxed whitespace-pre-wrap"
                      style={{ background: 'var(--surface-2)' }}
                    >
                      {s.prompt_template}
                    </pre>
                  )}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
