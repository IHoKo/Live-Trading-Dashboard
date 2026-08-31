import { useState, type FormEvent } from 'react'

/**
 * §11: single-user passphrase. The server sets an HttpOnly cookie, so there is
 * deliberately no token handling here — nothing for a script to read.
 */
export function Login({ onSuccess }: { onSuccess: () => void }) {
  const [passphrase, setPassphrase] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      const res = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ passphrase }),
      })
      if (res.ok) {
        onSuccess()
        return
      }
      const body = (await res.json().catch(() => ({}))) as { detail?: string }
      setError(body.detail ?? `HTTP ${res.status}`)
    } catch {
      setError('Could not reach the server.')
    } finally {
      setBusy(false)
      setPassphrase('')
    }
  }

  return (
    <main
      style={{
        minHeight: '100dvh',
        display: 'grid',
        placeItems: 'center',
        padding: 'var(--space-5)',
      }}
    >
      <form onSubmit={submit} style={{ display: 'grid', gap: 'var(--space-3)', width: 'min(22rem, 100%)' }}>
        <h1
          style={{
            fontFamily: 'var(--font-display)',
            fontSize: 'var(--step-3)',
            letterSpacing: '-0.03em',
            margin: 0,
          }}
        >
          TICKER
        </h1>
        <label style={{ display: 'grid', gap: 'var(--space-1)' }}>
          <span style={{ color: 'var(--muted)', fontSize: '0.7rem', letterSpacing: '0.1em' }}>
            PASSPHRASE
          </span>
          <input
            type="password"
            value={passphrase}
            onChange={(e) => setPassphrase(e.target.value)}
            autoFocus
            autoComplete="current-password"
            style={{
              padding: 'var(--space-3)',
              background: 'var(--slate)',
              border: '1px solid var(--rule)',
              color: 'var(--paper)',
              font: 'inherit',
            }}
          />
        </label>
        <button
          type="submit"
          disabled={busy || !passphrase}
          style={{
            padding: 'var(--space-3)',
            background: busy || !passphrase ? 'transparent' : 'var(--brass)',
            border: '1px solid var(--brass)',
            color: busy || !passphrase ? 'var(--muted)' : 'var(--ink)',
            font: 'inherit',
            letterSpacing: '0.1em',
            cursor: busy || !passphrase ? 'default' : 'pointer',
          }}
        >
          {busy ? 'CHECKING…' : 'ENTER'}
        </button>
        {error && (
          <p role="alert" style={{ color: 'var(--loss)', margin: 0, fontSize: '0.85rem' }}>
            {error}
          </p>
        )}
      </form>
    </main>
  )
}
