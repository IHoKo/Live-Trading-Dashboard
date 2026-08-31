import { useEffect, useRef, useState, type FormEvent } from 'react'

import { useChat, type ChatTurn, type PendingAction } from '../hooks/useChat'

/**
 * The chat panel from §8.1.
 *
 * Two things here are requirements, not decoration:
 * - every assistant answer carries a disclaimer line (§11)
 * - the confirm card is keyboard-reachable with a visible focus ring (§8.3),
 *   because it is the control that writes to the ledger
 */
export function Chat() {
  const { turns, streaming, error, send, resolve, clear } = useChat()
  const [draft, setDraft] = useState('')
  const endRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [turns])

  const submit = (event: FormEvent) => {
    event.preventDefault()
    const message = draft.trim()
    if (!message) return
    setDraft('')
    void send(message)
  }

  return (
    <section
      style={{
        display: 'flex',
        flexDirection: 'column',
        border: '1px solid var(--rule)',
        borderRadius: 6,
        background: 'var(--slate)',
        minHeight: '30rem',
        height: '100%',
      }}
    >
      <header
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          padding: 'var(--space-2) var(--space-3)',
          borderBottom: '1px solid var(--rule)',
        }}
      >
        <h2 style={{ margin: 0, fontSize: '0.7rem', letterSpacing: '0.12em', color: 'var(--muted)' }}>
          ANALYST
        </h2>
        {turns.length > 0 && (
          <button
            type="button"
            onClick={() => void clear()}
            style={{
              background: 'transparent',
              border: 'none',
              color: 'var(--muted)',
              font: 'inherit',
              fontSize: '0.7rem',
              cursor: 'pointer',
            }}
          >
            CLEAR
          </button>
        )}
      </header>

      <div style={{ flex: 1, overflowY: 'auto', padding: 'var(--space-3)', display: 'grid', gap: 'var(--space-3)' }}>
        {turns.length === 0 && (
          <p style={{ color: 'var(--muted)', margin: 0, fontSize: '0.85rem' }}>
            Ask how your portfolio is doing, why a holding moved, or say something like
            “add 10 shares of MSFT at 412.50”. Every change is confirmed by you first.
          </p>
        )}

        {turns.map((turn, i) => (
          <Turn key={i} turn={turn} onResolve={resolve} streaming={streaming && i === turns.length - 1} />
        ))}

        {error && (
          <p role="alert" style={{ color: 'var(--loss)', margin: 0, fontSize: '0.85rem' }}>
            {error}
          </p>
        )}
        <div ref={endRef} />
      </div>

      <form onSubmit={submit} style={{ display: 'flex', borderTop: '1px solid var(--rule)' }}>
        <label htmlFor="chat-input" className="sr-only" style={{ position: 'absolute', left: -9999 }}>
          Message the analyst
        </label>
        <input
          id="chat-input"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder={streaming ? 'Thinking…' : 'Ask about your portfolio…'}
          disabled={streaming}
          style={{
            flex: 1,
            padding: 'var(--space-3)',
            background: 'transparent',
            border: 'none',
            color: 'var(--paper)',
            font: 'inherit',
          }}
        />
        <button
          type="submit"
          disabled={streaming || !draft.trim()}
          style={{
            padding: '0 var(--space-4)',
            background: 'transparent',
            border: 'none',
            borderLeft: '1px solid var(--rule)',
            color: streaming || !draft.trim() ? 'var(--muted)' : 'var(--brass)',
            font: 'inherit',
            fontSize: '0.7rem',
            letterSpacing: '0.1em',
            cursor: streaming || !draft.trim() ? 'default' : 'pointer',
          }}
        >
          SEND
        </button>
      </form>
    </section>
  )
}

function Turn({
  turn,
  onResolve,
  streaming,
}: {
  turn: ChatTurn
  onResolve: (id: string, approved: boolean) => Promise<void>
  streaming: boolean
}) {
  if (turn.role === 'user') {
    return (
      <p
        style={{
          margin: 0,
          padding: 'var(--space-2) var(--space-3)',
          background: 'color-mix(in srgb, var(--paper) 8%, transparent)',
          borderRadius: 4,
          fontSize: '0.9rem',
        }}
      >
        {turn.text}
      </p>
    )
  }

  return (
    <div style={{ display: 'grid', gap: 'var(--space-2)' }}>
      {turn.tools.length > 0 && (
        <p style={{ margin: 0, color: 'var(--muted)', fontSize: '0.7rem' }}>
          {[...new Set(turn.tools)].join(' · ')}
        </p>
      )}

      {turn.text && (
        <p style={{ margin: 0, fontSize: '0.9rem', whiteSpace: 'pre-wrap', lineHeight: 1.5 }}>
          {turn.text}
          {streaming && <span aria-hidden> ▍</span>}
        </p>
      )}

      {turn.citations.length > 0 && (
        <ul style={{ margin: 0, paddingLeft: '1rem', fontSize: '0.75rem' }}>
          {turn.citations.map((citation) => (
            <li key={citation.url}>
              <a
                href={citation.url}
                target="_blank"
                rel="noopener noreferrer"
                style={{ color: 'var(--brass)' }}
              >
                {citation.title}
              </a>
            </li>
          ))}
        </ul>
      )}

      {turn.pending && <ConfirmCard action={turn.pending} resolution={turn.resolution} onResolve={onResolve} />}

      {turn.text && !streaming && (
        // §11: a disclaimer line under every AI answer, not just in the footer.
        <p style={{ margin: 0, color: 'var(--muted)', fontSize: '0.7rem' }}>
          Not investment advice. Verify before acting.
        </p>
      )}
    </div>
  )
}

function ConfirmCard({
  action,
  resolution,
  onResolve,
}: {
  action: PendingAction
  resolution?: 'recorded' | 'cancelled' | 'expired'
  onResolve: (id: string, approved: boolean) => Promise<void>
}) {
  const verb = action.kind === 'add' ? 'Buy' : 'Sell'

  if (resolution) {
    return (
      <p
        style={{
          margin: 0,
          fontSize: '0.8rem',
          color: resolution === 'recorded' ? 'var(--gain)' : 'var(--muted)',
        }}
      >
        {resolution === 'recorded'
          ? `Recorded: ${verb.toLowerCase()} ${action.quantity} ${action.symbol}.`
          : resolution === 'cancelled'
            ? 'Cancelled — nothing was recorded.'
            : 'This confirmation expired.'}
      </p>
    )
  }

  return (
    <div
      style={{
        border: '1px solid var(--brass)',
        borderRadius: 4,
        padding: 'var(--space-3)',
        display: 'grid',
        gap: 'var(--space-2)',
        background: 'color-mix(in srgb, var(--brass) 8%, transparent)',
      }}
    >
      <p style={{ margin: 0, fontWeight: 600 }}>
        {verb} <span className="num">{action.quantity}</span> {action.symbol} @{' '}
        <span className="num">{action.price.toFixed(2)}</span>
      </p>
      <p style={{ margin: 0, color: 'var(--muted)', fontSize: '0.75rem' }}>
        Total <span className="num">{action.total.toFixed(2)}</span>
        {action.price_source === 'quote' && ' · price filled from the current quote'}
      </p>
      <div style={{ display: 'flex', gap: 'var(--space-2)' }}>
        <CardButton primary onClick={() => void onResolve(action.action_id, true)}>
          Confirm
        </CardButton>
        <CardButton onClick={() => void onResolve(action.action_id, false)}>Cancel</CardButton>
      </div>
    </div>
  )
}

function CardButton({
  children,
  primary,
  onClick,
}: {
  children: React.ReactNode
  primary?: boolean
  onClick: () => void
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      style={{
        padding: 'var(--space-2) var(--space-3)',
        border: `1px solid ${primary ? 'var(--brass)' : 'var(--rule)'}`,
        background: primary ? 'var(--brass)' : 'transparent',
        color: primary ? 'var(--ink)' : 'var(--paper)',
        font: 'inherit',
        fontSize: '0.8rem',
        cursor: 'pointer',
      }}
    >
      {children}
    </button>
  )
}
