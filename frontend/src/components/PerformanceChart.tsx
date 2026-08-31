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
 * Portfolio value over time (§10 Phase 4), reconstructed server-side from the
 * transaction log and historical closes.
 *
 * Two lines, because one is not readable on its own: what the holdings are
 * worth, and what was paid for them. The gap between them is unrealized P/L,
 * which is the number the chart exists to show.
 */

const RANGES = ['1M', '6M', '1Y', '5Y', 'ALL'] as const
type Range = (typeof RANGES)[number]

type Point = { date: string; value: number; cost_basis: number; unrealized: number }
type Performance = { range: string; points: Point[]; unpriced_symbols: string[] }

function token(name: string, fallback: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback
}

export function PerformanceChart() {
  const [range, setRange] = useState<Range>('1Y')

  const { data, error, isLoading } = useQuery({
    queryKey: ['performance', range],
    queryFn: async () => {
      const res = await fetch(`/api/portfolio/performance?range=${range}`)
      if (!res.ok) {
        const body = (await res.json().catch(() => ({}))) as { detail?: string }
        throw new Error(body.detail ?? `HTTP ${res.status}`)
      }
      return (await res.json()) as Performance
    },
    retry: false,
    refetchOnWindowFocus: false,
  })

  return (
    <div style={{ display: 'grid', gap: 'var(--space-3)' }}>
      <div role="group" aria-label="Performance range" style={{ display: 'flex', gap: 1 }}>
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
              cursor: 'pointer',
            }}
          >
            {option}
          </button>
        ))}
      </div>

      {error ? (
        <p style={{ margin: 0, color: 'var(--loss)', fontSize: '0.85rem' }}>{error.message}</p>
      ) : isLoading ? (
        <div style={{ height: 220, background: 'var(--slate)', borderRadius: 4 }} />
      ) : !data?.points.length ? (
        <p style={{ margin: 0, color: 'var(--muted)', fontSize: '0.85rem' }}>
          No history yet — record a transaction, or historical bars are unavailable.
        </p>
      ) : (
        <ValuePanel data={data} />
      )}

      {data && data.unpriced_symbols.length > 0 && (
        <p style={{ margin: 0, color: 'var(--muted)', fontSize: '0.75rem' }}>
          No historical data for {data.unpriced_symbols.join(', ')} — excluded from the value
          line, but still counted in cost basis.
        </p>
      )}
    </div>
  )
}

function ValuePanel({ data }: { data: Performance }) {
  const host = useRef<HTMLDivElement>(null)
  const chart = useRef<IChartApi | null>(null)
  const value = useRef<ISeriesApi<'Area'> | null>(null)
  const basis = useRef<ISeriesApi<'Line'> | null>(null)

  useEffect(() => {
    if (!host.current) return

    const instance = createChart(host.current, {
      height: 220,
      layout: {
        background: { type: ColorType.Solid, color: 'transparent' },
        textColor: token('--muted', '#8a8f98'),
        fontFamily: token('--font-mono', 'monospace'),
      },
      grid: {
        vertLines: { visible: false },
        horzLines: { color: token('--rule', '#222') },
      },
      rightPriceScale: { borderColor: token('--rule', '#222') },
      timeScale: { borderColor: token('--rule', '#222') },
      crosshair: { mode: 0 },
      handleScale: false,
      handleScroll: false,
    })

    const brass = token('--brass', '#C9A227')
    value.current = instance.addAreaSeries({
      lineColor: brass,
      topColor: `${brass}44`,
      bottomColor: `${brass}00`,
      lineWidth: 2,
    })
    // Cost basis as a flat reference line — deliberately quiet, so the eye
    // reads the gap rather than the second line.
    basis.current = instance.addLineSeries({
      color: token('--muted', '#8a8f98'),
      lineWidth: 1,
      lineStyle: 2,
      crosshairMarkerVisible: false,
    })
    chart.current = instance

    const resize = () => instance.applyOptions({ width: host.current?.clientWidth ?? 0 })
    resize()
    window.addEventListener('resize', resize)

    return () => {
      window.removeEventListener('resize', resize)
      instance.remove()
      chart.current = null
    }
  }, [])

  useEffect(() => {
    const toTime = (iso: string) =>
      (Date.parse(`${iso}T00:00:00Z`) / 1000) as UTCTimestamp
    value.current?.setData(data.points.map((p) => ({ time: toTime(p.date), value: p.value })))
    basis.current?.setData(
      data.points.map((p) => ({ time: toTime(p.date), value: p.cost_basis })),
    )
    chart.current?.timeScale().fitContent()
  }, [data])

  const last = data.points[data.points.length - 1]
  const up = last.unrealized >= 0

  return (
    <div style={{ display: 'grid', gap: 'var(--space-2)' }}>
      <div ref={host} style={{ width: '100%' }} />
      <p className="num" style={{ margin: 0, fontSize: '0.75rem', color: 'var(--muted)' }}>
        value {money(last.value)} · invested {money(last.cost_basis)} ·{' '}
        <span style={{ color: up ? 'var(--gain)' : 'var(--loss)' }}>
          {up ? '▲' : '▼'} {money(Math.abs(last.unrealized))} unrealized
        </span>
      </p>
    </div>
  )
}
