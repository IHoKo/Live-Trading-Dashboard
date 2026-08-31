import { useEffect, useRef, useState } from 'react'

/**
 * The signature element from §8.2: a split-flap board, the mechanical kind that
 * hung above trading floors before screens.
 *
 * "On a price change the digits flip rather than fade." So each character is a
 * physical flap: the top half of the outgoing character falls away, then the
 * bottom half of the incoming one swings up. Two stages, hinged at the middle,
 * because that is how the real boards work — a cross-fade would be the generic
 * dashboard behaviour this is explicitly not.
 *
 * §8.2 restraint: this is used on the tape and nowhere else. The positions
 * table only tints.
 */

const FLIP_MS = 260

export function SplitFlapNumber({ value, label }: { value: string; label?: string }) {
  return (
    <span className="flap-row" aria-label={label ?? value} role="text">
      {value.split('').map((char, i) => (
        <Flap key={i} char={char} />
      ))}
    </span>
  )
}

function Flap({ char }: { char: string }) {
  const [shown, setShown] = useState(char)
  const [outgoing, setOutgoing] = useState<string | null>(null)
  const timer = useRef<number | null>(null)

  useEffect(() => {
    if (char === shown) return

    // §8.2 / §8.3: reduced motion swaps to a plain value change.
    if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) {
      setShown(char)
      return
    }

    setOutgoing(shown)
    setShown(char)
    if (timer.current) window.clearTimeout(timer.current)
    timer.current = window.setTimeout(() => setOutgoing(null), FLIP_MS)
    return () => {
      if (timer.current) window.clearTimeout(timer.current)
    }
  }, [char, shown])

  // A separator never flips — only the digits move, as on a real board.
  if (char === '.' || char === ',' || char === ' ') {
    return <span className="flap-sep">{char}</span>
  }

  return (
    <span className="flap" aria-hidden>
      <span className="flap-half flap-top">
        <span>{shown}</span>
      </span>
      <span className="flap-half flap-bottom">
        <span>{shown}</span>
      </span>

      {outgoing !== null && (
        <>
          {/* the outgoing character's top half, falling */}
          <span className="flap-half flap-top flap-leaf-fall">
            <span>{outgoing}</span>
          </span>
          {/* the incoming character's bottom half, swinging up behind it */}
          <span className="flap-half flap-bottom flap-leaf-rise">
            <span>{shown}</span>
          </span>
        </>
      )}
    </span>
  )
}
