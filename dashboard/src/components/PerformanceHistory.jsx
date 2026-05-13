import { useMemo, useState } from 'react'
import { fmtMoney, fmtPct, fmtTime, pnlColorClass } from '../util'

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

  const equityPoints = useMemo(() => buildEquityCurve(trades), [trades])

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
        <EquityCurve points={equityPoints} />
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

function buildEquityCurve(trades) {
  const sorted = [...trades].sort((a, b) =>
    String(a.closed_at).localeCompare(String(b.closed_at)),
  )
  let running = 0
  return sorted.map((t) => {
    running += Number(t.gross_pnl) || 0
    return { ts: t.closed_at, equity: running }
  })
}

function EquityCurve({ points }) {
  if (points.length < 2) {
    return <div className="text-xs text-fg-dim italic">Need ≥ 2 trades for a curve.</div>
  }
  const w = 600, h = 60
  const min = Math.min(...points.map(p => p.equity), 0)
  const max = Math.max(...points.map(p => p.equity), 0)
  const span = max - min || 1
  const xStep = w / (points.length - 1)
  const path = points
    .map((p, i) => {
      const x = i * xStep
      const y = h - ((p.equity - min) / span) * h
      return `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`
    })
    .join(' ')
  const lastPositive = points[points.length - 1].equity >= 0
  const stroke = lastPositive ? 'var(--positive)' : 'var(--negative)'
  return (
    <svg viewBox={`0 0 ${w} ${h}`} className="w-full h-16">
      <line
        x1={0} x2={w}
        y1={h - ((0 - min) / span) * h}
        y2={h - ((0 - min) / span) * h}
        stroke="var(--border)"
        strokeWidth={1}
        strokeDasharray="2,3"
      />
      <path d={path} stroke={stroke} fill="none" strokeWidth={1.5} />
    </svg>
  )
}
