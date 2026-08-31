import { money, usePortfolio } from '../hooks/usePortfolio'

/**
 * Allocation view (§10 Phase 3). A stacked proportional bar rather than a pie:
 * it reads left-to-right at a glance, stays legible at small widths, and needs
 * no chart library.
 *
 * Colours come from the §8.2 palette, cycling brass -> gain -> loss -> slate
 * at varying weight. Deliberately not a rainbow: hue here carries no meaning
 * beyond "these are different rows".
 */
const BANDS = ['var(--brass)', 'var(--gain)', 'var(--loss)', 'color-mix(in srgb, var(--paper) 40%, var(--slate))']

export function Allocation() {
  const { data } = usePortfolio()
  const priced = data?.positions.filter((p) => p.allocation_pct != null) ?? []

  if (!data || priced.length === 0) {
    return (
      <p style={{ color: 'var(--muted)', fontSize: '0.8rem', margin: 0 }}>
        Allocation needs live prices — none available right now.
      </p>
    )
  }

  return (
    <div style={{ display: 'grid', gap: 'var(--space-3)' }}>
      <div
        style={{ display: 'flex', height: '1.5rem', borderRadius: 3, overflow: 'hidden' }}
        role="img"
        aria-label={priced
          .map((p) => `${p.symbol} ${p.allocation_pct?.toFixed(1)} percent`)
          .join(', ')}
      >
        {priced.map((position, i) => (
          <div
            key={position.symbol}
            title={`${position.symbol} — ${position.allocation_pct?.toFixed(1)}%`}
            style={{
              width: `${position.allocation_pct}%`,
              background: BANDS[i % BANDS.length],
              opacity: 1 - Math.floor(i / BANDS.length) * 0.25,
            }}
          />
        ))}
      </div>

      <ul style={{ listStyle: 'none', margin: 0, padding: 0, display: 'grid', gap: 'var(--space-1)' }}>
        {priced.map((position, i) => (
          <li
            key={position.symbol}
            style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)', fontSize: '0.8rem' }}
          >
            <span
              aria-hidden
              style={{
                width: 10,
                height: 10,
                background: BANDS[i % BANDS.length],
                opacity: 1 - Math.floor(i / BANDS.length) * 0.25,
                borderRadius: 2,
              }}
            />
            <span style={{ fontWeight: 600, minWidth: '3.5rem' }}>{position.symbol}</span>
            <span className="num" style={{ color: 'var(--muted)' }}>
              {position.allocation_pct?.toFixed(1)}%
            </span>
            <span className="num" style={{ marginLeft: 'auto' }}>
              {money(position.market_value)}
            </span>
          </li>
        ))}
      </ul>
    </div>
  )
}
