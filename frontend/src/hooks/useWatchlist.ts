import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

export type WatchlistEntry = { symbol: string; added_at: string; sort_order: number }
export type Watchlist = {
  symbols: string[]
  entries: WatchlistEntry[]
  max_symbols: number
}

export type SymbolMatch = {
  symbol: string
  display_symbol: string
  description: string
  type: string
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, {
    ...init,
    headers: init?.body ? { 'Content-Type': 'application/json' } : undefined,
  })
  if (!res.ok) {
    let detail = `HTTP ${res.status}`
    try {
      const body = (await res.json()) as { detail?: unknown }
      if (typeof body.detail === 'string') detail = body.detail
    } catch {
      /* non-JSON error body */
    }
    throw new Error(detail)
  }
  return (await res.json()) as T
}

/** The symbols the tape follows. Server-owned since it drives what the price
    hub subscribes to upstream. */
export function useWatchlist() {
  return useQuery({
    queryKey: ['watchlist'],
    queryFn: () => request<Watchlist>('/api/watchlist'),
    refetchOnWindowFocus: false,
    staleTime: 30_000,
  })
}

function useWatchlistMutation<T>(fn: (arg: T) => Promise<Watchlist>) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: fn,
    onSuccess: (data) => {
      // Seed the cache from the response so the tape resubscribes immediately
      // rather than after a refetch round-trip.
      queryClient.setQueryData(['watchlist'], data)
    },
  })
}

export function useAddSymbol() {
  return useWatchlistMutation((symbol: string) =>
    request<Watchlist>('/api/watchlist', {
      method: 'POST',
      body: JSON.stringify({ symbol }),
    }),
  )
}

export function useRemoveSymbol() {
  return useWatchlistMutation((symbol: string) =>
    request<Watchlist>(`/api/watchlist/${encodeURIComponent(symbol)}`, { method: 'DELETE' }),
  )
}

/**
 * Symbol lookup for the add box. `/api/search` has existed since Phase 1 and
 * nothing has used it until now.
 *
 * Not cached: it runs while someone types, and a stale suggestion list is
 * worse than an extra request.
 */
export function useSymbolSearch(query: string) {
  return useQuery({
    queryKey: ['symbol-search', query],
    queryFn: () =>
      request<{ query: string; results: SymbolMatch[] }>(
        `/api/search?q=${encodeURIComponent(query)}`,
      ),
    enabled: query.trim().length >= 2,
    staleTime: 0,
    retry: false,
  })
}
