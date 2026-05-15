/**
 * Cumulative P&L line chart.
 *
 * Given a list of closed trades, plots running gross_pnl over time as an
 * SVG path. Green stroke if you're net positive at the end, red if not,
 * dim if flat. A subtle zero-line anchors the chart visually.
 *
 *   <EquityCurve trades={trades} />          # default (60px tall)
 *   <EquityCurve trades={trades} height={32} />  # compact for inline use
 *
 * With <2 trades the chart can't draw a line — renders a single dot at
 * the current P&L instead, or a "need more trades" hint if empty.
 */
export default function EquityCurve({ trades = [], height = 60, className = '' }) {
  const points = buildEquityCurve(trades)
  const w = 600  // viewBox width; SVG scales to container via 100% width
  const h = height

  if (points.length === 0) {
    return (
      <div className={`text-xs text-fg-dim italic ${className}`}>
        no closed trades yet
      </div>
    )
  }

  const min = Math.min(...points.map((p) => p.equity), 0)
  const max = Math.max(...points.map((p) => p.equity), 0)
  const span = max - min || 1
  const yOf = (eq) => h - ((eq - min) / span) * h
  const zeroY = yOf(0)

  const lastEquity = points[points.length - 1].equity
  const stroke =
    lastEquity > 0 ? 'var(--positive)' :
    lastEquity < 0 ? 'var(--negative)' :
    'var(--fg-dim)'

  // One trade → no path to draw; render a single dot at the data point.
  if (points.length === 1) {
    return (
      <svg viewBox={`0 0 ${w} ${h}`} className={`w-full ${className}`} style={{ height }}>
        <line
          x1={0} x2={w} y1={zeroY} y2={zeroY}
          stroke="var(--border)" strokeWidth={1} strokeDasharray="2,3"
        />
        <circle cx={w / 2} cy={yOf(lastEquity)} r={3} fill={stroke} />
      </svg>
    )
  }

  const xStep = w / (points.length - 1)
  const path = points
    .map((p, i) => {
      const x = i * xStep
      const y = yOf(p.equity)
      return `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`
    })
    .join(' ')

  // Area fill underneath the line — gives the chart visual weight without
  // adding markings that distract from the trend.
  const areaPath = `${path} L${w},${h} L0,${h} Z`

  return (
    <svg viewBox={`0 0 ${w} ${h}`} className={`w-full ${className}`} style={{ height }} preserveAspectRatio="none">
      {/* Zero line — dashed hairline so it doesn't dominate */}
      <line
        x1={0} x2={w} y1={zeroY} y2={zeroY}
        stroke="var(--border)" strokeWidth={1} strokeDasharray="2,3"
        vectorEffect="non-scaling-stroke"
      />
      {/* Area under curve */}
      <path d={areaPath} fill={stroke} opacity={0.10} />
      {/* The line itself */}
      <path
        d={path} fill="none" stroke={stroke} strokeWidth={1.5}
        vectorEffect="non-scaling-stroke"
      />
    </svg>
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
