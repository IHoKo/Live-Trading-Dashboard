import type { ReactNode } from 'react'

/**
 * The panel the whole dashboard is built from. §8.2 is an exchange board, so
 * these read as plates bolted to it: a flat slate ground, a hairline rule under
 * the label, and no drop shadows.
 */
export function Card({
  title,
  actions,
  children,
  pad = true,
}: {
  title?: string
  actions?: ReactNode
  children: ReactNode
  pad?: boolean
}) {
  return (
    <section
      style={{
        background: 'var(--slate)',
        border: '1px solid var(--rule)',
        borderRadius: 4,
        display: 'flex',
        flexDirection: 'column',
        minWidth: 0,
      }}
    >
      {title && (
        <header
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            gap: 'var(--space-3)',
            padding: '0.55rem var(--space-3)',
            borderBottom: '1px solid var(--rule)',
          }}
        >
          <h2
            style={{
              margin: 0,
              fontSize: '0.68rem',
              letterSpacing: '0.14em',
              fontWeight: 500,
              color: 'var(--muted)',
            }}
          >
            {title.toUpperCase()}
          </h2>
          {actions}
        </header>
      )}
      <div style={{ padding: pad ? 'var(--space-3)' : 0, minWidth: 0 }}>{children}</div>
    </section>
  )
}

/** A single headline number. Tabular by construction — these sit in a row. */
export function Stat({
  label,
  value,
  tone,
  hint,
}: {
  label: string
  value: string
  tone?: 'gain' | 'loss'
  hint?: string
}) {
  return (
    <div
      style={{
        background: 'var(--slate)',
        border: '1px solid var(--rule)',
        borderRadius: 4,
        padding: 'var(--space-3)',
        display: 'grid',
        gap: 2,
        minWidth: 0,
      }}
    >
      <span
        style={{
          fontSize: '0.62rem',
          letterSpacing: '0.14em',
          color: 'var(--muted)',
          whiteSpace: 'nowrap',
        }}
      >
        {label.toUpperCase()}
      </span>
      <span
        className="num"
        style={{
          fontSize: '1.45rem',
          fontWeight: 600,
          letterSpacing: '-0.02em',
          color: tone ? `var(--${tone})` : 'var(--paper)',
          lineHeight: 1.15,
        }}
      >
        {value}
      </span>
      {hint && (
        <span className="num" style={{ fontSize: '0.7rem', color: 'var(--muted)' }}>
          {hint}
        </span>
      )}
    </div>
  )
}
