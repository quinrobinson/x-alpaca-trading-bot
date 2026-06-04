import { useEffect, useState } from 'react'
import { fmtExpiration, fmtMoney, fmtPct, fmtRelative, pnlColorClass } from '../../util'
import { apiUrl } from '../../config.js'
import SellNowButton from '../SellNowButton.jsx'
import MiniCandlestickChart from './MiniCandlestickChart.jsx'

/**
 * Open position card — APDF dark.
 *   #1A1A1A card, 1px border, 12px radius, mono numbers,
 *   warm-amber outline highlight to mark "in trade".
 */
const CHART_POLL_MS = 30_000

export default function OpenPositionCard({ position, livePrice, snapshot }) {
  const [expanded, setExpanded] = useState(false)
  // Mini-chart state — timeframe toggle (1m/5m/15m) and bars from the new
  // /market/bars endpoint. Refreshes every 30s while the card is mounted.
  const [chartTimeframe, setChartTimeframe] = useState('5m')
  const [bars, setBars] = useState([])
  const [barsLoading, setBarsLoading] = useState(false)

  const entry = Number(position.entry_price)
  const current = livePrice ?? entry
  // Options trade in 100-share contracts: dollar P&L = per-share move
  // × qty contracts × 100. pnlPct is per-share so it needs no multiplier.
  const pnl = (current - entry) * position.qty * 100
  const pnlPct = (current - entry) / entry
  const stop = Number(position.current_stop_price)
  const target = entry * 1.30
  const span = target - stop
  const markerPct = span > 0 ? Math.min(100, Math.max(0, ((current - stop) / span) * 100)) : 0
  const entryPct = span > 0 ? Math.min(100, Math.max(0, ((entry - stop) / span) * 100)) : 0

  // Continuous trail (2026-06): ratchet_level reflects regime, not
  // discrete thresholds. We show the actual stop as a gain % from entry
  // since that's the operationally meaningful number.
  const stopGainPct = entry > 0 ? (stop - entry) / entry : 0
  const ratchetLabel = position.ratchet_level === 0
    ? 'initial −20%'
    : position.ratchet_level === 2
      ? `tight trail • stop ${stopGainPct >= 0 ? '+' : ''}${(stopGainPct * 100).toFixed(1)}%`
      : `trailing • stop ${stopGainPct >= 0 ? '+' : ''}${(stopGainPct * 100).toFixed(1)}%`

  // ---- Underlying bars for the mini chart -----------------------------

  useEffect(() => {
    let cancelled = false

    async function load() {
      setBarsLoading(true)
      try {
        const r = await fetch(
          apiUrl(
            `/market/bars?ticker=${encodeURIComponent(position.ticker)}` +
              `&timeframe=${chartTimeframe}&limit=60`,
          ),
        )
        if (!r.ok) throw new Error(`HTTP ${r.status}`)
        const json = await r.json()
        if (!cancelled && Array.isArray(json)) setBars(json)
      } catch {
        // Swallow — chart falls back to "No bar data".
      } finally {
        if (!cancelled) setBarsLoading(false)
      }
    }

    load()
    const id = setInterval(load, CHART_POLL_MS)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [position.ticker, chartTimeframe])

  return (
    <article
      className="rounded-card p-5"
      style={{
        background: 'var(--card)',
        border: '1px solid rgba(245,158,11,0.30)',
        boxShadow: '0 0 0 1px rgba(245,158,11,0.06)',
      }}
    >
      <header className="flex items-baseline justify-between gap-3">
        <div className="min-w-0">
          <h3 className="text-base font-display font-semibold text-fg tracking-tight truncate">
            {position.ticker} ${position.strike}{position.option_type?.[0]?.toUpperCase()}
            <span className="ml-2 text-xs text-fg-dim font-normal">{fmtExpiration(position.expiration)}</span>
          </h3>
          <div className="font-mono text-fg-faint mt-0.5 truncate" style={{ fontSize: 10 }}>
            {position.contract_symbol}
          </div>
        </div>
        <div className="text-right shrink-0">
          <div className={`text-xl font-bold tracking-tight ${pnlColorClass(pnl)}`}>{fmtMoney(pnl)}</div>
          <div className={`text-xs font-mono ${pnlColorClass(pnl)}`}>{fmtPct(pnlPct)}</div>
        </div>
      </header>

      {/* Stop → entry → target progress bar */}
      <div className="mt-4">
        <div className="flex justify-between mono-label mb-2" style={{ fontSize: 10 }}>
          <span>STOP {fmtMoney(stop)}</span>
          <span>ENTRY {fmtMoney(entry)}</span>
          <span>TARGET {fmtMoney(target)}</span>
        </div>
        <div className="relative h-2 rounded-full" style={{ background: 'var(--elevated)' }}>
          <div
            className="absolute top-0 h-full w-px"
            style={{ left: `${entryPct}%`, background: 'var(--border-hover)' }}
          />
          <div
            className={`absolute -top-1 w-2.5 h-4 rounded ${pnl >= 0 ? 'bg-positive' : 'bg-negative'}`}
            style={{ left: `calc(${markerPct}% - 5px)` }}
          />
        </div>
      </div>

      {/* Underlying candle chart — helps decide whether to manually close */}
      <div className="mt-4">
        <div className="flex items-center justify-between mb-2">
          <div className="mono-label" style={{ fontSize: 10 }}>
            {position.ticker} underlying
          </div>
          <TimeframeToggle value={chartTimeframe} onChange={setChartTimeframe} />
        </div>
        <MiniCandlestickChart
          bars={bars}
          entryPrice={entry}
          stopPrice={stop}
          height={140}
          loading={barsLoading}
        />
        {/* Legend: which line is which */}
        <div className="mt-2 flex items-center justify-end gap-4 text-fg-dim" style={{ fontSize: 10 }}>
          <span className="inline-flex items-center gap-1.5">
            <span
              aria-hidden
              className="inline-block"
              style={{ width: 12, height: 0, borderTop: '1px dashed var(--fg-dim)' }}
            />
            entry
          </span>
          <span className="inline-flex items-center gap-1.5">
            <span
              aria-hidden
              className="inline-block"
              style={{ width: 12, height: 0, borderTop: '1.5px solid var(--accent-amber, #f59e0b)' }}
            />
            stop
          </span>
        </div>
      </div>

      {/* The originating tweet */}
      {position.source_post && (
        <div
          className="mt-4 p-3 rounded-lg"
          style={{
            background: 'var(--surface)',
            border: '1px solid var(--border)',
          }}
        >
          <div className="mono-label mb-1" style={{ fontSize: 10 }}>Triggered by</div>
          <p className="text-sm text-fg-muted leading-snug">
            "{position.source_post.post_text}"
          </p>
          <div className="text-fg-dim mt-1.5" style={{ fontSize: 11 }}>
            {fmtRelative(position.source_post.posted_at)}
          </div>
        </div>
      )}

      {/* Manual close — "Sell now" with inline confirm */}
      <div className="mt-4 flex justify-end">
        <SellNowButton
          signalId={position.signal_id}
          ticker={position.ticker}
          closingInProgress={position.closing_in_progress}
        />
      </div>

      <button
        onClick={() => setExpanded((e) => !e)}
        className="mt-3 w-full text-xs text-fg-dim hover:text-fg flex items-center justify-center gap-1.5 transition-colors py-1"
      >
        <span
          className="font-mono uppercase tracking-wider"
          style={{ fontSize: 10, letterSpacing: '0.16em' }}
        >
          {expanded ? 'Hide' : 'Show'} Greeks &amp; indicators
        </span>
        <svg
          className={`w-3 h-3 transition-transform ${expanded ? 'rotate-180' : ''}`}
          viewBox="0 0 12 12"
        >
          <path d="M3 4.5l3 3 3-3" stroke="currentColor" strokeWidth="1.5" fill="none" />
        </svg>
      </button>

      {expanded && (
        <div className="mt-3 pt-4 border-t border-border space-y-4">
          <div className="grid grid-cols-4 gap-3">
            <Stat label="Delta" value={snapshot?.delta} />
            <Stat label="Gamma" value={snapshot?.gamma} />
            <Stat label="Theta" value={snapshot?.theta} negative />
            <Stat label="Vega" value={snapshot?.vega} />
          </div>
          <div className="grid grid-cols-4 gap-3">
            <Stat label="RSI 14" value={snapshot?.rsi_14} />
            <Stat label="VWAP" value={snapshot?.vwap} />
            <Stat label="ATR 14" value={snapshot?.atr_14} />
            <Stat label="IV" value={snapshot?.iv} />
          </div>
          <div className="flex items-center justify-between text-xs text-fg-muted pt-3 border-t border-border">
            <span>Ratchet: <span className="text-fg font-medium">{ratchetLabel}</span></span>
            <span>Qty: <span className="text-fg font-medium">{position.qty}</span></span>
            <span>Opened {fmtRelative(position.opened_at)}</span>
          </div>
        </div>
      )}
    </article>
  )
}

function Stat({ label, value, negative }) {
  const n = Number(value)
  const isNeg = negative && Number.isFinite(n) && n < 0
  return (
    <div>
      <div className="mono-label" style={{ fontSize: 10 }}>{label}</div>
      <div className={`text-sm font-mono mt-0.5 ${isNeg ? 'text-negative' : 'text-fg'}`}>
        {value === null || value === undefined ? '—' : value}
      </div>
    </div>
  )
}

const TIMEFRAMES = ['1m', '5m', '15m']

function TimeframeToggle({ value, onChange }) {
  return (
    <div
      className="inline-flex rounded overflow-hidden"
      style={{ border: '1px solid var(--border)' }}
      role="group"
      aria-label="Chart timeframe"
    >
      {TIMEFRAMES.map((tf) => {
        const active = tf === value
        return (
          <button
            key={tf}
            type="button"
            onClick={() => onChange(tf)}
            className="px-2 py-0.5 font-mono uppercase transition-colors"
            style={{
              fontSize: 10,
              letterSpacing: '0.16em',
              background: active ? 'var(--elevated)' : 'transparent',
              color: active ? 'var(--fg)' : 'var(--fg-dim)',
            }}
            aria-pressed={active}
          >
            {tf}
          </button>
        )
      })}
    </div>
  )
}
