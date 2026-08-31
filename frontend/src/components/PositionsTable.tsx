import {
  money,
  percent,
  qty,
  usePortfolio,
  type Position,
} from '../hooks/usePortfolio'

/**
 * §8.1 positions table. Every number column is tabular-nums via `.num`, or the
 * table jitters as values change (§8.2).
 *
 * A null price renders as an em dash, never as zero — §4's rule that a number
 * you do not have must not look like a number you do.
 */
export function PositionsTable() {
  const { data, isLoading, error } = usePortfolio()

  if (isLoading) return <SkeletonRows />
  if (error) return <Panel tone="loss">Could not load portfolio: {error.message}</Panel>
  if (!data || data.positions.length === 0) {
    return (
      <Panel>
        No positions yet — record a buy below, or just tell the chat what you bought.
      </Panel>
    )
  }

  return (
    <div style={{ overflowX: 'auto' }}>
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 'var(--step-0)' }}>
        <thead>
          <tr style={{ color: 'var(--muted)', textAlign: 'right' }}>
            <Th align="left">SYM</Th>
            <Th>QTY</Th>
            <Th>AVG COST</Th>
            <Th>LAST</Th>
            <Th>DAY</Th>
            <Th>MKT VALUE</Th>
            <Th>P/L</Th>
            <Th>ALLOC</Th>
          </tr>
        </thead>
        <tbody>
          {data.positions.map((position, i) => (
            <Row key={position.symbol} position={position} banded={i % 2 === 1} />
          ))}
        </tbody>
        <tfoot>
          <tr style={{ borderTop: '1px solid var(--rule)' }}>
            <Td align="left" bold>
              TOTAL
            </Td>
            <Td colSpan={4} />
            <Td bold>{money(data.market_value_total ?? data.cost_basis_total)}</Td>
            <Td bold tone={toneOf(data.unrealized_pnl_total)}>
              {money(data.unrealized_pnl_total)}
            </Td>
            <Td />
          </tr>
        </tfoot>
      </table>

      {data.unpriced_symbols.length > 0 && (
        <p style={{ color: 'var(--muted)', fontSize: '0.75rem', marginTop: 'var(--space-2)' }}>
          No price available for {data.unpriced_symbols.join(', ')} — cost basis shown, market
          value omitted.
        </p>
      )}
      <p style={{ color: 'var(--muted)', fontSize: '0.75rem', marginTop: 'var(--space-2)' }}>
        Realized P/L to date: <span className="num">{money(data.realized_pnl_total)}</span>
      </p>
    </div>
  )
}

function Row({ position, banded }: { position: Position; banded: boolean }) {
  return (
    <tr style={{ background: banded ? 'var(--slate)' : 'transparent' }}>
      <Td align="left" bold>
        {position.symbol}
      </Td>
      <Td>{qty(position.quantity)}</Td>
      <Td>{money(position.average_cost)}</Td>
      <Td>{money(position.last_price)}</Td>
      <Td tone={toneOf(position.day_change_pct)}>{percent(position.day_change_pct)}</Td>
      <Td>{money(position.market_value)}</Td>
      <Td tone={toneOf(position.unrealized_pnl)}>
        {money(position.unrealized_pnl)}
        {position.unrealized_pct != null && (
          <span style={{ color: 'var(--muted)' }}> ({percent(position.unrealized_pct)})</span>
        )}
      </Td>
      <Td>{position.allocation_pct == null ? '—' : `${position.allocation_pct.toFixed(1)}%`}</Td>
    </tr>
  )
}

function toneOf(value: number | null | undefined): 'gain' | 'loss' | undefined {
  if (value == null || value === 0) return undefined
  return value > 0 ? 'gain' : 'loss'
}

function Th({ children, align = 'right' }: { children?: React.ReactNode; align?: 'left' | 'right' }) {
  return (
    <th
      scope="col"
      style={{
        textAlign: align,
        padding: 'var(--space-2) var(--space-3)',
        fontWeight: 500,
        fontSize: '0.7rem',
        letterSpacing: '0.08em',
      }}
    >
      {children}
    </th>
  )
}

function Td({
  children,
  align = 'right',
  bold,
  tone,
  colSpan,
}: {
  children?: React.ReactNode
  align?: 'left' | 'right'
  bold?: boolean
  tone?: 'gain' | 'loss'
  colSpan?: number
}) {
  return (
    <td
      colSpan={colSpan}
      className={align === 'right' ? 'num' : undefined}
      style={{
        textAlign: align,
        padding: 'var(--space-2) var(--space-3)',
        fontWeight: bold ? 600 : 400,
        color: tone ? `var(--${tone})` : undefined,
      }}
    >
      {children}
    </td>
  )
}

function Panel({ children, tone }: { children: React.ReactNode; tone?: 'loss' }) {
  return (
    <p
      style={{
        color: tone ? `var(--${tone})` : 'var(--muted)',
        padding: 'var(--space-3)',
        border: '1px dashed var(--rule)',
        borderRadius: 6,
        margin: 0,
      }}
    >
      {children}
    </p>
  )
}

/** Skeleton rows on first load, not spinners (§8.3). */
function SkeletonRows() {
  return (
    <div style={{ display: 'grid', gap: 'var(--space-2)' }}>
      {[0, 1, 2].map((i) => (
        <div
          key={i}
          style={{
            height: '1.75rem',
            background: 'var(--slate)',
            borderRadius: 4,
            opacity: 1 - i * 0.25,
          }}
        />
      ))}
    </div>
  )
}
