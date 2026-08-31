import { useState, type FormEvent } from 'react'

import {
  money,
  qty,
  useAddTransaction,
  useDeleteTransaction,
  useTransactions,
} from '../hooks/usePortfolio'

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
  const [executedAt, setExecutedAt] = useState('')

  const submit = (event: FormEvent) => {
    event.preventDefault()
    add.mutate(
      {
        symbol,
        side,
        quantity: Number(quantity),
        price: Number(price),
        fees: fees ? Number(fees) : 0,
        ...(executedAt ? { executed_at: new Date(executedAt).toISOString() } : {}),
      },
      {
        onSuccess: () => {
          setSymbol('')
          setQuantity('')
          setPrice('')
          setFees('')
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

        <Field label="Symbol" value={symbol} onChange={setSymbol} placeholder="AAPL" required width="7rem" />
        <Field label="Quantity" value={quantity} onChange={setQuantity} type="number" step="any" min="0" required width="7rem" />
        <Field label="Price" value={price} onChange={setPrice} type="number" step="any" min="0" required width="8rem" />
        <Field label="Fees" value={fees} onChange={setFees} type="number" step="any" min="0" width="6rem" />
        <Field label="Executed" value={executedAt} onChange={setExecutedAt} type="datetime-local" width="13rem" />

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

      {add.error && (
        <p role="alert" style={{ color: 'var(--loss)', margin: 0 }}>
          {add.error.message}
        </p>
      )}
    </form>
  )
}

function Field({
  label,
  value,
  onChange,
  width,
  ...rest
}: {
  label: string
  value: string
  onChange: (v: string) => void
  width: string
  type?: string
  step?: string
  min?: string
  placeholder?: string
  required?: boolean
}) {
  return (
    <label style={{ display: 'grid', gap: 2 }}>
      <span style={{ color: 'var(--muted)', fontSize: '0.7rem', letterSpacing: '0.08em' }}>
        {label.toUpperCase()}
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

  if (!data || data.transactions.length === 0) return null

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
