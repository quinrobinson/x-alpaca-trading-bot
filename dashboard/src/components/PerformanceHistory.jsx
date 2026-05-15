import { useMemo, useState } from 'react'  // useMemo still used for `sorted`
import { fmtMoney, fmtPct, fmtTime, pnlColorClass } from '../util'
import EquityCurve from './v2/EquityCurve.jsx'

/**
 * Legacy performance history panel (used in /details). APDF dark tokens.
 */
export default function PerformanceHistory({ performance }) {
  const [sortKey, setSortKey] = useState('closed_at')
  const [sortDir, setSortDir] = useState('desc')

  const stats = performance?.stats ?? {}
  const trades = performance?.trades ?? []

  const sorted = useMemo(() => {
    const out = [...trades]
    out.sort((a, b) => {
      const av = a[sortKey], bv = b[sortKey]
      const an = Number(av), bn = Number(bv)
      const cmp = Number.isFinite(an) && Number.isFinite(bn)
        ? an - bn
        : String(av ?? '').localeCompare(String(bv ?? ''))
      return sortDir === 'asc' ? cmp : -cmp
    })
    return out
  }, [trades, sortKey, sortDir])

  const toggleSort = (key) => {
    if (sortKey === key) setSortDir(d => (d === 'asc' ? 'desc' : 'asc'))
    else { setSortKey(key); setSortDir('desc') }
  }

  return (
    <div className="card p-5">
      <div className="flex items-center justify-between mb-4">
        <h2 className="mono-label" style={{ fontSize: 11 }}>Performance</h2>
        <span className="text-xs text-fg-dim">{stats.total_trades ?? 0} trades</span>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-6 gap-3 mb-5">
        <StatCard label="Win rate" value={stats.win_rate != null ? fmtPct(stats.win_rate) : '—'} />
        <StatCard label="Avg win" value={stats.avg_win_pct != null ? fmtPct(Number(stats.avg_win_pct)) : '—'} positive />
        <StatCard label="Avg loss" value={stats.avg_loss_pct != null ? fmtPct(Number(stats.avg_loss_pct)) : '—'} negative />
        <StatCard label="Profit factor" value={stats.profit_factor != null ? Number(stats.profit_factor).toFixed(2) : '—'} />
        <StatCard label="Wins / losses" value={`${stats.wins ?? 0} / ${stats.losses ?? 0}`} />
        <StatCard label="Total P&L" value={fmtMoney(stats.total_pnl)} colorize="signed" />
      </div>

      <div className="mb-5">
        <div className="mono-label mb-2" style={{ fontSize: 10 }}>Cumulative P&L</div>
        <EquityCurve trades={trades} height={60} />
      </div>

      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="text-fg-dim text-left">
              <Th onClick={() => toggleSort('closed_at')} active={sortKey === 'closed_at'} dir={sortDir}>Closed</Th>
              <Th onClick={() => toggleSort('ticker')} active={sortKey === 'ticker'} dir={sortDir}>Ticker</Th>
              <Th>Strike</Th>
              <Th>Entry</Th>
              <Th>Exit</Th>
              <Th onClick={() => toggleSort('pnl_pct')} active={sortKey === 'pnl_pct'} dir={sortDir}>P&L %</Th>
              <Th onClick={() => toggleSort('exit_reason')} active={sortKey === 'exit_reason'} dir={sortDir}>Reason</Th>
              <Th>Hold</Th>
            </tr>
          </thead>
          <tbody>
            {sorted.length === 0 && (
              <tr><td className="py-3 text-fg-dim" colSpan={8}>No trades yet.</td></tr>
            )}
            {sorted.map((t) => {
              const pnl = Number(t.pnl_pct)
              return (
                <tr key={t.id} className="border-t border-border">
                  <td className="py-2 text-fg-muted">{fmtTime(t.closed_at)}</td>
                  <td className="py-2 font-mono text-fg">{t.ticker} {t.option_type?.[0]?.toUpperCase()}</td>
                  <td className="py-2 text-fg-muted">${t.strike}</td>
                  <td className="py-2 font-mono text-fg-muted">{t.entry_price}</td>
                  <td className="py-2 font-mono text-fg-muted">{t.exit_price}</td>
                  <td className={`py-2 font-mono font-medium ${pnlColorClass(pnl)}`}>{fmtPct(pnl)}</td>
                  <td className="py-2 text-fg-dim">{t.exit_reason}</td>
                  <td className="py-2 text-fg-dim font-mono">{t.hold_minutes}m</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function StatCard({ label, value, positive, negative, colorize }) {
  let tone = 'text-fg'
  if (positive) tone = 'text-positive'
  if (negative) tone = 'text-negative'
  if (colorize === 'signed') {
    const n = typeof value === 'string' && value.startsWith('-') ? -1 : 1
    tone = n < 0 ? 'text-negative' : 'text-positive'
  }
  return (
    <div
      className="rounded-md px-3 py-2.5"
      style={{
        background: 'var(--surface)',
        border: '1px solid var(--border)',
      }}
    >
      <div className="mono-label" style={{ fontSize: 10 }}>{label}</div>
      <div className={`text-base font-mono font-medium mt-0.5 ${tone}`}>{value}</div>
    </div>
  )
}

function Th({ children, onClick, active, dir }) {
  return (
    <th
      className={`py-2 pr-3 font-medium ${onClick ? 'cursor-pointer select-none hover:text-fg' : ''} ${active ? 'text-fg' : ''}`}
      onClick={onClick}
    >
      {children}
      {active && <span className="ml-1">{dir === 'asc' ? '▲' : '▼'}</span>}
    </th>
  )
}

