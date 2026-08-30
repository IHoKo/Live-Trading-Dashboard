import { useEffect, useState } from 'react'

import { TickerTape } from './components/TickerTape'
import { PriceSocketProvider } from './hooks/usePriceSocket'
import { usePriceStore } from './store/prices'

type Health = {
  status: 'ok'
  provider: string
  feed: 'not_started' | 'live' | 'polling' | 'down'
  ws_connected: boolean
  market_open: boolean | null
  subscribed_symbols: number
}

/**
 * Phase 2 shell: the always-on tape from §8.1 plus a feed panel.
 * Portfolio, chart and chat are Phases 3-5; the §8.2 design pass is Phase 6.
 */
export function App() {
  return (
    <PriceSocketProvider>
      <main style={{ minHeight: '100dvh', display: 'flex', flexDirection: 'column' }}>
        <TickerTape />
        <div
          style={{
            flex: 1,
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
              Phase 2 — live prices
            </p>
          </header>

          <FeedPanel />

          <footer
            style={{
              marginTop: 'auto',
              color: 'var(--muted)',
              fontSize: '0.75rem',
              borderTop: '1px solid var(--rule)',
              paddingTop: 'var(--space-3)',
            }}
          >
            Prices may be delayed. Nothing here is investment advice — verify before acting.
          </footer>
        </div>
      </main>
    </PriceSocketProvider>
  )
}

function FeedPanel() {
  const status = usePriceStore((s) => s.status)
  const connection = usePriceStore((s) => s.connection)
  const tickCount = usePriceStore((s) => Object.keys(s.ticks).length)
  const [health, setHealth] = useState<Health | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    fetch('/api/health', { signal: controller.signal })
      .then((res) => (res.ok ? (res.json() as Promise<Health>) : Promise.reject(res.status)))
      .then(setHealth)
      .catch(() => undefined)
    return () => controller.abort()
  }, [])

  const rows: [string, string][] = [
    ['socket', connection],
    ['feed', status?.state ?? '—'],
    ['market', status ? (status.market_open ? 'open' : 'closed') : '—'],
    ['provider', status?.provider ?? health?.provider ?? '—'],
    ['symbols priced', String(tickCount)],
  ]

  return (
    <section
      style={{
        background: 'var(--slate)',
        border: '1px solid var(--rule)',
        borderRadius: 6,
        padding: 'var(--space-3)',
        maxWidth: '32rem',
      }}
    >
      <dl
        style={{
          display: 'grid',
          gridTemplateColumns: 'auto 1fr',
          gap: 'var(--space-2) var(--space-3)',
          margin: 0,
        }}
      >
        {rows.map(([label, value]) => (
          <div key={label} style={{ display: 'contents' }}>
            <dt style={{ color: 'var(--muted)' }}>{label}</dt>
            <dd className="num" style={{ margin: 0 }}>
              {value}
            </dd>
          </div>
        ))}
      </dl>
    </section>
  )
}
