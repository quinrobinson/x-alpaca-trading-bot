import { fmtExpiration, fmtMoney, fmtPct, pnlColorClass } from '../util'

/**
 * Legacy position card (used in /details). APDF dark tokens.
 */
export default function PositionCard({ position, livePrice, snapshot }) {
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

  const ratchetLabels = ['initial', '+10%', '+20%', '+25%', '+40%']
  const ratchetLabel = ratchetLabels[position.ratchet_level] ?? 'initial'

  return (
    <div className="card p-5">
      <div className="flex items-center justify-between">
        <div>
          <div className="text-lg font-display font-semibold text-fg tracking-tight">
            {position.ticker} ${position.strike}{position.option_type?.toUpperCase()?.[0]}
            <span className="text-sm text-fg-dim ml-2 font-normal">{fmtExpiration(position.expiration)}</span>
          </div>
          <div className="text-xs text-fg-faint font-mono mt-0.5">{position.contract_symbol}</div>
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

      <div className="mt-4 flex items-center justify-between text-xs text-fg-muted">
        <span>Ratchet: <span className="text-fg font-medium">{ratchetLabel}</span></span>
        <span>Qty: <span className="text-fg font-medium">{position.qty}</span></span>
        <span>
          Stop order: <span className="text-fg font-mono" style={{ fontSize: 10 }}>{position.stop_order_id ?? '—'}</span>
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
      <div className={`text-sm font-mono mt-0.5 ${isNeg ? 'text-negative' : 'text-fg'}`}>
        {value === null || value === undefined ? '—' : value}
      </div>
    </div>
  )
}
