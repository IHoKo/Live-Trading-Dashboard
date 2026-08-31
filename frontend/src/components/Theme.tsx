import { useEffect, useState } from 'react'

/**
 * §8.2: "Dark mode is the default; the light variant inverts ink/paper and
 * drops the brass a shade for contrast." The token overrides live in
 * tokens.css; this just stamps `data-theme` on the root.
 */
type Theme = 'dark' | 'light'

function initial(): Theme {
  try {
    const stored = localStorage.getItem('ticker-theme')
    if (stored === 'light' || stored === 'dark') return stored
  } catch {
    /* private browsing, blocked storage */
  }
  return 'dark'
}

export function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>(initial)

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme)
    try {
      localStorage.setItem('ticker-theme', theme)
    } catch {
      /* nothing to do; the attribute is already applied */
    }
  }, [theme])

  return (
    <button
      type="button"
      onClick={() => setTheme((t) => (t === 'dark' ? 'light' : 'dark'))}
      aria-label={`Switch to ${theme === 'dark' ? 'light' : 'dark'} board`}
      title={`Switch to ${theme === 'dark' ? 'light' : 'dark'} board`}
      style={{
        background: 'transparent',
        border: '1px solid var(--rule)',
        borderRadius: 3,
        color: 'var(--muted)',
        font: 'inherit',
        fontSize: '0.7rem',
        letterSpacing: '0.1em',
        padding: '0.25rem 0.5rem',
        cursor: 'pointer',
      }}
    >
      {theme === 'dark' ? 'LIGHT' : 'DARK'}
    </button>
  )
}
