import { useState } from 'react'
import { fmtMoney, fmtPct, pnlColorClass } from '../../util'

/**
 * One-line stats summary. Tap "More" to reveal avg win/loss + profit factor.
 *
 *   Win rate 67%  ·  3 trades  ·  +$1.50  ▾
 */
export default function StatsBar({ performance }) {
  const [open, setOpen] = useState(false)
  const stats = performance?.stats ?? {}
  const totalPnl = stats.total_pnl != null ? Number(stats.total_pnl) : null

  return (
    <section className="bg-slate-900 border border-slate-800 rounded-lg">
      <button
        onClick={() => setOpen((o) => !o)}
        className="w-full flex items-center justify-between px-4 py-3 text-left"
      >
        <div className="flex items-center gap-3 text-sm">
          <span className="text-slate-400 text-xs uppercase">Stats</span>
          <span className="text-slate-100">
            Win rate {stats.win_rate != null ? `${(stats.win_rate * 100).toFixed(0)}%` : '—'}
          </span>
          <span className="text-slate-500">·</span>
          <span className="text-slate-100">
            {stats.total_trades ?? 0} trades
          </span>
          {totalPnl !== null && (
            <>
              <span className="text-slate-500">·</span>
              <span className={`font-mono ${pnlColorClass(totalPnl)}`}>{fmtMoney(totalPnl)}</span>
            </>
          )}
        </div>
        <svg className={`w-3 h-3 text-slate-400 transition-transform ${open ? 'rotate-180' : ''}`}
             viewBox="0 0 12 12">
          <path d="M3 4.5l3 3 3-3" stroke="currentColor" strokeWidth="1.5" fill="none" />
        </svg>
      </button>

      {open && (
        <div className="px-4 pb-4 grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs border-t border-slate-800 pt-3">
          <Tile label="Avg win"
                value={stats.avg_win_pct != null ? fmtPct(Number(stats.avg_win_pct)) : '—'}
                tone="text-emerald-400" />
          <Tile label="Avg loss"
                value={stats.avg_loss_pct != null ? fmtPct(Number(stats.avg_loss_pct)) : '—'}
                tone="text-rose-400" />
          <Tile label="Profit factor"
                value={stats.profit_factor != null ? Number(stats.profit_factor).toFixed(2) : '—'} />
          <Tile label="Wins / losses"
                value={`${stats.wins ?? 0} / ${stats.losses ?? 0}`} />
        </div>
      )}
    </section>
  )
}

function Tile({ label, value, tone = 'text-slate-100' }) {
  return (
    <div>
      <div className="text-[10px] uppercase text-slate-500">{label}</div>
      <div className={`text-sm font-mono ${tone}`}>{value}</div>
    </div>
  )
}
