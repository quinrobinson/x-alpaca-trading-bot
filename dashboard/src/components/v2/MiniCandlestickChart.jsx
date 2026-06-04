/**
 * MiniCandlestickChart — pure SVG OHLC chart for the open-position card.
 *
 * Props:
 *   bars            Array<{open, high, low, close}> oldest-first. Values
 *                   may be strings (decimals) or numbers; coerced via
 *                   Number().
 *   referencePrice  Optional. Dashed horizontal reference line. Must be
 *                   on the SAME price scale as the bars (e.g. underlying
 *                   stock price). Use for "where the underlying was at
 *                   entry" or similar single-line context.
 *   height          Pixel height. Default 140.
 *   loading         Boolean. When true and no bars yet, shows skeleton.
 *
 * The chart is responsive: the SVG uses a viewBox with a fixed nominal
 * width (1000 units) and scales to its container. The reference line
 * is folded into the y-axis range so it can't be clipped at the edge.
 */
export default function MiniCandlestickChart({
  bars,
  referencePrice,
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

  // Build the y-axis range from candle extremes PLUS the reference line
  // (if any) so the line can't fall off the chart edge.
  const allValues = ohlc.flatMap((b) => [b.high, b.low])
  if (Number.isFinite(referencePrice)) allValues.push(referencePrice)
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
      {/* Reference line — dashed, neutral. Single line at referencePrice
          (e.g. underlying-at-entry). Drawn before candles so wicks/bodies
          render on top of it. */}
      {Number.isFinite(referencePrice) && (
        <line
          x1={0}
          x2={VIEW_W}
          y1={y(referencePrice)}
          y2={y(referencePrice)}
          stroke="var(--fg-dim)"
          strokeWidth="1"
          strokeDasharray="6 4"
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
