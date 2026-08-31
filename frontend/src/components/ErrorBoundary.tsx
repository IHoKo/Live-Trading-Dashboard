import { Component, type ErrorInfo, type ReactNode } from 'react'

/**
 * §10 Phase 6: error boundaries. A render crash in one panel should not blank
 * the whole dashboard — losing the portfolio because the chat threw is a much
 * worse failure than the one that caused it.
 */
type Props = { children: ReactNode; name: string }
type State = { error: Error | null }

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error(`[${this.props.name}]`, error, info.componentStack)
  }

  render() {
    if (!this.state.error) return this.props.children
    return (
      <div
        role="alert"
        style={{
          border: '1px solid var(--loss)',
          borderRadius: 6,
          padding: 'var(--space-3)',
          color: 'var(--muted)',
          fontSize: '0.85rem',
        }}
      >
        <p style={{ margin: 0, color: 'var(--loss)' }}>
          The {this.props.name} panel hit an error.
        </p>
        <p style={{ margin: 'var(--space-2) 0 0' }}>
          The rest of the dashboard is unaffected.{' '}
          <button
            type="button"
            onClick={() => this.setState({ error: null })}
            style={{
              background: 'transparent',
              border: 'none',
              color: 'var(--brass)',
              font: 'inherit',
              textDecoration: 'underline',
              cursor: 'pointer',
              padding: 0,
            }}
          >
            Try again
          </button>
        </p>
      </div>
    )
  }
}
