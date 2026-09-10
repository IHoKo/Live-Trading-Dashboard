import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

/** Mirrors app/routers/portfolio.py. */
export type Position = {
  symbol: string
  quantity: number
  cost_basis: number
  average_cost: number
  last_price: number | null
  market_value: number | null
  unrealized_pnl: number | null
  unrealized_pct: number | null
  day_change_pct: number | null
  allocation_pct: number | null
}

export type Portfolio = {
  positions: Position[]
  cost_basis_total: number
  market_value_total: number | null
  unrealized_pnl_total: number | null
  realized_pnl_total: number
  priced_symbols: number
  unpriced_symbols: string[]
}

export type Transaction = {
  id: number
  symbol: string
  side: 'BUY' | 'SELL'
  quantity: number
  price: number
  fees: number
  executed_at: string
  note: string | null
  source: string
}

export type NewTransaction = {
  symbol: string
  side: 'BUY' | 'SELL'
  quantity: number
  price: number
  fees?: number
  executed_at?: string
  note?: string
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, {
    ...init,
    headers: init?.body ? { 'Content-Type': 'application/json' } : undefined,
  })
  if (!res.ok) {
    // FastAPI puts the useful part in `detail`; surface it rather than "500".
    let detail = `HTTP ${res.status}`
    try {
      const body = (await res.json()) as { detail?: unknown }
      if (typeof body.detail === 'string') detail = body.detail
    } catch {
      /* non-JSON error body */
    }
    throw new Error(detail)
  }
  return res.status === 204 ? (undefined as T) : ((await res.json()) as T)
}

/**
 * Prices come from the server on each read, and the fetch policy is manual
 * outside market hours — so this does not poll. Refetching is driven by the
 * user: a mutation, or the tape's REFRESH button.
 */
export function usePortfolio() {
  return useQuery({
    queryKey: ['portfolio'],
    queryFn: () => request<Portfolio>('/api/portfolio'),
    refetchOnWindowFocus: false,
    refetchInterval: false,
  })
}

export function useTransactions(limit = 25) {
  return useQuery({
    queryKey: ['transactions', limit],
    queryFn: () =>
      request<{ transactions: Transaction[]; total: number }>(
        `/api/transactions?limit=${limit}`,
      ),
    refetchOnWindowFocus: false,
  })
}

function useLedgerMutation<TVars>(fn: (vars: TVars) => Promise<unknown>) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: fn,
    // Both tables are derived from the same ledger, so any write invalidates both.
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ['portfolio'] })
      void queryClient.invalidateQueries({ queryKey: ['transactions'] })
    },
  })
}

export function useAddTransaction() {
  return useLedgerMutation((tx: NewTransaction) =>
    request<Transaction>('/api/transactions', { method: 'POST', body: JSON.stringify(tx) }),
  )
}

export function useDeleteTransaction() {
  return useLedgerMutation((id: number) =>
    request<void>(`/api/transactions/${id}`, { method: 'DELETE' }),
  )
}

const MONEY = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
})

export function money(value: number | null | undefined): string {
  return value == null ? '—' : MONEY.format(value)
}

export function percent(value: number | null | undefined): string {
  return value == null ? '—' : `${value >= 0 ? '+' : ''}${value.toFixed(2)}%`
}

export function qty(value: number): string {
  return Number.isInteger(value) ? String(value) : value.toFixed(4).replace(/0+$/, '')
}

export type Quote = {
  symbol: string
  price: number
  change: number | null
  change_pct: number | null
  prev_close: number | null
  as_of: number
  age_seconds: number | null
}

/**
 * One symbol's quote, for prefilling the transaction form's price.
 *
 * The price socket already carries every watchlist and held symbol, so this is
 * only the fallback for a symbol being typed for the first time. Kept short-
 * lived: a prefilled price that is minutes old would be quietly wrong, and the
 * form labels its freshness either way (§4).
 */
export function useQuote(symbol: string) {
  return useQuery({
    queryKey: ['quote', symbol],
    queryFn: async () => {
      const body = await request<{ quotes: Quote[]; unavailable: string[] }>(
        `/api/quotes?symbols=${encodeURIComponent(symbol)}`,
      )
      return body.quotes[0] ?? null
    },
    enabled: symbol.length > 0,
    staleTime: 30_000,
    retry: false,
  })
}
