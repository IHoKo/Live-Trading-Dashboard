import { useEffect, useState } from 'react'

/** Shape of GET /api/health — mirrors app/routers/health.py. */
type Health = {
  status: 'ok'
  provider: string
  feed: 'not_started' | 'live' | 'polling' | 'down'
  ws_connected: boolean
  market_open: boolean | null
}

/**
 * Phase 0 shell. Its only job is to prove the deploy path end to end: the Vite
 * build is served by FastAPI, and the SPA can reach the API on the same origin.
 * The real layout (§8.1) arrives with the dashboard in Phases 2–4.
 */
export function App() {
  const [health, setHealth] = useState<Health | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    fetch('/api/health', { signal: controller.signal })
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        return res.json() as Promise<Health>
      })
      .then(setHealth)
      .catch((err: unknown) => {
        if (err instanceof DOMException && err.name === 'AbortError') return
        setError(err instanceof Error ? err.message : String(err))
      })
    return () => controller.abort()
  }, [])

  return (
    <main
      style={{
        minHeight: '100dvh',
        display: 'flex',
        flexDirection: 'column',
        gap: 'var(--space-4)',
        padding: 'var(--space-5)',
      }}
    >
      <header>
        <h1
          style={{
            fontFamily: 'var(--font-display)',
            fontSize: 'var(--step-3)',
            letterSpacing: '-0.03em',
            margin: 0,
          }}
        >
          TICKER
        </h1>
        <p style={{ color: 'var(--muted)', margin: 'var(--space-1) 0 0' }}>
          Phase 0 — skeleton deploy
        </p>
      </header>

      <section
        style={{
          background: 'var(--slate)',
          border: `1px solid var(--rule)`,
          borderRadius: 6,
          padding: 'var(--space-3)',
          maxWidth: '32rem',
        }}
      >
        {error !== null ? (
          <p style={{ color: 'var(--loss)', margin: 0 }}>
            <span className="num">/api/health</span> unreachable: {error}
          </p>
        ) : health === null ? (
          <p style={{ color: 'var(--muted)', margin: 0 }}>Checking backend…</p>
        ) : (
          <dl
            style={{
              display: 'grid',
              gridTemplateColumns: 'auto 1fr',
              gap: 'var(--space-2) var(--space-3)',
              margin: 0,
            }}
          >
            <dt style={{ color: 'var(--muted)' }}>status</dt>
            <dd className="num" style={{ margin: 0, color: 'var(--gain)' }}>
              {health.status}
            </dd>
            <dt style={{ color: 'var(--muted)' }}>provider</dt>
            <dd className="num" style={{ margin: 0 }}>
              {health.provider}
            </dd>
            <dt style={{ color: 'var(--muted)' }}>feed</dt>
            <dd className="num" style={{ margin: 0 }}>
              {health.feed}
            </dd>
            <dt style={{ color: 'var(--muted)' }}>market</dt>
            <dd className="num" style={{ margin: 0 }}>
              {health.market_open === null ? 'unknown' : health.market_open ? 'open' : 'closed'}
            </dd>
          </dl>
        )}
      </section>

      {/* Persistent disclaimer — plan.md §11. Not optional, at any phase. */}
      <footer
        style={{
          marginTop: 'auto',
          color: 'var(--muted)',
          fontSize: '0.75rem',
          borderTop: `1px solid var(--rule)`,
          paddingTop: 'var(--space-3)',
        }}
      >
        Prices may be delayed. Nothing here is investment advice — verify before acting.
      </footer>
    </main>
  )
}
