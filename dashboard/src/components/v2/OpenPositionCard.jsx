import { useState } from 'react'
import { fmtMoney, fmtPct, fmtRelative, pnlColorClass } from '../../util'

/**
 * Open position card — Hyper "Sales Report" style:
 *   white surface, 16px radius, soft multi-layer shadow,
 *   16px title, mono numbers, hairline-separated metric grid.
 */
export default function OpenPositionCard({ position, livePrice, snapshot }) {
  const [expanded, setExpanded] = useState(false)

  const entry = Number(position.entry_price)
  const current = livePrice ?? entry
  const pnl = (current - entry) * position.qty
  const pnlPct = (current - entry) / entry
  const stop = Number(position.current_stop_price)
  const target = entry * 1.30
  const span = target - stop
  const markerPct = span > 0 ? Math.min(100, Math.max(0, ((current - stop) / span) * 100)) : 0
  const entryPct = span > 0 ? Math.min(100, Math.max(0, ((entry - stop) / span) * 100)) : 0

  const ratchetLabels = ['initial', '+10%', '+20%', '+25%', '+40%']
  const ratchetLabel = ratchetLabels[position.ratchet_level] ?? 'initial'

  return (
    <article
      className="bg-surface rounded-card p-5"
      style={{ boxShadow: 'var(--shadow-card)' }}
    >
      <header className="flex items-baseline justify-between gap-3">
        <div className="min-w-0">
          <h3 className="text-base font-bold text-ink-900 tracking-tight truncate">
            {position.ticker} ${position.strike} {position.option_type?.[0]?.toUpperCase()}
            <span className="ml-2 text-xs text-ink-500 font-normal">{position.expiration}</span>
          </h3>
          <div className="font-mono text-ink-400 mt-0.5 truncate" style={{ fontSize: 10 }}>
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
        <div className="relative h-2 bg-ink-100 rounded-full">
          <div
            className="absolute top-0 h-full w-px bg-ink-300"
            style={{ left: `${entryPct}%` }}
          />
          <div
            className={`absolute -top-1 w-2.5 h-4 rounded ${pnl >= 0 ? 'bg-positive' : 'bg-negative'}`}
            style={{ left: `calc(${markerPct}% - 5px)` }}
          />
        </div>
      </div>

      {/* The originating tweet */}
      {position.source_post && (
        <div className="mt-4 p-3 rounded-xl bg-surface-2 border border-hairline">
          <div className="mono-label mb-1" style={{ fontSize: 10 }}>Triggered by</div>
          <p className="text-sm text-ink-700 leading-snug">
            "{position.source_post.post_text}"
          </p>
          <div className="text-ink-500 mt-1.5" style={{ fontSize: 11 }}>
            {fmtRelative(position.source_post.posted_at)}
          </div>
        </div>
      )}

      <button
        onClick={() => setExpanded((e) => !e)}
        className="mt-4 w-full text-xs text-ink-500 hover:text-ink-900 flex items-center justify-center gap-1.5 transition-colors py-1"
      >
        <span className="font-mono uppercase tracking-wider" style={{ fontSize: 10 }}>
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
        <div className="mt-3 pt-4 border-t border-hairline space-y-4">
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
          <div className="flex items-center justify-between text-xs text-ink-600 pt-3 border-t border-hairline">
            <span>Ratchet: <span className="text-ink-900 font-medium">{ratchetLabel}</span></span>
            <span>Qty: <span className="text-ink-900 font-medium">{position.qty}</span></span>
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
      <div className={`text-sm font-mono mt-0.5 ${isNeg ? 'text-negative' : 'text-ink-900'}`}>
        {value === null || value === undefined ? '—' : value}
      </div>
    </div>
  )
}
