import { createContext, useCallback, useContext, useEffect, useRef, type ReactNode } from 'react'

import { usePriceStore, type Status, type Tick } from '../store/prices'

/**
 * One WebSocket for the whole app, owned by a provider (§8.3).
 *
 * Reconnects with exponential backoff, 1s -> 30s cap, and re-subscribes on open
 * — a Fly deploy replaces the machine, so every client has to survive the
 * socket vanishing mid-session (§9.4).
 */

const RECONNECT_BASE_MS = 1_000
const RECONNECT_CAP_MS = 30_000

type SocketApi = {
  subscribe: (symbols: string[]) => void
  unsubscribe: (symbols: string[]) => void
  /** Fetch now. Outside market hours this is the only thing that moves prices. */
  refresh: (symbols?: string[]) => void
}

const PriceSocketContext = createContext<SocketApi | null>(null)

export function PriceSocketProvider({ children }: { children: ReactNode }) {
  const socketRef = useRef<WebSocket | null>(null)
  // Symbols this client wants. Replayed on every reconnect.
  const wantedRef = useRef<Set<string>>(new Set())
  const backoffRef = useRef(RECONNECT_BASE_MS)
  const retryRef = useRef<number | null>(null)
  const closedByUsRef = useRef(false)

  const applyTick = usePriceStore((s) => s.applyTick)
  const setStatus = usePriceStore((s) => s.setStatus)
  const setConnection = usePriceStore((s) => s.setConnection)

  const send = useCallback((payload: object) => {
    const sock = socketRef.current
    if (sock?.readyState === WebSocket.OPEN) sock.send(JSON.stringify(payload))
  }, [])

  const subscribe = useCallback(
    (symbols: string[]) => {
      const added = symbols.map((s) => s.trim().toUpperCase()).filter(Boolean)
      added.forEach((s) => wantedRef.current.add(s))
      if (added.length) send({ type: 'subscribe', symbols: added })
    },
    [send],
  )

  const unsubscribe = useCallback(
    (symbols: string[]) => {
      const removed = symbols.map((s) => s.trim().toUpperCase()).filter(Boolean)
      removed.forEach((s) => wantedRef.current.delete(s))
      if (removed.length) send({ type: 'unsubscribe', symbols: removed })
    },
    [send],
  )

  const refresh = useCallback(
    (symbols?: string[]) => {
      send({ type: 'refresh', ...(symbols?.length ? { symbols } : {}) })
    },
    [send],
  )

  useEffect(() => {
    closedByUsRef.current = false

    const connect = () => {
      const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws'
      const sock = new WebSocket(`${scheme}://${window.location.host}/ws/prices`)
      socketRef.current = sock
      setConnection('connecting')

      sock.onopen = () => {
        backoffRef.current = RECONNECT_BASE_MS
        setConnection('open')
        // Re-subscribe: the server has no memory of this client.
        const wanted = [...wantedRef.current]
        if (wanted.length) sock.send(JSON.stringify({ type: 'subscribe', symbols: wanted }))
      }

      sock.onmessage = (event) => {
        let msg: unknown
        try {
          msg = JSON.parse(event.data as string)
        } catch {
          return
        }
        if (typeof msg !== 'object' || msg === null) return
        const data = msg as { type?: string }

        if (data.type === 'tick') applyTick(msg as Tick)
        else if (data.type === 'status') setStatus(msg as Status)
        // §9.4: the Fly proxy drops idle sockets, so the server pings us.
        else if (data.type === 'ping') sock.send(JSON.stringify({ type: 'pong' }))
      }

      sock.onclose = () => {
        setConnection('closed')
        if (closedByUsRef.current) return
        const delay = backoffRef.current
        backoffRef.current = Math.min(delay * 2, RECONNECT_CAP_MS)
        retryRef.current = window.setTimeout(connect, delay)
      }

      sock.onerror = () => sock.close()
    }

    connect()

    return () => {
      closedByUsRef.current = true
      if (retryRef.current !== null) window.clearTimeout(retryRef.current)
      socketRef.current?.close()
      socketRef.current = null
    }
  }, [applyTick, setStatus, setConnection])

  return (
    <PriceSocketContext.Provider value={{ subscribe, unsubscribe, refresh }}>
      {children}
    </PriceSocketContext.Provider>
  )
}

export function usePriceSocket(): SocketApi {
  const ctx = useContext(PriceSocketContext)
  if (!ctx) throw new Error('usePriceSocket must be used inside <PriceSocketProvider>')
  return ctx
}

/** Subscribe for the lifetime of a component. */
export function useSymbols(symbols: string[]) {
  const { subscribe, unsubscribe } = usePriceSocket()
  const key = symbols.join(',')
  useEffect(() => {
    const list = key ? key.split(',') : []
    subscribe(list)
    return () => unsubscribe(list)
  }, [key, subscribe, unsubscribe])
}
