import { fmtMoney, fmtPct, pnlColorClass } from '../util'

/**
 * Panel 3 — Active Positions (center, largest panel).
 *
 * One card per open position. Shows: header (ticker+strike+exp+type),
 * P&L section with progress bar (stop → entry → target),
 * Greeks, time info, and the trailing-stop tracker.
 *
 * Live updates: PositionCard re-renders whenever the parent passes a new
 * `livePrices` map (keyed by signal_id). The orchestrator's monitor
 * snapshots populate it via the trade.updated WS event.
 */
export default function PositionCard({ position, livePrice, snapshot }) {
  const entry = Number(position.entry_price)
  const current = livePrice ?? entry
  const pnl = (current - entry) * position.qty
  const pnlPct = (current - entry) / entry
  const stop = Number(position.current_stop_price)

  // Progress bar: stop → entry → +30% target. Position the marker by price.
  const target = entry * 1.30
  const span = target - stop
  const markerPct = span > 0 ? Math.min(100, Math.max(0, ((current - stop) / span) * 100)) : 0
  const entryPct = span > 0 ? Math.min(100, Math.max(0, ((entry - stop) / span) * 100)) : 0

  const ratchetLabels = ['initial', '+10%', '+20%', '+25%', '+40%']
  const ratchetLabel = ratchetLabels[position.ratchet_level] ?? 'initial'

  return (
    <div className="bg-slate-900 border border-slate-800 rounded-lg p-4">
      <div className="flex items-center justify-between">
        <div>
          <div className="text-lg font-semibold">
            {position.ticker} ${position.strike} {position.option_type?.toUpperCase()?.[0]}
            <span className="text-sm text-slate-400 ml-2">{position.expiration}</span>
          </div>
          <div className="text-xs text-slate-500 font-mono">{position.contract_symbol}</div>
        </div>
        <div className="text-right">
          <div className={`text-xl font-bold ${pnlColorClass(pnl)}`}>{fmtMoney(pnl)}</div>
          <div className={`text-sm ${pnlColorClass(pnl)}`}>{fmtPct(pnlPct)}</div>
        </div>
      </div>

      {/* Stop → entry → target progress bar */}
      <div className="mt-4">
        <div className="flex justify-between text-[10px] text-slate-500 mb-1">
          <span>STOP {fmtMoney(stop)}</span>
          <span>ENTRY {fmtMoney(entry)}</span>
          <span>TARGET {fmtMoney(target)}</span>
        </div>
        <div className="relative h-2 bg-slate-800 rounded">
          {/* Entry tick */}
          <div className="absolute top-0 h-full w-px bg-slate-500" style={{ left: `${entryPct}%` }} />
          {/* Current price marker */}
          <div
            className={`absolute -top-1 w-2 h-4 rounded ${pnl >= 0 ? 'bg-emerald-400' : 'bg-rose-400'}`}
            style={{ left: `calc(${markerPct}% - 4px)` }}
          />
        </div>
      </div>

      {/* Greeks */}
      <div className="mt-4 grid grid-cols-4 gap-3">
        <Stat label="Delta" value={snapshot?.delta} />
        <Stat label="Gamma" value={snapshot?.gamma} />
        <Stat label="Theta" value={snapshot?.theta} colorize="negative" />
        <Stat label="Vega" value={snapshot?.vega} />
      </div>

      {/* Indicators (underlying) */}
      <div className="mt-3 grid grid-cols-4 gap-3">
        <Stat label="RSI 14" value={snapshot?.rsi_14} />
        <Stat label="VWAP" value={snapshot?.vwap} />
        <Stat label="ATR 14" value={snapshot?.atr_14} />
        <Stat label="IV" value={snapshot?.iv} />
      </div>

      {/* Stop tracker */}
      <div className="mt-4 flex items-center justify-between text-xs">
        <span className="text-slate-400">
          Ratchet: <span className="text-slate-100">{ratchetLabel}</span>
        </span>
        <span className="text-slate-400">
          Qty: <span className="text-slate-100">{position.qty}</span>
        </span>
        <span className="text-slate-400">
          Stop order: <span className="text-slate-100 font-mono text-[10px]">{position.stop_order_id ?? '—'}</span>
        </span>
      </div>
    </div>
  )
}

function Stat({ label, value, colorize }) {
  const n = Number(value)
  const isNeg = colorize === 'negative' && Number.isFinite(n) && n < 0
  return (
    <div>
      <div className="text-[10px] uppercase text-slate-500">{label}</div>
      <div className={`text-sm font-mono ${isNeg ? 'text-rose-400' : 'text-slate-100'}`}>
        {value === null || value === undefined ? '—' : value}
      </div>
    </div>
  )
}
