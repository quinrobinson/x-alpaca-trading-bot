import { useState } from 'react'
import { fmtMoney, fmtPct, pnlColorClass } from '../../util'

/**
 * One-line stats summary. Hyper widget style — title row + collapsed
 * "tiles" below the hairline divider when expanded.
 */
export default function StatsBar({ performance }) {
  const [open, setOpen] = useState(false)
  const stats = performance?.stats ?? {}
  const totalPnl = stats.total_pnl != null ? Number(stats.total_pnl) : null

  return (
    <section
      className="bg-surface rounded-card"
      style={{ boxShadow: 'var(--shadow-card)' }}
    >
      <button
        onClick={() => setOpen((o) => !o)}
        className="w-full flex items-center justify-between px-5 py-4 text-left"
      >
        <div className="flex items-center gap-3 text-sm min-w-0">
          <span className="mono-label" style={{ fontSize: 10 }}>Stats</span>
          <span className="text-ink-900 font-medium">
            Win rate {stats.win_rate != null ? `${(stats.win_rate * 100).toFixed(0)}%` : '—'}
          </span>
          <span className="text-ink-300">·</span>
          <span className="text-ink-700">{stats.total_trades ?? 0} trades</span>
          {totalPnl !== null && (
            <>
              <span className="text-ink-300">·</span>
              <span className={`font-mono ${pnlColorClass(totalPnl)}`}>{fmtMoney(totalPnl)}</span>
            </>
          )}
        </div>
        <svg
          className={`w-3 h-3 text-ink-500 transition-transform shrink-0 ${open ? 'rotate-180' : ''}`}
          viewBox="0 0 12 12"
        >
          <path d="M3 4.5l3 3 3-3" stroke="currentColor" strokeWidth="1.5" fill="none" />
        </svg>
      </button>

      {open && (
        <div className="px-5 pb-5 grid grid-cols-2 sm:grid-cols-4 gap-4 border-t border-hairline pt-4">
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

function Tile({ label, value, tone = 'text-ink-900' }) {
  return (
    <div>
      <div className="mono-label" style={{ fontSize: 10 }}>{label}</div>
      <div className={`text-base font-bold tracking-tight mt-0.5 font-mono ${tone}`}>{value}</div>
    </div>
  )
}
