import { useEffect, useState } from 'react'
import { useAskStream } from './lib/useAskStream'
import type { ConfigInfo, DocumentRow, Health } from './lib/types'
import { AnswerPanel } from './components/AnswerPanel'
import { SourcesPanel } from './components/SourcesPanel'
import { CostMeter } from './components/CostMeter'
import { ProvenancePanel } from './components/ProvenancePanel'
import { DocumentPanel } from './components/DocumentPanel'

const SAMPLES = [
  'What is the per diem in Tokyo?',
  "I'm a Senior Engineer flying London to Singapore. What cabin can I book?",
  'Can I book a hotel above the nightly cap for a conference?',
  'Does the company pay for my visa application?',
]

type Tab = 'sources' | 'provenance' | 'documents'

export default function App() {
  const ask = useAskStream()
  const [question, setQuestion] = useState('')
  const [health, setHealth] = useState<Health | null>(null)
  const [config, setConfig] = useState<ConfigInfo | null>(null)
  const [docs, setDocs] = useState<DocumentRow[]>([])
  const [tab, setTab] = useState<Tab>('sources')

  const [loadError, setLoadError] = useState<string | null>(null)

  useEffect(() => {
    fetch('/api/health').then((r) => r.json()).then(setHealth).catch(() => {})
    fetch('/api/config').then((r) => r.json()).then(setConfig).catch(() => {})
    refreshDocs()
  }, [])

  function refreshDocs() {
    fetch('/api/documents')
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`)
        return r.json()
      })
      .then((d) => {
        setDocs(d)
        setLoadError(null)
      })
      // Swallowing this made a backend that was down look identical to a
      // database with no documents in it, which sent the reader looking for
      // the wrong problem entirely.
      .catch((e) => setLoadError(`Could not reach the API (${e.message}).`))
  }

  function submit(q: string) {
    const text = q.trim()
    if (!text || ask.running) return
    setQuestion(text)
    setTab('sources')
    ask.ask(text)
  }

  const hasDoc = docs.some((d) => d.status === 'ready')

  return (
    <div className="min-h-full" style={{ background: 'var(--bg)' }}>
      <header
        className="sticky top-0 z-20 border-b backdrop-blur"
        style={{ borderColor: 'var(--border)', background: 'color-mix(in srgb, var(--bg) 88%, transparent)' }}
      >
        <div className="mx-auto flex max-w-[1400px] flex-wrap items-center gap-3 px-4 py-3">
          <div className="flex items-center gap-2">
            <div
              className="grid h-8 w-8 place-items-center rounded-lg text-sm font-bold"
              style={{ background: 'var(--accent-soft)', color: 'var(--accent)' }}
            >
              TP
            </div>
            <div className="leading-tight">
              <div className="text-sm font-semibold">Travel Policy Assistant</div>
              <div className="text-[11px]" style={{ color: 'var(--text-dim)' }}>
                Grounded answers with citations
              </div>
            </div>
          </div>

          <div className="ml-auto flex flex-wrap items-center gap-2">
            {config && (
              <span
                className="rounded-md border px-2 py-1 font-mono text-[11px]"
                style={{ borderColor: 'var(--border)', color: 'var(--text-dim)' }}
                title={`Config bundle ${config.bundle_hash}`}
              >
                cfg {config.bundle_hash.slice(0, 8)}
              </span>
            )}
            {health?.degraded && (
              <span
                className="rounded-md px-2 py-1 text-[11px] font-medium"
                style={{ background: 'color-mix(in srgb, var(--warn) 16%, transparent)', color: 'var(--warn)' }}
                title={`Missing: ${health.missing_keys.join(', ')}`}
              >
                Degraded — no API keys
              </span>
            )}
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-[1400px] px-4 py-6">
        {/* Ask box */}
        <form
          onSubmit={(e) => {
            e.preventDefault()
            submit(question)
          }}
          className="rounded-xl border p-3"
          style={{ borderColor: 'var(--border)', background: 'var(--surface)' }}
        >
          <textarea
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault()
                submit(question)
              }
            }}
            rows={2}
            placeholder="Ask about booking, cabin class, per diems, hotel caps, expenses…"
            className="w-full resize-none bg-transparent px-2 py-1 text-[15px] outline-none"
            style={{ color: 'var(--text)' }}
          />
          <div className="flex flex-wrap items-center gap-2 pt-2">
            <div className="flex flex-wrap gap-1.5">
              {SAMPLES.map((s) => (
                <button
                  key={s}
                  type="button"
                  onClick={() => submit(s)}
                  className="rounded-md border px-2 py-1 text-[11px] transition-colors hover:border-current"
                  style={{ borderColor: 'var(--border)', color: 'var(--text-dim)' }}
                >
                  {s.length > 44 ? s.slice(0, 42) + '…' : s}
                </button>
              ))}
            </div>
            <div className="ml-auto flex items-center gap-2">
              {ask.running ? (
                <button
                  type="button"
                  onClick={ask.cancel}
                  className="rounded-lg border px-3 py-1.5 text-sm"
                  style={{ borderColor: 'var(--border)' }}
                >
                  Stop
                </button>
              ) : (
                <button
                  type="submit"
                  disabled={!question.trim()}
                  className="rounded-lg px-4 py-1.5 text-sm font-medium text-white disabled:opacity-40"
                  style={{ background: 'var(--accent)' }}
                >
                  Ask
                </button>
              )}
            </div>
          </div>
        </form>

        {/* Gate on "no READY document", not "no rows". A document that has been
            estimated but not ingested has a row, zero chunks, and cannot answer
            anything — previously that state showed no guidance at all, and the
            first sign of trouble was an error after clicking Ask. */}
        {(loadError || !hasDoc) && (
          <div
            className="mt-4 rounded-xl border p-4 text-sm"
            style={{
              borderColor: loadError ? 'var(--danger)' : 'var(--border)',
              background: 'var(--surface)',
              color: 'var(--text-dim)',
            }}
          >
            {loadError ? (
              <span style={{ color: 'var(--danger)' }}>
                {loadError} Is the backend running on :8000?
              </span>
            ) : docs.length === 0 ? (
              <>
                No document yet. Open{' '}
                <button className="underline" onClick={() => setTab('documents')}>
                  Documents
                </button>{' '}
                to estimate the cost, then ingest.
              </>
            ) : (
              <>
                <strong>“{docs[0].title}” is priced but not ingested</strong> — it
                has {docs[0].chunks} chunks and {docs[0].embedded} embeddings, so
                there is nothing to retrieve yet.{' '}
                {health?.degraded ? (
                  <>
                    Ingestion needs an embedding key; {health.missing_keys.join(' and ')}{' '}
                    {health.missing_keys.length > 1 ? 'are' : 'is'} not set.
                  </>
                ) : (
                  <>
                    Open{' '}
                    <button className="underline" onClick={() => setTab('documents')}>
                      Documents
                    </button>{' '}
                    and press Ingest.
                  </>
                )}
              </>
            )}
          </div>
        )}

        <div className="mt-5 grid gap-5 lg:grid-cols-[minmax(0,1fr)_minmax(0,420px)]">
          <div className="flex flex-col gap-4">
            <AnswerPanel ask={ask} />
            <CostMeter cost={ask.cost} cacheHit={ask.cacheHit} />
          </div>

          <div className="flex flex-col gap-3">
            <div
              className="flex gap-1 rounded-lg border p-1 text-xs"
              style={{ borderColor: 'var(--border)', background: 'var(--surface)' }}
            >
              {(
                [
                  ['sources', `Sources${ask.citations.length ? ` (${ask.citations.length})` : ''}`],
                  ['provenance', 'Provenance'],
                  ['documents', 'Documents'],
                ] as [Tab, string][]
              ).map(([id, label]) => (
                <button
                  key={id}
                  onClick={() => setTab(id)}
                  className="flex-1 rounded-md px-2 py-1.5 font-medium transition-colors"
                  style={
                    tab === id
                      ? { background: 'var(--accent-soft)', color: 'var(--accent)' }
                      : { color: 'var(--text-dim)' }
                  }
                >
                  {label}
                </button>
              ))}
            </div>

            {tab === 'sources' && <SourcesPanel citations={ask.citations} />}
            {tab === 'provenance' && <ProvenancePanel runId={ask.runId} config={config} />}
            {tab === 'documents' && <DocumentPanel docs={docs} onChange={refreshDocs} />}
          </div>
        </div>
      </main>
    </div>
  )
}
