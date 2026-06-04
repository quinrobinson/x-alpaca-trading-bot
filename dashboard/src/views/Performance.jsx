import { fmtMoney, fmtPct, pnlColorClass } from '../util.js'
import EquityCurve from '../components/v2/EquityCurve.jsx'
import { useAppData } from '../AppShell.jsx'

/**
 * Performance — the "is the bot working" view.
 *
 * Full-width cumulative P&L curve at the top, then a tile grid of the
 * key stats (win rate, total P&L, profit factor, average win/loss).
 * Trade-by-trade history list deferred to a future iteration.
 */
export default function Performance() {
  const { performance } = useAppData()
  const stats = performance?.stats ?? {}
  const trades = performance?.trades ?? []

  const winRate = stats.win_rate != null ? Number(stats.win_rate) : null
  const totalPnl = stats.total_pnl != null ? Number(stats.total_pnl) : null
  const tradeCount = stats.total_trades ?? 0
  const avgWin = stats.avg_win_pct != null ? Number(stats.avg_win_pct) : null
  const avgLoss = stats.avg_loss_pct != null ? Number(stats.avg_loss_pct) : null
  const pf = stats.profit_factor != null ? Number(stats.profit_factor) : null

  return (
    <div className="space-y-4">
      {/* Equity curve — the big chart up top */}
      <section className="card p-5">
        <div className="flex items-baseline justify-between mb-3">
          <h2
            className="mono-label"
            style={{ fontSize: 11, letterSpacing: '0.16em' }}
          >
            Cumulative P&L
          </h2>
          {totalPnl !== null && (
            <div className={`text-2xl font-bold tracking-tight ${pnlColorClass(totalPnl)}`}>
              {fmtMoney(totalPnl)}
            </div>
          )}
        </div>
        <EquityCurve trades={trades} height={140} />
        <div className="mt-3 flex items-center justify-between text-xs text-fg-dim">
          <span>{tradeCount} trade{tradeCount === 1 ? '' : 's'} closed</span>
          {winRate !== null && (
            <span>
              <span className="text-fg-muted">Win rate</span>{' '}
              <span className="text-fg font-medium">{(winRate * 100).toFixed(0)}%</span>
            </span>
          )}
        </div>
      </section>

      {/* Stats tile grid */}
      <section className="card p-5">
        <h2
          className="mono-label mb-4"
          style={{ fontSize: 11, letterSpacing: '0.16em' }}
        >
          Trade stats
        </h2>
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
          <Tile
            label="Win rate"
            value={winRate !== null ? `${(winRate * 100).toFixed(0)}%` : '—'}
          />
          <Tile
            label="Wins / losses"
            value={`${stats.wins ?? 0} / ${stats.losses ?? 0}`}
          />
          <Tile
            label="Avg win"
            value={avgWin !== null ? fmtPct(avgWin) : '—'}
            tone="text-positive"
          />
          <Tile
            label="Avg loss"
            value={avgLoss !== null ? fmtPct(avgLoss) : '—'}
            tone="text-negative"
          />
          <Tile
            label="Profit factor"
            value={pf !== null ? pf.toFixed(2) : '—'}
          />
          <Tile label="Trade count" value={tradeCount} />
          <Tile
            label="Total P&L"
            value={totalPnl !== null ? fmtMoney(totalPnl) : '—'}
            tone={totalPnl !== null ? pnlColorClass(totalPnl) : 'text-fg'}
          />
          {/* Reserved slot — keeps a 4-up grid even on the last row */}
          <div />
        </div>
      </section>
    </div>
  )
}

function Tile({ label, value, tone = 'text-fg' }) {
  return (
    <div>
      <div className="mono-label" style={{ fontSize: 10 }}>{label}</div>
      <div className={`text-xl font-bold tracking-tight mt-1 font-mono ${tone}`}>
        {value}
      </div>
    </div>
  )
}
