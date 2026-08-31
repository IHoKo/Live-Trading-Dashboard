import { useEffect, useRef, useState } from 'react'

import { usePriceSocket, useSymbols } from '../hooks/usePriceSocket'
import { freshness, selectTick, usePriceStore } from '../store/prices'
import { SplitFlapNumber } from './SplitFlap'

/**
 * The always-on strip from §8.1.
 *
 * Phase 2 builds the mechanism: live subscription, per-symbol rendering, honest
 * staleness labels. The split-flap flip from §8.2 is the Phase 6 design pass —
 * this uses a brief tint wash on change, which is the restrained half of that
 * spec and already respects prefers-reduced-motion.
 */

// Phase 3 replaces this with the watchlist table (§5).
export const DEFAULT_WATCHLIST = ['AAPL', 'MSFT', 'NVDA', 'GOOGL', 'AMZN', 'META', 'TSLA']

export function TickerTape({ symbols = DEFAULT_WATCHLIST }: { symbols?: string[] }) {
  useSymbols(symbols)
  const status = usePriceStore((s) => s.status)
  const connection = usePriceStore((s) => s.connection)

  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'stretch',
        gap: 0,
        borderBottom: '1px solid var(--rule)',
        background: 'var(--slate)',
        overflowX: 'auto',
      }}
    >
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          padding: '0 var(--space-3)',
          borderRight: '1px solid var(--rule)',
          color: 'var(--brass)',
          fontFamily: 'var(--font-display)',
          letterSpacing: '0.12em',
          fontSize: '0.7rem',
          whiteSpace: 'nowrap',
        }}
        title={
          status
            ? `${status.provider} · feed ${status.state} · market ${status.market_open ? 'open' : 'closed'}`
            : 'connecting'
        }
      >
        {connection !== 'open'
          ? connection === 'connecting'
            ? 'TAPE ·'
            : 'TAPE ✕'
          : status?.state === 'live'
            ? 'TAPE ● LIVE'
            : status?.market_open === false
              ? 'TAPE ○ CLOSED'
              : 'TAPE'}
      </div>

      {symbols.map((symbol) => (
        <TapeCell key={symbol} symbol={symbol} />
      ))}

      <RefreshButton symbols={symbols} />
    </div>
  )
}

/**
 * The manual fetch. Background polling only runs during market hours, so when
 * the market is closed this button is the only thing that moves prices — hence
 * it lives permanently on the tape rather than hiding in a menu.
 */
function RefreshButton({ symbols }: { symbols: string[] }) {
  const { refresh } = usePriceSocket()
  const status = usePriceStore((s) => s.status)
  const [pending, setPending] = useState(false)
  const lastRefresh = status?.last_refresh_at ?? null

  // Clear the pending state when a fresh result comes back, or after a timeout
  // so a dropped socket can't leave the button stuck.
  useEffect(() => {
    if (!pending) return
    const timer = window.setTimeout(() => setPending(false), 8000)
    return () => window.clearTimeout(timer)
  }, [pending, lastRefresh])

  useEffect(() => {
    setPending(false)
  }, [lastRefresh])

  return (
    <button
      type="button"
      onClick={() => {
        setPending(true)
        refresh(symbols)
      }}
      disabled={pending}
      title={
        lastRefresh
          ? `Last fetched ${new Date(lastRefresh * 1000).toLocaleTimeString()}`
          : 'Fetch current prices'
      }
      style={{
        marginLeft: 'auto',
        alignSelf: 'stretch',
        padding: '0 var(--space-3)',
        border: 'none',
        borderLeft: '1px solid var(--rule)',
        background: 'transparent',
        color: pending ? 'var(--muted)' : 'var(--brass)',
        font: 'inherit',
        fontSize: '0.7rem',
        letterSpacing: '0.12em',
        cursor: pending ? 'default' : 'pointer',
        whiteSpace: 'nowrap',
      }}
    >
      {pending ? 'FETCHING…' : '↻ REFRESH'}
    </button>
  )
}

function TapeCell({ symbol }: { symbol: string }) {
  // Selecting one symbol means one print re-renders one cell (§8.3).
  const tick = usePriceStore(selectTick(symbol))
  const status = usePriceStore((s) => s.status)
  const flash = useFlash(tick?.p)
  const fresh = freshness(tick, status)

  const up = (tick?.dp ?? 0) >= 0
  const changeColor = tick?.dp == null ? 'var(--muted)' : up ? 'var(--gain)' : 'var(--loss)'

  return (
    <div
      className={`tape-cell${flash ? (up ? ' wash-up' : ' wash-down') : ''}`}
      style={{
        display: 'flex',
        flexDirection: 'column',
        gap: 3,
        padding: 'var(--space-2) var(--space-3)',
        borderRight: '1px solid var(--rule)',
        minWidth: '8.75rem',
      }}
    >
      <div style={{ display: 'flex', justifyContent: 'space-between', gap: 'var(--space-2)' }}>
        <span style={{ fontWeight: 600, letterSpacing: '0.04em' }}>{symbol}</span>
        <span
          className="num"
          style={{ fontSize: '0.6rem', color: fresh.tone === 'live' ? 'var(--gain)' : fresh.tone === 'stale' ? 'var(--loss)' : 'var(--muted)' }}
        >
          {fresh.label}
        </span>
      </div>
      <div style={{ fontSize: 'var(--step-1)' }}>
        {/* §8.2: the digits flip. The tape is the only place this happens. */}
        <SplitFlapNumber
          value={tick ? tick.p.toFixed(2) : '--.--'}
          label={tick ? `${symbol} ${tick.p.toFixed(2)}` : `${symbol} no price`}
        />
      </div>
      <div className="num" style={{ fontSize: '0.7rem', color: changeColor }}>
        {tick?.dp == null ? '—' : `${up ? '▲' : '▼'} ${Math.abs(tick.dp).toFixed(2)}%`}
      </div>
    </div>
  )
}

/** Brief true after the value changes, so the cell can tint and decay. */
function useFlash(value: number | undefined, ms = 400): boolean {
  const [on, setOn] = useState(false)
  const previous = useRef(value)

  useEffect(() => {
    if (previous.current === value || value === undefined) return
    previous.current = value

    const reduced =
      typeof window !== 'undefined' &&
      window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
    if (reduced) return // §8.3 accessibility floor

    setOn(true)
    const timer = window.setTimeout(() => setOn(false), ms)
    return () => window.clearTimeout(timer)
  }, [value, ms])

  return on
}
