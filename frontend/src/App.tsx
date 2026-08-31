import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useCallback, useEffect, useState } from 'react'

import { Allocation } from './components/Allocation'
import { Card, Stat } from './components/Card'
import { Chart } from './components/Chart'
import { Chat } from './components/Chat'
import { ErrorBoundary } from './components/ErrorBoundary'
import { Login } from './components/Login'
import { PerformanceChart } from './components/PerformanceChart'
import { PositionsTable } from './components/PositionsTable'
import { ThemeToggle } from './components/Theme'
import { DEFAULT_WATCHLIST, TickerTape } from './components/TickerTape'
import { TransactionForm, TransactionHistory } from './components/TransactionForm'
import { money, percent, usePortfolio } from './hooks/usePortfolio'
import { PriceSocketProvider } from './hooks/usePriceSocket'
import { usePriceStore } from './store/prices'

/**
 * §8.1 layout: an always-on tape across the top, portfolio and chart down the
 * left, chat on the right. Built from Cards so the page reads as plates on a
 * board rather than text separated by rules.
 */
const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, staleTime: 10_000 } },
})

export function App() {
  const [gate, setGate] = useState<'checking' | 'locked' | 'open'>('checking')

  const check = useCallback(async () => {
    try {
      const res = await fetch('/api/auth/session')
      const body = (await res.json()) as { authenticated: boolean; configured: boolean }
      setGate(body.authenticated || !body.configured ? 'open' : 'locked')
    } catch {
      setGate('locked')
    }
  }, [])

  useEffect(() => {
    void check()
  }, [check])

  if (gate === 'checking') return null
  if (gate === 'locked') return <Login onSuccess={() => setGate('open')} />

  return (
    <QueryClientProvider client={queryClient}>
      <PriceSocketProvider>
        <div style={{ minHeight: '100dvh', display: 'flex', flexDirection: 'column' }}>
          <TopBar />
          <TickerTape />

          <main
            style={{
              flex: 1,
              width: '100%',
              maxWidth: '96rem',
              margin: '0 auto',
              padding: 'var(--space-4)',
              display: 'grid',
              gap: 'var(--space-4)',
              gridTemplateColumns: 'minmax(0, 1fr) minmax(21rem, 26rem)',
              alignItems: 'start',
            }}
            className="dashboard-grid"
          >
            <div style={{ display: 'grid', gap: 'var(--space-4)', minWidth: 0 }}>
              <StatRow />

              <ErrorBoundary name="positions">
                <Card title="Positions" pad={false}>
                  <PositionsTable />
                </Card>
              </ErrorBoundary>

              <div
                style={{
                  display: 'grid',
                  gap: 'var(--space-4)',
                  gridTemplateColumns: 'repeat(auto-fit, minmax(22rem, 1fr))',
                }}
              >
                <ErrorBoundary name="portfolio value">
                  <Card title="Portfolio value">
                    <PerformanceChart />
                  </Card>
                </ErrorBoundary>

                <ErrorBoundary name="chart">
                  <Card title="Price">
                    <SymbolChart />
                  </Card>
                </ErrorBoundary>
              </div>

              <div
                style={{
                  display: 'grid',
                  gap: 'var(--space-4)',
                  gridTemplateColumns: 'repeat(auto-fit, minmax(20rem, 1fr))',
                }}
              >
                <ErrorBoundary name="allocation">
                  <Card title="Allocation">
                    <Allocation />
                  </Card>
                </ErrorBoundary>

                <ErrorBoundary name="history">
                  <Card title="Recent activity" pad={false}>
                    <TransactionHistory />
                  </Card>
                </ErrorBoundary>
              </div>

              <ErrorBoundary name="transaction form">
                <Card title="Record a transaction">
                  <TransactionForm />
                </Card>
              </ErrorBoundary>
            </div>

            <ErrorBoundary name="chat">
              <Chat />
            </ErrorBoundary>
          </main>

          <footer
            style={{
              color: 'var(--muted)',
              fontSize: '0.72rem',
              borderTop: '1px solid var(--rule)',
              padding: 'var(--space-3) var(--space-4)',
              textAlign: 'center',
            }}
          >
            Prices may be delayed. Nothing here is investment advice — verify before acting.
          </footer>
        </div>
      </PriceSocketProvider>
    </QueryClientProvider>
  )
}

function TopBar() {
  const status = usePriceStore((s) => s.status)
  const connection = usePriceStore((s) => s.connection)

  const feed =
    connection !== 'open'
      ? { text: 'RECONNECTING', tone: 'var(--muted)' }
      : status?.state === 'live'
        ? { text: 'LIVE', tone: 'var(--gain)' }
        : status?.market_open === false
          ? { text: 'MARKET CLOSED', tone: 'var(--muted)' }
          : { text: 'DELAYED', tone: 'var(--muted)' }

  return (
    <header
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 'var(--space-3)',
        padding: '0.7rem var(--space-4)',
        borderBottom: '1px solid var(--rule)',
      }}
    >
      <span
        style={{
          fontFamily: 'var(--font-display)',
          fontSize: '1.1rem',
          letterSpacing: '-0.02em',
          fontWeight: 700,
        }}
      >
        TICKER
      </span>
      <span
        style={{
          display: 'inline-flex',
          alignItems: 'center',
          gap: '0.35rem',
          fontSize: '0.62rem',
          letterSpacing: '0.14em',
          color: feed.tone,
        }}
      >
        <span
          aria-hidden
          style={{
            width: 6,
            height: 6,
            borderRadius: '50%',
            background: feed.tone,
            display: 'inline-block',
          }}
        />
        {feed.text}
      </span>
      {status && (
        <span style={{ fontSize: '0.62rem', color: 'var(--muted)', letterSpacing: '0.1em' }}>
          {status.provider}
        </span>
      )}
      <span style={{ marginLeft: 'auto', display: 'flex', gap: 'var(--space-2)' }}>
        <ThemeToggle />
        <button
          type="button"
          onClick={() => {
            void fetch('/api/auth/logout', { method: 'POST' }).then(() =>
              window.location.reload(),
            )
          }}
          style={{
            background: 'transparent',
            border: '1px solid var(--rule)',
            borderRadius: 3,
            color: 'var(--muted)',
            font: 'inherit',
            fontSize: '0.68rem',
            letterSpacing: '0.1em',
            padding: '0.25rem 0.55rem',
            cursor: 'pointer',
          }}
        >
          LOG OUT
        </button>
      </span>
    </header>
  )
}

function StatRow() {
  const { data } = usePortfolio()
  const value = data?.market_value_total ?? null
  const basis = data?.cost_basis_total ?? 0
  const unrealized = data?.unrealized_pnl_total ?? null
  const realized = data?.realized_pnl_total ?? 0
  const pct = unrealized != null && basis ? (unrealized / basis) * 100 : null

  return (
    <div
      style={{
        display: 'grid',
        gap: 'var(--space-3)',
        gridTemplateColumns: 'repeat(auto-fit, minmax(11rem, 1fr))',
      }}
    >
      <Stat
        label={value == null ? 'Cost basis' : 'Portfolio value'}
        value={money(value ?? basis)}
        hint={value == null ? 'awaiting prices' : `invested ${money(basis)}`}
      />
      <Stat
        label="Unrealized P/L"
        value={unrealized == null ? '—' : money(unrealized)}
        tone={unrealized == null || unrealized === 0 ? undefined : unrealized > 0 ? 'gain' : 'loss'}
        hint={pct == null ? undefined : percent(pct)}
      />
      <Stat label="Realized P/L" value={money(realized)} hint="to date" />
      <Stat label="Positions" value={String(data?.positions.length ?? 0)} hint="open" />
    </div>
  )
}

/** §8.1: the chart follows the selected symbol. Until there is a selection UI,
    it follows the largest holding, falling back to the tape's lead symbol. */
function SymbolChart() {
  const { data } = usePortfolio()
  const symbol = data?.positions[0]?.symbol ?? DEFAULT_WATCHLIST[0]
  return <Chart symbol={symbol} />
}
