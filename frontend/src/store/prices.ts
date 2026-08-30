import { create } from 'zustand'

/** Server -> client messages, exactly the shapes in plan.md §6. */
export type Tick = {
  s: string
  p: number
  t: number
  dp: number | null
  stale_since: number | null
}

export type FeedState = 'not_started' | 'live' | 'polling' | 'down' | 'idle'

export type Status = {
  provider: string
  state: FeedState
  market_open: boolean
  /** Unix seconds of the last manual/on-demand fetch, or null. */
  last_refresh_at: number | null
}

export type ConnectionState = 'connecting' | 'open' | 'closed'

type PriceStore = {
  ticks: Record<string, Tick>
  status: Status | null
  connection: ConnectionState
  applyTick: (tick: Tick) => void
  setStatus: (status: Status) => void
  setConnection: (connection: ConnectionState) => void
}

/**
 * Keyed by symbol so a component can select exactly its own row (§8.3).
 * `ticks` gets a new identity on every update, but `ticks[symbol]` keeps its
 * reference for untouched symbols — so one print re-renders one row, not the
 * whole table.
 */
export const usePriceStore = create<PriceStore>()((set) => ({
  ticks: {},
  status: null,
  connection: 'connecting',
  applyTick: (tick) => set((s) => ({ ticks: { ...s.ticks, [tick.s]: tick } })),
  setStatus: (status) => set({ status }),
  setConnection: (connection) => set({ connection }),
}))

export const selectTick = (symbol: string) => (s: PriceStore) => s.ticks[symbol]

/** Honesty labels from §4. Never render a stale number as if it were live. */
export type Freshness =
  | { label: 'LIVE'; tone: 'live' }
  | { label: 'DELAYED'; tone: 'muted' }
  | { label: 'CLOSED'; tone: 'muted' }
  | { label: `STALE ${string}`; tone: 'stale' }
  | { label: '—'; tone: 'muted' }

/** Anything older than this during a live feed is not "live" any more. */
const LIVE_MAX_AGE_MS = 60_000

export function freshness(
  tick: Tick | undefined,
  status: Status | null,
  now: number = Date.now(),
): Freshness {
  if (!tick) return { label: '—', tone: 'muted' }

  const ageMs = Math.max(0, now - tick.t)

  // An explicit stale marker from the server always wins.
  if (tick.stale_since !== null || status?.state === 'down') {
    return { label: `STALE ${humanAge(ageMs)}`, tone: 'stale' }
  }
  if (status && !status.market_open) return { label: 'CLOSED', tone: 'muted' }
  if (status?.state === 'live' && ageMs < LIVE_MAX_AGE_MS) {
    return { label: 'LIVE', tone: 'live' }
  }
  if (status?.state === 'polling') return { label: 'DELAYED', tone: 'muted' }
  return { label: `STALE ${humanAge(ageMs)}`, tone: 'stale' }
}

function humanAge(ms: number): string {
  const mins = Math.floor(ms / 60_000)
  if (mins < 1) return `${Math.floor(ms / 1000)}s`
  if (mins < 60) return `${mins}m`
  const hours = Math.floor(mins / 60)
  return hours < 24 ? `${hours}h` : `${Math.floor(hours / 24)}d`
}
