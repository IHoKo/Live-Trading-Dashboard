import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { Allocation } from './components/Allocation'
import { Chat } from './components/Chat'
import { PositionsTable } from './components/PositionsTable'
import { TickerTape } from './components/TickerTape'
import { TransactionForm, TransactionHistory } from './components/TransactionForm'
import { PriceSocketProvider } from './hooks/usePriceSocket'
import { money, usePortfolio } from './hooks/usePortfolio'

/**
 * Phase 3 shell: tape, portfolio value, positions, allocation, add/remove.
 * Chart is Phase 4 and chat is Phase 5, so the §8.1 right-hand column is not
 * built yet. The §8.2 design pass is Phase 6.
 */
const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, staleTime: 10_000 } },
})

export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <PriceSocketProvider>
        <main style={{ minHeight: '100dvh', display: 'flex', flexDirection: 'column' }}>
          <TickerTape />
          <div
            style={{
              flex: 1,
              display: 'flex',
              flexDirection: 'column',
              gap: 'var(--space-5)',
              padding: 'var(--space-5)',
              maxWidth: '84rem',
              width: '100%',
            }}
          >
            <PortfolioValue />

            <div
              style={{
                display: 'grid',
                gap: 'var(--space-5)',
                gridTemplateColumns: 'minmax(0, 2fr) minmax(20rem, 1fr)',
                alignItems: 'start',
              }}
              className="dashboard-grid"
            >
              <div style={{ display: 'grid', gap: 'var(--space-5)', minWidth: 0 }}>
            <Section title="Positions">
              <PositionsTable />
            </Section>

            <Section title="Allocation">
              <Allocation />
            </Section>

            <Section title="Record a transaction">
              <TransactionForm />
            </Section>

            <Section title="History">
              <TransactionHistory />
            </Section>
              </div>

              <Chat />
            </div>

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
    </QueryClientProvider>
  )
}

function PortfolioValue() {
  const { data } = usePortfolio()
  const total = data?.market_value_total ?? data?.cost_basis_total ?? null
  const pnl = data?.unrealized_pnl_total ?? null
  const tone = pnl == null || pnl === 0 ? 'var(--paper)' : pnl > 0 ? 'var(--gain)' : 'var(--loss)'

  return (
    <header>
      <p
        style={{
          color: 'var(--muted)',
          margin: 0,
          fontSize: '0.7rem',
          letterSpacing: '0.12em',
        }}
      >
        {data?.market_value_total == null ? 'PORTFOLIO COST BASIS' : 'PORTFOLIO VALUE'}
      </p>
      {/* aria-live on the total only — announcing every tick would flood a
          screen reader (§8.3). */}
      <p
        aria-live="polite"
        className="num"
        style={{
          fontFamily: 'var(--font-display)',
          fontSize: 'var(--step-3)',
          letterSpacing: '-0.02em',
          margin: 'var(--space-1) 0 0',
        }}
      >
        {money(total)}
      </p>
      <p className="num" style={{ color: tone, margin: 'var(--space-1) 0 0' }}>
        {pnl == null ? 'Unrealized P/L unavailable' : `${pnl >= 0 ? '▲' : '▼'} ${money(Math.abs(pnl))} unrealized`}
      </p>
    </header>
  )
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section style={{ display: 'grid', gap: 'var(--space-3)' }}>
      <h2
        style={{
          margin: 0,
          fontSize: '0.7rem',
          letterSpacing: '0.12em',
          color: 'var(--muted)',
          fontWeight: 500,
          borderBottom: '1px solid var(--rule)',
          paddingBottom: 'var(--space-2)',
        }}
      >
        {title.toUpperCase()}
      </h2>
      {children}
    </section>
  )
}
