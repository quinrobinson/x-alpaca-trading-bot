import { useState } from 'react'
import { fmtMoney, fmtPct, pnlColorClass } from '../../util'
import EquityCurve from './EquityCurve.jsx'

/**
 * Stats card — APDF dark. Always shows:
 *   - the headline (win rate, trade count, P&L)
 *   - a compact cumulative-P&L sparkline beneath it
 * Click to expand the per-metric tile grid (avg win/loss, profit factor,
 * wins/losses).
 */
export default function StatsBar({ performance }) {
  const [open, setOpen] = useState(false)
  const stats = performance?.stats ?? {}
  const trades = performance?.trades ?? []
  const totalPnl = stats.total_pnl != null ? Number(stats.total_pnl) : null
  const tradeCount = stats.total_trades ?? 0

  return (
    <section className="card">
      <button
        onClick={() => setOpen((o) => !o)}
        className="w-full flex items-center justify-between px-5 pt-4 pb-2 text-left"
      >
        <div className="flex items-center gap-3 text-sm min-w-0 flex-wrap">
          <span className="mono-label" style={{ fontSize: 10 }}>Stats</span>
          <span className="text-fg font-medium">
            {stats.win_rate != null ? `${(stats.win_rate * 100).toFixed(0)}% win` : '— win'}
          </span>
          <span className="text-fg-faint">·</span>
          <span className="text-fg-muted">
            {tradeCount} trade{tradeCount === 1 ? '' : 's'}
          </span>
          {totalPnl !== null && (
            <>
              <span className="text-fg-faint">·</span>
              <span className="mono-label" style={{ fontSize: 10 }}>P&L</span>
              <span className={`font-mono ${pnlColorClass(totalPnl)}`}>
                {fmtMoney(totalPnl)}
              </span>
            </>
          )}
        </div>
        <svg
          className={`w-3 h-3 text-fg-dim transition-transform shrink-0 ${open ? 'rotate-180' : ''}`}
          viewBox="0 0 12 12"
        >
          <path d="M3 4.5l3 3 3-3" stroke="currentColor" strokeWidth="1.5" fill="none" />
        </svg>
      </button>

      {/* Always-visible sparkline of cumulative P&L. Decent visual weight
          but compact enough to stay in the collapsed view. */}
      <div className="px-5 pb-3">
        <EquityCurve trades={trades} height={36} />
      </div>

      {open && (
        <div className="px-5 pb-5 grid grid-cols-2 sm:grid-cols-4 gap-4 border-t border-border pt-4">
          <Tile
            label="Avg win"
            value={stats.avg_win_pct != null ? fmtPct(Number(stats.avg_win_pct)) : '—'}
            tone="text-positive"
          />
          <Tile
            label="Avg loss"
            value={stats.avg_loss_pct != null ? fmtPct(Number(stats.avg_loss_pct)) : '—'}
            tone="text-negative"
          />
          <Tile
            label="Profit factor"
            value={stats.profit_factor != null ? Number(stats.profit_factor).toFixed(2) : '—'}
          />
          <Tile
            label="Wins / losses"
            value={`${stats.wins ?? 0} / ${stats.losses ?? 0}`}
          />
        </div>
      )}
    </section>
  )
}

function Tile({ label, value, tone = 'text-fg' }) {
  return (
    <div>
      <div className="mono-label" style={{ fontSize: 10 }}>{label}</div>
      <div className={`text-base font-bold tracking-tight mt-0.5 font-mono ${tone}`}>{value}</div>
    </div>
  )
}
