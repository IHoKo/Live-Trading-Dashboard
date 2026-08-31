import { useQuery } from '@tanstack/react-query'
import {
  ColorType,
  createChart,
  type IChartApi,
  type ISeriesApi,
  type UTCTimestamp,
} from 'lightweight-charts'
import { useEffect, useRef, useState } from 'react'

import { money } from '../hooks/usePortfolio'

/**
 * §8.1 chart panel with the §6 range switcher.
 *
 * Styled from the §8.2 tokens rather than lightweight-charts' defaults, so the
 * chart belongs to the same board as everything else — its stock look is the
 * generic finance-dashboard palette this design is explicitly avoiding.
 */

const RANGES = ['1D', '5D', '1M', '6M', '1Y', '5Y'] as const
type Range = (typeof RANGES)[number]

type Candle = {
  time: number
  open: number
  high: number
  low: number
  close: number
  volume: number
}

function token(name: string, fallback: string): string {
  if (typeof window === 'undefined') return fallback
  return (
    getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback
  )
}

export function Chart({ symbol }: { symbol: string }) {
  const [range, setRange] = useState<Range>('1M')

  const { data, error, isLoading } = useQuery({
    queryKey: ['candles', symbol, range],
    queryFn: async () => {
      const res = await fetch(`/api/candles/${symbol}?range=${range}`)
      if (!res.ok) {
        const body = (await res.json().catch(() => ({}))) as { detail?: string }
        throw new Error(body.detail ?? `HTTP ${res.status}`)
      }
      return (await res.json()) as { candles: Candle[] }
    },
    retry: false,
    refetchOnWindowFocus: false,
  })

  return (
    <div style={{ display: 'grid', gap: 'var(--space-3)' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-3)' }}>
        <span style={{ fontWeight: 600, letterSpacing: '0.04em' }}>{symbol}</span>
        <div role="group" aria-label="Chart range" style={{ display: 'flex', gap: 1 }}>
          {RANGES.map((option) => (
            <button
              key={option}
              type="button"
              onClick={() => setRange(option)}
              aria-pressed={range === option}
              style={{
                padding: '0.2rem 0.5rem',
                border: '1px solid var(--rule)',
                background: range === option ? 'var(--brass)' : 'transparent',
                color: range === option ? 'var(--ink)' : 'var(--muted)',
                font: 'inherit',
                fontSize: '0.7rem',
                letterSpacing: '0.06em',
                cursor: 'pointer',
              }}
            >
              {option}
            </button>
          ))}
        </div>
      </div>

      {error ? (
        <Notice tone="loss">{error.message}</Notice>
      ) : isLoading ? (
        <div style={{ height: 260, background: 'var(--slate)', borderRadius: 4 }} />
      ) : !data?.candles.length ? (
        <Notice>No bars for {symbol} over {range}.</Notice>
      ) : (
        <CandlePanel candles={data.candles} />
      )}
    </div>
  )
}

function CandlePanel({ candles }: { candles: Candle[] }) {
  const host = useRef<HTMLDivElement>(null)
  const chart = useRef<IChartApi | null>(null)
  const series = useRef<ISeriesApi<'Candlestick'> | null>(null)

  useEffect(() => {
    if (!host.current) return

    const instance = createChart(host.current, {
      height: 260,
      layout: {
        background: { type: ColorType.Solid, color: 'transparent' },
        textColor: token('--muted', '#8a8f98'),
        fontFamily: token('--font-mono', 'monospace'),
      },
      grid: {
        vertLines: { color: token('--rule', '#222') },
        horzLines: { color: token('--rule', '#222') },
      },
      rightPriceScale: { borderColor: token('--rule', '#222') },
      timeScale: { borderColor: token('--rule', '#222'), timeVisible: false },
      crosshair: { mode: 0 },
      handleScale: false,
      handleScroll: false,
    })

    const gain = token('--gain', '#2F7D6E')
    const loss = token('--loss', '#A8442F')
    series.current = instance.addCandlestickSeries({
      upColor: gain,
      downColor: loss,
      borderUpColor: gain,
      borderDownColor: loss,
      wickUpColor: gain,
      wickDownColor: loss,
    })
    chart.current = instance

    const resize = () => instance.applyOptions({ width: host.current?.clientWidth ?? 0 })
    resize()
    window.addEventListener('resize', resize)

    return () => {
      window.removeEventListener('resize', resize)
      instance.remove()
      chart.current = null
      series.current = null
    }
  }, [])

  useEffect(() => {
    series.current?.setData(
      candles.map((c) => ({
        time: c.time as UTCTimestamp,
        open: c.open,
        high: c.high,
        low: c.low,
        close: c.close,
      })),
    )
    chart.current?.timeScale().fitContent()
  }, [candles])

  const first = candles[0]
  const last = candles[candles.length - 1]
  const change = first.open ? ((last.close - first.open) / first.open) * 100 : 0

  return (
    <div style={{ display: 'grid', gap: 'var(--space-2)' }}>
      <div ref={host} style={{ width: '100%' }} />
      <p className="num" style={{ margin: 0, fontSize: '0.75rem', color: 'var(--muted)' }}>
        {money(first.open)} → {money(last.close)}{' '}
        <span style={{ color: change >= 0 ? 'var(--gain)' : 'var(--loss)' }}>
          ({change >= 0 ? '+' : ''}
          {change.toFixed(2)}%)
        </span>{' '}
        · {candles.length} bars
      </p>
    </div>
  )
}

function Notice({ children, tone }: { children: React.ReactNode; tone?: 'loss' }) {
  return (
    <p
      style={{
        margin: 0,
        padding: 'var(--space-3)',
        border: '1px dashed var(--rule)',
        borderRadius: 4,
        color: tone ? `var(--${tone})` : 'var(--muted)',
        fontSize: '0.85rem',
      }}
    >
      {children}
    </p>
  )
}
