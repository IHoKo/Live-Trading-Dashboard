import { useEffect, useRef, useState } from 'react'

/**
 * The signature element from §8.2: a split-flap board, the mechanical kind that
 * hung above trading floors before screens.
 *
 * The first version clipped each character into two halves and relied on that
 * geometry being right for the digit to be readable. It wasn't, and the price
 * was unreadable — a design where legibility depends on an animation is the
 * wrong design.
 *
 * So the character is now always rendered whole and legible. The flip is an
 * overlay on top of it: a leaf carrying the outgoing character folds down over
 * the incoming one and is removed. If the animation misbehaves, or is disabled
 * for reduced motion, you still just see the number.
 */

const FLIP_MS = 280

export function SplitFlapNumber({ value, label }: { value: string; label?: string }) {
  return (
    <span className="flap-row" aria-label={label ?? value} role="img">
      {value.split('').map((char, i) => (
        <Flap key={i} char={char} />
      ))}
    </span>
  )
}

function Flap({ char }: { char: string }) {
  const [shown, setShown] = useState(char)
  const [leaf, setLeaf] = useState<string | null>(null)
  const timer = useRef<number | null>(null)

  useEffect(() => {
    if (char === shown) return
    const previous = shown
    setShown(char)

    // §8.2 / §8.3: reduced motion is a plain value change.
    if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) return

    setLeaf(previous)
    if (timer.current) window.clearTimeout(timer.current)
    timer.current = window.setTimeout(() => setLeaf(null), FLIP_MS)
  }, [char, shown])

  useEffect(
    () => () => {
      if (timer.current) window.clearTimeout(timer.current)
    },
    [],
  )

  if (char === '.' || char === ',') {
    return <span className="flap-sep">{char}</span>
  }

  return (
    <span className="flap">
      {/* Always whole, always readable. */}
      <span className="flap-face">{shown}</span>
      {/* The outgoing character, folding away over the top. */}
      {leaf !== null && (
        <span className="flap-leaf" aria-hidden>
          <span className="flap-leaf-face">{leaf}</span>
        </span>
      )}
    </span>
  )
}
