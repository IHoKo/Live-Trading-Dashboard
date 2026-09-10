import { useEffect, useState, type FormEvent, type ReactNode } from 'react'

import {
  money,
  qty,
  useAddTransaction,
  useDeleteTransaction,
  useQuote,
  useTransactions,
} from '../hooks/usePortfolio'
import { freshness, selectTick, usePriceStore, type Status, type Tick } from '../store/prices'

/** Debounce before hitting /api/quotes, matching the watchlist add box. */
const SYMBOL_DEBOUNCE_MS = 220

/**
 * Add and remove, per the Phase 3 checklist.
 *
 * There is deliberately no edit form. `transactions` is append-only (§5) — a
 * correction is a delete plus a new row, which is exactly what these two
 * controls give you. An edit endpoint would mean mutating the source of truth.
 */
export function TransactionForm() {
  const add = useAddTransaction()
  const [side, setSide] = useState<'BUY' | 'SELL'>('BUY')
  const [symbol, setSymbol] = useState('')
  const [quantity, setQuantity] = useState('')
  const [price, setPrice] = useState('')
  const [fees, setFees] = useState('')

  // Once the price has been typed in, an arriving quote must not overwrite it.
  // Changing the symbol clears the flag: the old price belongs to the old
  // symbol, so re-prefilling is the right move.
  const [priceEdited, setPriceEdited] = useState(false)

  const normalised = symbol.trim().toUpperCase()
  const [debounced, setDebounced] = useState('')
  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(normalised), SYMBOL_DEBOUNCE_MS)
    return () => window.clearTimeout(timer)
  }, [normalised])

  // The socket already carries watchlist and held symbols, so most of the time
  // this costs nothing. The REST quote is the fallback for a symbol typed for
  // the first time.
  const tick = usePriceStore(selectTick(normalised))
  const status = usePriceStore((s) => s.status)
  const { data: quote } = useQuote(tick ? '' : debounced)

  const quoteTick: Tick | undefined =
    tick ??
    // The debounce means `quote` can still describe the previous symbol for a
    // moment. Prefilling from it would put one symbol's price against another.
    (quote && quote.symbol === normalised
      ? {
          s: quote.symbol,
          p: quote.price,
          t: quote.as_of * 1000,
          dp: quote.change_pct,
          stale_since: null,
        }
      : undefined)

  const suggested = quoteTick?.p ?? null

  // Fill an empty price field, then leave it alone. A live tick arriving
  // mid-entry must not move the number under the cursor — this is an execution
  // price being recorded, not a display of the market. Clearing the field asks
  // for a fresh one.
  useEffect(() => {
    if (suggested !== null && !priceEdited && price === '') setPrice(String(suggested))
  }, [suggested, priceEdited, price])

  const changeSymbol = (value: string) => {
    setSymbol(value)
    setPriceEdited(false)
    setPrice('')
  }

  // The point of showing this: quantity is shares, price is per share, and the
  // two multiply out to what actually left the account.
  const total =
    Number(quantity) > 0 && price !== '' && Number.isFinite(Number(price))
      ? Number(quantity) * Number(price) + (fees ? Number(fees) : 0)
      : null

  const submit = (event: FormEvent) => {
    event.preventDefault()
    add.mutate(
      {
        symbol,
        side,
        quantity: Number(quantity),
        price: Number(price),
        fees: fees ? Number(fees) : 0,
      },
      {
        onSuccess: () => {
          setSymbol('')
          setQuantity('')
          setPrice('')
          setFees('')
          setPriceEdited(false)
        },
      },
    )
  }

  return (
    <form onSubmit={submit} style={{ display: 'grid', gap: 'var(--space-3)' }}>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 'var(--space-2)', alignItems: 'end' }}>
        <div role="group" aria-label="Side" style={{ display: 'flex' }}>
          {(['BUY', 'SELL'] as const).map((option) => (
            <button
              key={option}
              type="button"
              onClick={() => setSide(option)}
              aria-pressed={side === option}
              style={{
                padding: 'var(--space-2) var(--space-3)',
                border: '1px solid var(--rule)',
                background: side === option ? 'var(--brass)' : 'transparent',
                color: side === option ? 'var(--ink)' : 'var(--paper)',
                font: 'inherit',
                cursor: 'pointer',
              }}
            >
              {option}
            </button>
          ))}
        </div>

        <Field label="Symbol" value={symbol} onChange={changeSymbol} placeholder="AAPL" required width="7rem" />
        <Field label="Shares" value={quantity} onChange={setQuantity} type="number" step="any" min="0" required width="7rem" />
        <Field
          label="Price / share"
          value={price}
          onChange={(value) => {
            setPriceEdited(true)
            setPrice(value)
          }}
          type="number"
          step="any"
          min="0"
          required
          width="8rem"
          hint={suggested === null ? undefined : <PriceHint tick={quoteTick} status={status} />}
        />
        <Field label="Fees" value={fees} onChange={setFees} type="number" step="any" min="0" width="6rem" />

        <button
          type="submit"
          disabled={add.isPending}
          style={{
            padding: 'var(--space-2) var(--space-4)',
            border: '1px solid var(--brass)',
            background: 'transparent',
            color: 'var(--brass)',
            font: 'inherit',
            cursor: add.isPending ? 'default' : 'pointer',
          }}
        >
          {add.isPending ? 'RECORDING…' : 'RECORD'}
        </button>
      </div>

      {total !== null && (
        <p className="num" style={{ margin: 0, color: 'var(--muted)', fontSize: '0.75rem' }}>
          {qty(Number(quantity))} × {money(Number(price))}
          {fees ? ` + ${money(Number(fees))} fees` : ''} = {money(total)}
        </p>
      )}

      {add.error && (
        <p role="alert" style={{ color: 'var(--loss)', margin: 0 }}>
          {add.error.message}
        </p>
      )}
    </form>
  )
}

/**
 * §4: never render a stale number as if it were live. A prefilled price is a
 * market price, so it carries the same LIVE / DELAYED / CLOSED / STALE label
 * the tape uses — the user is being handed a number and should see how old it
 * is before recording it as an execution price.
 */
function PriceHint({ tick, status }: { tick: Tick | undefined; status: Status | null }) {
  const { label, tone } = freshness(tick, status)
  const color = tone === 'live' ? 'var(--gain)' : tone === 'stale' ? 'var(--loss)' : 'var(--muted)'
  return <span style={{ color }}>{label}</span>
}

function Field({
  label,
  value,
  onChange,
  width,
  hint,
  ...rest
}: {
  label: string
  value: string
  onChange: (v: string) => void
  width: string
  /** Rendered beside the label — used for the price field's freshness tag. */
  hint?: ReactNode
  type?: string
  step?: string
  min?: string
  placeholder?: string
  required?: boolean
}) {
  return (
    <label style={{ display: 'grid', gap: 2 }}>
      <span
        style={{
          display: 'flex',
          gap: 'var(--space-2)',
          color: 'var(--muted)',
          fontSize: '0.7rem',
          letterSpacing: '0.08em',
        }}
      >
        {label.toUpperCase()}
        {hint}
      </span>
      <input
        {...rest}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="num"
        style={{
          width,
          padding: 'var(--space-2)',
          background: 'var(--ink)',
          border: '1px solid var(--rule)',
          color: 'var(--paper)',
          font: 'inherit',
        }}
      />
    </label>
  )
}

/** Recent history, with remove. Deleting reverses and rebuilds the lots (§6). */
export function TransactionHistory() {
  const { data } = useTransactions()
  const remove = useDeleteTransaction()

  if (!data || data.transactions.length === 0) {
    return (
      <p style={{ color: 'var(--muted)', padding: 'var(--space-4) var(--space-3)', margin: 0, fontSize: '0.85rem' }}>
        Nothing recorded yet.
      </p>
    )
  }

  return (
    <div style={{ overflowX: 'auto' }}>
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.8rem' }}>
        <tbody>
          {data.transactions.map((tx) => (
            <tr key={tx.id} style={{ borderTop: '1px solid var(--rule)' }}>
              <td style={{ padding: 'var(--space-2)', color: 'var(--muted)' }}>
                {tx.executed_at.slice(0, 10)}
              </td>
              <td style={{ padding: 'var(--space-2)', color: tx.side === 'BUY' ? 'var(--gain)' : 'var(--loss)' }}>
                {tx.side}
              </td>
              <td style={{ padding: 'var(--space-2)', fontWeight: 600 }}>{tx.symbol}</td>
              <td className="num" style={{ padding: 'var(--space-2)', textAlign: 'right' }}>
                {qty(tx.quantity)}
              </td>
              <td className="num" style={{ padding: 'var(--space-2)', textAlign: 'right' }}>
                {money(tx.price)}
              </td>
              <td style={{ padding: 'var(--space-2)', textAlign: 'right' }}>
                <button
                  type="button"
                  onClick={() => remove.mutate(tx.id)}
                  disabled={remove.isPending}
                  aria-label={`Remove ${tx.side} ${tx.quantity} ${tx.symbol}`}
                  style={{
                    background: 'transparent',
                    border: 'none',
                    color: 'var(--muted)',
                    font: 'inherit',
                    cursor: 'pointer',
                  }}
                >
                  ✕
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {remove.error && (
        <p role="alert" style={{ color: 'var(--loss)' }}>
          {remove.error.message}
        </p>
      )}
    </div>
  )
}
