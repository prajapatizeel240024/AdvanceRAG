import type { CostEvent } from '../lib/types'

/**
 * Per-query cost, broken down by graph node.
 *
 * Costs are shown in millicents because a query lands in the 10^-3 to 10^-2
 * dollar range and rounding to cents displays "$0.00", which reads as free and
 * makes the whole cost story invisible. The exact numeric stays in the ledger;
 * this is a display choice only.
 *
 * The cache column is the interesting one: it shows how much of each call's
 * input was served from the prompt cache at 10% of the input rate. On the
 * rerank node that is the difference between $0.0700 and $0.0183.
 */
export function CostMeter({
  cost,
  cacheHit,
}: {
  cost: CostEvent | null
  cacheHit: 'none' | 'exact' | 'semantic'
}) {
  if (!cost) return null

  const maxCost = Math.max(...cost.breakdown.map((b) => Number(b.cost_usd)), 1e-9)

  return (
    <div
      className="rounded-xl border"
      style={{ borderColor: 'var(--border)', background: 'var(--surface)' }}
    >
      <div
        className="flex items-baseline gap-3 border-b px-4 py-2.5"
        style={{ borderColor: 'var(--border)' }}
      >
        <span className="text-xs font-medium" style={{ color: 'var(--text-dim)' }}>
          This query
        </span>
        <span className="font-mono text-lg font-semibold tabular-nums">
          {cost.total_millicents.toFixed(2)}
          <span className="ml-1 text-[11px] font-normal" style={{ color: 'var(--text-dim)' }}>
            millicents
          </span>
        </span>
        <span className="font-mono text-[11px]" style={{ color: 'var(--text-dim)' }}>
          ${Number(cost.total_usd).toFixed(6)}
        </span>
        <span className="ml-auto font-mono text-[11px]" style={{ color: 'var(--text-dim)' }}>
          {cost.latency_ms} ms
        </span>
      </div>

      {cacheHit !== 'none' ? (
        <div className="px-4 py-3 text-xs" style={{ color: 'var(--ok)' }}>
          Served from the {cacheHit} cache — no model calls were made.
        </div>
      ) : (
        <div className="px-4 py-2">
          <table className="w-full text-[11px]">
            <thead>
              <tr style={{ color: 'var(--text-dim)' }} className="text-left">
                <th className="py-1 font-medium">Node</th>
                <th className="py-1 font-medium">Model</th>
                <th className="py-1 text-right font-medium">In</th>
                <th className="py-1 text-right font-medium">Cached</th>
                <th className="py-1 text-right font-medium">Out</th>
                <th className="py-1 text-right font-medium">Cost</th>
              </tr>
            </thead>
            <tbody className="font-mono tabular-nums">
              {cost.breakdown.map((b, i) => (
                <tr key={i} className="border-t" style={{ borderColor: 'var(--border)' }}>
                  <td className="py-1.5 font-sans">{b.node}</td>
                  <td className="py-1.5" style={{ color: 'var(--text-dim)' }}>
                    {b.model_id.replace('claude-', '').replace('gemini-', '')}
                  </td>
                  <td className="py-1.5 text-right">{Number(b.input_tokens).toLocaleString()}</td>
                  <td
                    className="py-1.5 text-right"
                    style={{ color: Number(b.cache_read_tokens) > 0 ? 'var(--ok)' : 'var(--text-dim)' }}
                  >
                    {Number(b.cache_read_tokens) > 0
                      ? Number(b.cache_read_tokens).toLocaleString()
                      : '—'}
                  </td>
                  <td className="py-1.5 text-right">{Number(b.output_tokens).toLocaleString()}</td>
                  <td className="py-1.5 text-right">
                    <div className="flex items-center justify-end gap-1.5">
                      <div
                        className="h-1 rounded-full"
                        style={{
                          width: `${Math.max(2, (Number(b.cost_usd) / maxCost) * 36)}px`,
                          background: 'var(--accent)',
                        }}
                      />
                      <span>{(Number(b.cost_usd) * 100000).toFixed(2)}</span>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
