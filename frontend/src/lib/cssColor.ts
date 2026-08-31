/**
 * Resolve a CSS custom property to a real colour.
 *
 * `getComputedStyle(root).getPropertyValue('--rule')` returns the *unresolved*
 * token text. For `--gain: #2F7D6E` that happens to be a usable colour; for
 *
 *     --rule: color-mix(in srgb, var(--paper) 14%, var(--ink));
 *
 * it returns that string verbatim, `var()` calls and all. Handing it to a
 * canvas library throws — which is exactly how the chart panel died while the
 * rest of the dashboard looked fine.
 *
 * Assigning it to a real colour property and reading it back makes the browser
 * resolve it to `rgb(...)` for us.
 */
export function cssColor(name: string, fallback: string): string {
  if (typeof document === 'undefined') return fallback
  const probe = document.createElement('span')
  probe.style.color = `var(${name}, ${fallback})`
  probe.style.position = 'absolute'
  probe.style.visibility = 'hidden'
  document.body.appendChild(probe)
  const resolved = getComputedStyle(probe).color
  probe.remove()
  return resolved || fallback
}

/** Plain token text — correct for fonts, which never need resolving. */
export function cssToken(name: string, fallback: string): string {
  if (typeof document === 'undefined') return fallback
  return (
    getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback
  )
}

/**
 * Apply an alpha to a resolved colour.
 *
 * `cssColor` returns `rgb(r, g, b)`, so the old trick of appending hex digits
 * (`${brass}44`) produces `rgb(201, 162, 39)44` — silently invalid. Canvas
 * fills then fall back to black or throw, depending on the library.
 */
export function withAlpha(color: string, alpha: number): string {
  const rgb = color.match(/rgba?\(([^)]+)\)/)
  if (rgb) {
    const [r, g, b] = rgb[1].split(/[,/\s]+/).filter(Boolean).slice(0, 3)
    return `rgba(${r}, ${g}, ${b}, ${alpha})`
  }
  const hex = color.trim().match(/^#([0-9a-f]{6})$/i)
  if (hex) {
    const n = parseInt(hex[1], 16)
    return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`
  }
  return color
}
