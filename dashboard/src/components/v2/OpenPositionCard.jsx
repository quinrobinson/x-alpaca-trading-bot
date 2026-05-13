import { useState } from 'react'
import { fmtMoney, fmtPct, fmtRelative, pnlColorClass } from '../../util'

/**
 * Open position card for the primary view.
 *
 * Always shown:
 *   - Header (ticker + strike + expiry + type)
 *   - Live P&L
 *   - Stop-loss → entry → target progress bar
 *   - The tweet that triggered this position
 *
 * Collapsed by default, expandable:
 *   - Greeks (delta/gamma/theta/vega)
 *   - Indicators (RSI/VWAP/ATR/IV)
 *   - Trade meta (ratchet, qty, time-in-trade)
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
    <article className="bg-slate-900 border border-slate-800 rounded-lg p-4">
      <header className="flex items-baseline justify-between gap-3">
        <div className="min-w-0">
          <h3 className="text-base font-semibold truncate">
            {position.ticker} ${position.strike} {position.option_type?.[0]?.toUpperCase()}
            <span className="ml-2 text-xs text-slate-400 font-normal">{position.expiration}</span>
          </h3>
          <div className="text-[10px] text-slate-500 font-mono mt-0.5 truncate">
            {position.contract_symbol}
          </div>
        </div>
        <div className="text-right shrink-0">
          <div className={`text-lg font-bold ${pnlColorClass(pnl)}`}>{fmtMoney(pnl)}</div>
          <div className={`text-xs ${pnlColorClass(pnl)}`}>{fmtPct(pnlPct)}</div>
        </div>
      </header>

      {/* Stop → entry → target progress bar */}
      <div className="mt-3">
        <div className="flex justify-between text-[9px] text-slate-500 mb-1 font-mono">
          <span>STOP {fmtMoney(stop)}</span>
          <span>ENTRY {fmtMoney(entry)}</span>
          <span>TARGET {fmtMoney(target)}</span>
        </div>
        <div className="relative h-2 bg-slate-800 rounded">
          <div className="absolute top-0 h-full w-px bg-slate-500" style={{ left: `${entryPct}%` }} />
          <div
            className={`absolute -top-1 w-2 h-4 rounded ${pnl >= 0 ? 'bg-emerald-400' : 'bg-rose-400'}`}
            style={{ left: `calc(${markerPct}% - 4px)` }}
          />
        </div>
      </div>

      {/* The originating tweet */}
      {position.source_post && (
        <div className="mt-3 p-2 rounded bg-slate-950/60 border border-slate-800/60">
          <div className="text-[9px] uppercase text-slate-500 mb-0.5">Triggered by</div>
          <p className="text-xs text-slate-200 italic">"{position.source_post.post_text}"</p>
          <div className="text-[10px] text-slate-500 mt-1">
            {fmtRelative(position.source_post.posted_at)}
          </div>
        </div>
      )}

      <button
        onClick={() => setExpanded((e) => !e)}
        className="mt-3 w-full text-[11px] text-slate-400 hover:text-slate-100 flex items-center justify-center gap-1"
      >
        {expanded ? 'Hide' : 'Show'} Greeks &amp; indicators
        <svg className={`w-3 h-3 transition-transform ${expanded ? 'rotate-180' : ''}`}
             viewBox="0 0 12 12">
          <path d="M3 4.5l3 3 3-3" stroke="currentColor" strokeWidth="1.5" fill="none" />
        </svg>
      </button>

      {expanded && (
        <div className="mt-3 space-y-3">
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
          <div className="flex items-center justify-between text-[11px] text-slate-400 pt-1 border-t border-slate-800">
            <span>Ratchet: <span className="text-slate-100">{ratchetLabel}</span></span>
            <span>Qty: <span className="text-slate-100">{position.qty}</span></span>
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
      <div className="text-[9px] uppercase text-slate-500">{label}</div>
      <div className={`text-xs font-mono ${isNeg ? 'text-rose-400' : 'text-slate-100'}`}>
        {value === null || value === undefined ? '—' : value}
      </div>
    </div>
  )
}
