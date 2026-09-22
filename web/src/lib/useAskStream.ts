import { useCallback, useRef, useState } from 'react'
import type {
  AskEvent,
  Citation,
  CostEvent,
  Outcome,
  StageName,
} from './types'

/**
 * Drives the /api/ask SSE stream.
 *
 * Native `EventSource` cannot issue a POST and cannot set headers, and the
 * question has to go in a request body. So this uses fetch with a
 * ReadableStream reader and parses the SSE framing by hand -- which is a dozen
 * lines and avoids contorting the API into a GET with the question in the
 * query string.
 *
 * SSE framing: events are separated by a blank line; within an event, lines are
 * `event: <name>` and `data: <json>`. Chunks arrive on arbitrary byte
 * boundaries, so a partial event has to be carried over to the next read --
 * getting that wrong produces JSON parse errors under load and works fine in
 * testing, which is the worst combination.
 */

export interface AskState {
  running: boolean
  stage: StageName | null
  stageDetail: string
  answer: string
  citations: Citation[]
  cost: CostEvent | null
  runId: string | null
  outcome: Outcome | null
  degraded: boolean
  degradedReason: string | null
  cacheHit: 'none' | 'exact' | 'semantic'
  error: string | null
}

const EMPTY: AskState = {
  running: false,
  stage: null,
  stageDetail: '',
  answer: '',
  citations: [],
  cost: null,
  runId: null,
  outcome: null,
  degraded: false,
  degradedReason: null,
  cacheHit: 'none',
  error: null,
}

function describeStage(e: Extract<AskEvent, { type: 'stage' }>): string {
  switch (e.stage) {
    case 'started':
      return 'Starting'
    case 'cache_hit':
      return `Cache hit (${e.kind})`
    case 'routing':
      return 'Classifying the question'
    case 'routed':
      return `Intent: ${e.intent}`
    case 'translating':
      return 'Rewriting into policy vocabulary'
    case 'translated':
      return `${e.queries?.length ?? 0} search queries`
    case 'retrieving':
      return 'Searching the policy'
    case 'retrieved':
      return `${e.count} candidate passages`
    case 'reranking':
      return 'Ranking passages with Opus 5'
    case 'reranked':
      return `${e.kept} passages kept`
    case 'generating':
      return 'Writing the answer'
    default:
      return ''
  }
}

export function useAskStream() {
  const [state, setState] = useState<AskState>(EMPTY)
  const abortRef = useRef<AbortController | null>(null)

  const cancel = useCallback(() => {
    abortRef.current?.abort()
    setState((s) => ({ ...s, running: false }))
  }, [])

  const ask = useCallback(async (question: string) => {
    abortRef.current?.abort()
    const controller = new AbortController()
    abortRef.current = controller

    setState({ ...EMPTY, running: true, stage: 'started' })

    try {
      const res = await fetch('/api/ask', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ question }),
        signal: controller.signal,
      })

      if (!res.ok || !res.body) {
        const text = await res.text().catch(() => res.statusText)
        setState((s) => ({ ...s, running: false, error: text || 'Request failed' }))
        return
      }

      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      // Carries the tail of a partially-received event across reads.
      let buffer = ''

      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })

        // Everything up to the last blank line is complete events; whatever
        // follows is a fragment and stays in the buffer.
        const parts = buffer.split('\n\n')
        buffer = parts.pop() ?? ''

        for (const part of parts) {
          let name = 'message'
          const data: string[] = []
          for (const line of part.split('\n')) {
            if (line.startsWith('event: ')) name = line.slice(7).trim()
            else if (line.startsWith('data: ')) data.push(line.slice(6))
          }
          if (!data.length) continue

          let payload: unknown
          try {
            payload = JSON.parse(data.join('\n'))
          } catch {
            continue
          }
          applyEvent(setState, name, payload)
        }
      }
      setState((s) => ({ ...s, running: false }))
    } catch (err) {
      if ((err as Error).name === 'AbortError') return
      setState((s) => ({ ...s, running: false, error: String(err) }))
    }
  }, [])

  return { ...state, ask, cancel }
}

function applyEvent(
  setState: React.Dispatch<React.SetStateAction<AskState>>,
  name: string,
  payload: any,
) {
  switch (name) {
    case 'stage':
      setState((s) => ({
        ...s,
        stage: payload.stage,
        stageDetail: describeStage({ type: 'stage', ...payload }),
        runId: payload.run_id ?? s.runId,
      }))
      break
    case 'token':
      // Appending rather than replacing: the server sends deltas, and the
      // cache-hit path sends one whole-answer token.
      setState((s) => ({ ...s, answer: s.answer + payload.text }))
      break
    case 'citations':
      setState((s) => ({ ...s, citations: payload as Citation[] }))
      break
    case 'cost':
      setState((s) => ({ ...s, cost: { type: 'cost', ...payload } }))
      break
    case 'done':
      setState((s) => ({
        ...s,
        running: false,
        stage: null,
        stageDetail: '',
        runId: payload.run_id,
        outcome: payload.outcome,
        degraded: payload.degraded,
        degradedReason: payload.degraded_reason,
        cacheHit: payload.cache_hit,
      }))
      break
    case 'error':
      setState((s) => ({ ...s, running: false, error: payload.message }))
      break
  }
}
