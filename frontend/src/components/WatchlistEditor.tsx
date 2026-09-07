import { useEffect, useRef, useState, type FormEvent } from 'react'

import {
  useAddSymbol,
  useRemoveSymbol,
  useSymbolSearch,
  useWatchlist,
} from '../hooks/useWatchlist'

/**
 * Choose what the tape follows (§6, §8.1).
 *
 * Typing looks symbols up through /api/search rather than trusting whatever is
 * typed — the server validates again on save, but catching it here means you
 * pick "Advanced Micro Devices" instead of guessing whether it's AMD.
 */
export function WatchlistEditor({ onClose }: { onClose: () => void }) {
  const { data } = useWatchlist()
  const add = useAddSymbol()
  const remove = useRemoveSymbol()
  const [query, setQuery] = useState('')
  const [debounced, setDebounced] = useState('')
  const inputRef = useRef<HTMLInputElement>(null)

  // The free tier allows 60 lookups a minute; one per keystroke would spend
  // that in ten seconds of typing.
  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(query), 220)
    return () => window.clearTimeout(timer)
  }, [query])

  const { data: search, isFetching } = useSymbolSearch(debounced)
  const onList = new Set(data?.symbols ?? [])
  const matches = (search?.results ?? []).filter((m) => !onList.has(m.symbol)).slice(0, 6)
  const full = (data?.symbols.length ?? 0) >= (data?.max_symbols ?? 50)

  const submit = (event: FormEvent) => {
    event.preventDefault()
    const symbol = matches[0]?.symbol ?? query.trim().toUpperCase()
    if (!symbol) return
    add.mutate(symbol, {
      onSuccess: () => {
        setQuery('')
        setDebounced('')
        inputRef.current?.focus()
      },
    })
  }

  const pick = (symbol: string) => {
    add.mutate(symbol, { onSuccess: () => { setQuery(''); setDebounced('') } })
  }

  return (
    <div style={{ display: 'grid', gap: 'var(--space-3)' }}>
      <form onSubmit={submit} style={{ display: 'flex', gap: 'var(--space-2)' }}>
        <label htmlFor="wl-add" style={{ position: 'absolute', left: -9999 }}>
          Add a symbol to the tape
        </label>
        <input
          id="wl-add"
          ref={inputRef}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder={full ? 'Watchlist is full' : 'Add a symbol — name or ticker'}
          disabled={full || add.isPending}
          autoComplete="off"
          style={{
            flex: 1,
            padding: 'var(--space-2)',
            background: 'var(--ink)',
            border: '1px solid var(--rule)',
            color: 'var(--paper)',
            font: 'inherit',
          }}
        />
        <button
          type="submit"
          disabled={full || add.isPending || !query.trim()}
          style={{
            padding: 'var(--space-2) var(--space-3)',
            border: '1px solid var(--brass)',
            background: 'transparent',
            color: 'var(--brass)',
            font: 'inherit',
            fontSize: '0.72rem',
            letterSpacing: '0.08em',
            cursor: 'pointer',
          }}
        >
          {add.isPending ? 'ADDING…' : 'ADD'}
        </button>
        <button
          type="button"
          onClick={onClose}
          style={{
            padding: 'var(--space-2) var(--space-3)',
            border: '1px solid var(--rule)',
            background: 'transparent',
            color: 'var(--muted)',
            font: 'inherit',
            fontSize: '0.72rem',
            cursor: 'pointer',
          }}
        >
          DONE
        </button>
      </form>

      {(add.error || remove.error) && (
        <p role="alert" style={{ margin: 0, color: 'var(--loss)', fontSize: '0.82rem' }}>
          {(add.error ?? remove.error)?.message}
        </p>
      )}

      {debounced.length >= 2 && (
        <ul style={{ listStyle: 'none', margin: 0, padding: 0, display: 'grid', gap: 1 }}>
          {isFetching && matches.length === 0 && (
            <li style={{ color: 'var(--muted)', fontSize: '0.8rem' }}>Searching…</li>
          )}
          {!isFetching && matches.length === 0 && (
            <li style={{ color: 'var(--muted)', fontSize: '0.8rem' }}>No matches.</li>
          )}
          {matches.map((match) => (
            <li key={match.symbol}>
              <button
                type="button"
                onClick={() => pick(match.symbol)}
                style={{
                  width: '100%',
                  display: 'flex',
                  gap: 'var(--space-3)',
                  alignItems: 'baseline',
                  padding: 'var(--space-2)',
                  background: 'transparent',
                  border: '1px solid transparent',
                  borderBottom: '1px solid var(--rule)',
                  color: 'var(--paper)',
                  font: 'inherit',
                  fontSize: '0.85rem',
                  textAlign: 'left',
                  cursor: 'pointer',
                }}
              >
                <span className="num" style={{ fontWeight: 600, minWidth: '4.5rem' }}>
                  {match.display_symbol || match.symbol}
                </span>
                <span style={{ color: 'var(--muted)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {match.description}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}

      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 'var(--space-2)' }}>
        {data?.symbols.map((symbol) => (
          <span
            key={symbol}
            style={{
              display: 'inline-flex',
              alignItems: 'center',
              gap: '0.4rem',
              border: '1px solid var(--rule)',
              borderRadius: 3,
              padding: '0.2rem 0.25rem 0.2rem 0.55rem',
              fontSize: '0.8rem',
            }}
          >
            <span className="num" style={{ fontWeight: 600 }}>{symbol}</span>
            <button
              type="button"
              onClick={() => remove.mutate(symbol)}
              aria-label={`Remove ${symbol} from the tape`}
              style={{
                background: 'transparent',
                border: 'none',
                color: 'var(--muted)',
                font: 'inherit',
                cursor: 'pointer',
                lineHeight: 1,
                padding: '0 0.2rem',
              }}
            >
              ✕
            </button>
          </span>
        ))}
        {data?.symbols.length === 0 && (
          <span style={{ color: 'var(--muted)', fontSize: '0.82rem' }}>
            The tape is empty. Add a symbol above.
          </span>
        )}
      </div>
    </div>
  )
}
