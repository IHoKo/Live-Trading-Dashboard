import { useQueryClient } from '@tanstack/react-query'
import { useCallback, useEffect, useRef, useState } from 'react'

/**
 * Chat over SSE. The stream is a POST (it carries the message body), so
 * EventSource is unusable — it only does GET. This parses the SSE framing off
 * a fetch body stream instead.
 */

export type Citation = { url: string; title: string }

export type PendingAction = {
  action_id: string
  kind: 'add' | 'remove'
  symbol: string
  quantity: number
  price: number
  price_source: 'user' | 'quote'
  executed_at: string
  note: string | null
  total: number
  expires_at: number
}

export type ChatTurn = {
  role: 'user' | 'assistant'
  text: string
  citations: Citation[]
  pending: PendingAction | null
  /** Set once the card is resolved, so it stops being actionable. */
  resolution?: 'recorded' | 'cancelled' | 'expired'
  tools: string[]
}

type Frame = { event: string; data: Record<string, unknown> }

/** Split an SSE byte stream into frames. */
async function* frames(body: ReadableStream<Uint8Array>): AsyncGenerator<Frame> {
  const reader = body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })

    let split: number
    while ((split = buffer.indexOf('\n\n')) !== -1) {
      const raw = buffer.slice(0, split)
      buffer = buffer.slice(split + 2)

      let event = 'message'
      const dataLines: string[] = []
      for (const line of raw.split('\n')) {
        if (line.startsWith('event:')) event = line.slice(6).trim()
        else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim())
      }
      if (!dataLines.length) continue
      try {
        yield { event, data: JSON.parse(dataLines.join('\n')) as Record<string, unknown> }
      } catch {
        /* a malformed frame must not kill the stream */
      }
    }
  }
}

export function useChat() {
  const queryClient = useQueryClient()
  const [turns, setTurns] = useState<ChatTurn[]>([])
  const [streaming, setStreaming] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const abortRef = useRef<AbortController | null>(null)

  useEffect(() => () => abortRef.current?.abort(), [])

  const patchLast = useCallback((fn: (turn: ChatTurn) => ChatTurn) => {
    setTurns((prev) => {
      if (!prev.length) return prev
      const next = [...prev]
      next[next.length - 1] = fn(next[next.length - 1])
      return next
    })
  }, [])

  const send = useCallback(
    async (message: string) => {
      if (!message.trim() || streaming) return
      setError(null)
      setStreaming(true)
      setTurns((prev) => [
        ...prev,
        { role: 'user', text: message, citations: [], pending: null, tools: [] },
        { role: 'assistant', text: '', citations: [], pending: null, tools: [] },
      ])

      const controller = new AbortController()
      abortRef.current = controller

      try {
        const res = await fetch('/api/chat/stream', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ message }),
          signal: controller.signal,
        })

        if (!res.ok || !res.body) {
          const detail = await res
            .json()
            .then((b: { detail?: string }) => b.detail)
            .catch(() => null)
          throw new Error(detail ?? `HTTP ${res.status}`)
        }

        for await (const frame of frames(res.body)) {
          switch (frame.event) {
            case 'text':
              patchLast((t) => ({ ...t, text: t.text + String(frame.data.delta ?? '') }))
              break
            case 'tool':
              if (frame.data.status === 'running') {
                patchLast((t) => ({ ...t, tools: [...t.tools, String(frame.data.name)] }))
              }
              break
            case 'citation':
              patchLast((t) => ({
                ...t,
                citations: [...t.citations, frame.data as unknown as Citation],
              }))
              break
            case 'pending_action':
              patchLast((t) => ({ ...t, pending: frame.data as unknown as PendingAction }))
              break
            case 'error':
              setError(String(frame.data.message ?? 'Something went wrong.'))
              break
          }
        }
      } catch (err) {
        if (!(err instanceof DOMException && err.name === 'AbortError')) {
          setError(err instanceof Error ? err.message : String(err))
        }
      } finally {
        setStreaming(false)
        abortRef.current = null
      }
    },
    [patchLast, streaming],
  )

  /** The only path that writes to the ledger (§7.2). */
  const resolve = useCallback(
    async (actionId: string, approved: boolean) => {
      const res = await fetch('/api/chat/confirm', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action_id: actionId, approved }),
      })

      if (res.status === 410) {
        setTurns((prev) =>
          prev.map((t) =>
            t.pending?.action_id === actionId ? { ...t, resolution: 'expired' } : t,
          ),
        )
        setError('That confirmation expired. Ask again to redo it.')
        return
      }
      if (!res.ok) {
        const detail = await res
          .json()
          .then((b: { detail?: string }) => b.detail)
          .catch(() => null)
        setError(detail ?? `HTTP ${res.status}`)
        return
      }

      setTurns((prev) =>
        prev.map((t) =>
          t.pending?.action_id === actionId
            ? { ...t, resolution: approved ? 'recorded' : 'cancelled' }
            : t,
        ),
      )
      if (approved) {
        void queryClient.invalidateQueries({ queryKey: ['portfolio'] })
        void queryClient.invalidateQueries({ queryKey: ['transactions'] })
      }
    },
    [queryClient],
  )

  const clear = useCallback(async () => {
    await fetch('/api/chat/history', { method: 'DELETE' })
    setTurns([])
    setError(null)
  }, [])

  return { turns, streaming, error, send, resolve, clear }
}
