/**
 * MiniCandlestickChart — pure SVG OHLC chart for the open-position card.
 *
 * Props:
 *   bars        Array<{open, high, low, close}> oldest-first. Values may
 *               be strings (decimals) or numbers; coerced via Number().
 *   entryPrice  Optional. Renders a dashed horizontal reference line.
 *   stopPrice   Optional. Renders a solid amber reference line.
 *   height      Pixel height. Default 140.
 *   loading     Boolean. When true and no bars yet, renders a thin skeleton.
 *
 * The chart is responsive: the SVG uses a viewBox with a fixed nominal
 * width (1000 units) and scales to its container. Reference lines
 * (entry, stop) are included in the y-axis range so they're never
 * clipped at the top or bottom edge.
 */
export default function MiniCandlestickChart({
  bars,
  entryPrice,
  stopPrice,
  height = 140,
  loading = false,
}) {
  if (!bars || bars.length === 0) {
    return (
      <div
        className="flex items-center justify-center text-xs text-fg-dim"
        style={{ height }}
      >
        {loading ? 'Loading bars…' : 'No bar data'}
      </div>
    )
  }

  // Normalize OHLC to numbers — the API returns strings to preserve
  // Decimal precision; the chart only cares about pixel positions so
  // float is fine.
  const ohlc = bars.map((b) => ({
    open: Number(b.open),
    high: Number(b.high),
    low: Number(b.low),
    close: Number(b.close),
  }))

  // Build the y-axis range from candle extremes PLUS reference lines so
  // entry/stop are always visible.
  const allValues = ohlc.flatMap((b) => [b.high, b.low])
  if (Number.isFinite(entryPrice)) allValues.push(entryPrice)
  if (Number.isFinite(stopPrice)) allValues.push(stopPrice)
  const yMin = Math.min(...allValues)
  const yMax = Math.max(...allValues)
  const yRange = yMax - yMin || 1

  // Layout — nominal coordinate space, scaled by viewBox.
  const VIEW_W = 1000
  const VIEW_H = height
  const PAD = 6  // top/bottom breathing room
  const innerH = VIEW_H - PAD * 2
  const barSpan = VIEW_W / ohlc.length
  const bodyWidth = Math.max(1, barSpan * 0.65)

  const y = (price) => PAD + innerH - ((price - yMin) / yRange) * innerH

  return (
    <svg
      viewBox={`0 0 ${VIEW_W} ${VIEW_H}`}
      preserveAspectRatio="none"
      style={{ width: '100%', height, display: 'block' }}
      role="img"
      aria-label="Candlestick chart of the underlying"
    >
      {/* Entry reference — dashed, neutral */}
      {Number.isFinite(entryPrice) && (
        <line
          x1={0}
          x2={VIEW_W}
          y1={y(entryPrice)}
          y2={y(entryPrice)}
          stroke="var(--fg-dim)"
          strokeWidth="1"
          strokeDasharray="6 4"
          vectorEffect="non-scaling-stroke"
        />
      )}
      {/* Stop reference — solid, amber */}
      {Number.isFinite(stopPrice) && (
        <line
          x1={0}
          x2={VIEW_W}
          y1={y(stopPrice)}
          y2={y(stopPrice)}
          stroke="var(--accent-amber, #f59e0b)"
          strokeWidth="1.25"
          vectorEffect="non-scaling-stroke"
        />
      )}

      {/* Candles */}
      {ohlc.map((b, i) => {
        const isUp = b.close >= b.open
        const color = isUp ? 'var(--positive)' : 'var(--negative)'
        const cx = (i + 0.5) * barSpan
        const yHigh = y(b.high)
        const yLow = y(b.low)
        const yOpen = y(b.open)
        const yClose = y(b.close)
        const bodyTop = Math.min(yOpen, yClose)
        const bodyHeight = Math.max(1, Math.abs(yClose - yOpen))
        return (
          <g key={i}>
            {/* Wick — thin centered line through the body */}
            <line
              x1={cx}
              x2={cx}
              y1={yHigh}
              y2={yLow}
              stroke={color}
              strokeWidth="1.2"
              vectorEffect="non-scaling-stroke"
            />
            {/* Body */}
            <rect
              x={cx - bodyWidth / 2}
              y={bodyTop}
              width={bodyWidth}
              height={bodyHeight}
              fill={color}
            />
          </g>
        )
      })}
    </svg>
  )
}
