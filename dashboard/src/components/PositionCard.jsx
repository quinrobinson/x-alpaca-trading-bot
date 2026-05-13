import { fmtMoney, fmtPct, pnlColorClass } from '../util'

/**
 * Legacy position card (used in /details). Hyper light tokens.
 */
export default function PositionCard({ position, livePrice, snapshot }) {
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
    <div className="bg-surface rounded-card p-5" style={{ boxShadow: 'var(--shadow-card)' }}>
      <div className="flex items-center justify-between">
        <div>
          <div className="text-lg font-bold text-ink-900 tracking-tight">
            {position.ticker} ${position.strike} {position.option_type?.toUpperCase()?.[0]}
            <span className="text-sm text-ink-500 ml-2 font-normal">{position.expiration}</span>
          </div>
          <div className="text-xs text-ink-400 font-mono mt-0.5">{position.contract_symbol}</div>
        </div>
        <div className="text-right">
          <div className={`text-xl font-bold tracking-tight ${pnlColorClass(pnl)}`}>{fmtMoney(pnl)}</div>
          <div className={`text-sm font-mono ${pnlColorClass(pnl)}`}>{fmtPct(pnlPct)}</div>
        </div>
      </div>

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

      <div className="mt-4 grid grid-cols-4 gap-3">
        <Stat label="Delta" value={snapshot?.delta} />
        <Stat label="Gamma" value={snapshot?.gamma} />
        <Stat label="Theta" value={snapshot?.theta} colorize="negative" />
        <Stat label="Vega" value={snapshot?.vega} />
      </div>

      <div className="mt-3 grid grid-cols-4 gap-3">
        <Stat label="RSI 14" value={snapshot?.rsi_14} />
        <Stat label="VWAP" value={snapshot?.vwap} />
        <Stat label="ATR 14" value={snapshot?.atr_14} />
        <Stat label="IV" value={snapshot?.iv} />
      </div>

      <div className="mt-4 flex items-center justify-between text-xs text-ink-600">
        <span>Ratchet: <span className="text-ink-900 font-medium">{ratchetLabel}</span></span>
        <span>Qty: <span className="text-ink-900 font-medium">{position.qty}</span></span>
        <span>
          Stop order: <span className="text-ink-900 font-mono" style={{ fontSize: 10 }}>{position.stop_order_id ?? '—'}</span>
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
      <div className="mono-label" style={{ fontSize: 10 }}>{label}</div>
      <div className={`text-sm font-mono mt-0.5 ${isNeg ? 'text-negative' : 'text-ink-900'}`}>
        {value === null || value === undefined ? '—' : value}
      </div>
    </div>
  )
}
