/**
 * Resolve a CSS custom property to a colour a canvas library can actually parse.
 *
 * Two traps, both hit in production:
 *
 * 1. `getComputedStyle(root).getPropertyValue('--rule')` returns the
 *    *unresolved* token text. For `--gain: #2F7D6E` that is usable; for
 *    `--rule: color-mix(in srgb, var(--paper) 14%, var(--ink))` it hands back
 *    that string, `var()` calls and all.
 *
 * 2. Resolving it is not enough. A `color-mix()` computes to **CSS Color 4**
 *    syntax — `color(srgb 0.553 0.552 0.548)` — and lightweight-charts'
 *    parser predates that: it understands `rgb()`, `rgba()`, hex and named
 *    colours only, and throws "Cannot parse color" on anything else.
 *
 * So: resolve through a probe element, then normalise to legacy `rgb()`/
 * `rgba()`. Anything handed to a canvas must go through here.
 */

function toByte(value: string): number {
  return Math.round(Math.min(1, Math.max(0, Number.parseFloat(value))) * 255)
}

/** `color(srgb r g b[ / a])` -> `rgb()`/`rgba()`. Returns null for other input. */
function fromColorSyntax(value: string): string | null {
  const match = value
    .trim()
    .match(/^color\(\s*srgb\s+([\d.eE+-]+)\s+([\d.eE+-]+)\s+([\d.eE+-]+)(?:\s*\/\s*([\d.eE+-]+))?\s*\)$/)
  if (!match) return null
  const [, r, g, b, a] = match
  const rgb = `${toByte(r)}, ${toByte(g)}, ${toByte(b)}`
  return a === undefined ? `rgb(${rgb})` : `rgba(${rgb}, ${Number.parseFloat(a)})`
}

/** Normalise any resolved colour string to something a canvas parser accepts. */
export function normaliseColor(value: string, fallback: string): string {
  const trimmed = value.trim()
  if (!trimmed) return fallback
  return fromColorSyntax(trimmed) ?? trimmed
}

export function cssColor(name: string, fallback: string): string {
  if (typeof document === 'undefined') return fallback
  const probe = document.createElement('span')
  probe.style.color = `var(${name}, ${fallback})`
  probe.style.position = 'absolute'
  probe.style.visibility = 'hidden'
  document.body.appendChild(probe)
  const resolved = getComputedStyle(probe).color
  probe.remove()
  return normaliseColor(resolved, fallback)
}

/** Plain token text — correct for fonts, which never need resolving. */
export function cssToken(name: string, fallback: string): string {
  if (typeof document === 'undefined') return fallback
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback
}

/**
 * Apply an alpha to a resolved colour. `cssColor` returns `rgb(...)`, so the
 * old trick of appending hex digits (`${brass}44`) produced
 * `rgb(201, 162, 39)44` — silently invalid.
 */
export function withAlpha(color: string, alpha: number): string {
  const normalised = normaliseColor(color, color)

  const rgb = normalised.match(/rgba?\(([^)]+)\)/)
  if (rgb) {
    const [r, g, b] = rgb[1].split(/[,/\s]+/).filter(Boolean).slice(0, 3)
    return `rgba(${r}, ${g}, ${b}, ${alpha})`
  }

  const hex = normalised.match(/^#([0-9a-f]{6})$/i)
  if (hex) {
    const n = Number.parseInt(hex[1], 16)
    return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`
  }

  return normalised
}
