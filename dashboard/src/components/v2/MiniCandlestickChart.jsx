/**
 * MiniCandlestickChart — pure SVG Heikin-Ashi chart for the position card.
 *
 * Rendered candles are Heikin-Ashi (HA), not raw OHLC. HA smooths out
 * intrabar noise so trends read as clean runs of one color and reversals
 * stand out as small bodies with long wicks. The raw bars come in from
 * the API; we apply the HA transform in this component because each HA
 * value depends on the PREVIOUS HA candle, which we'd have to ship state
 * around for if we did it server-side.
 *
 * HA formulae:
 *   HA close = (open + high + low + close) / 4              [from raw]
 *   HA open  = (previous HA open + previous HA close) / 2   [recursive]
 *   HA high  = max(raw high, HA open, HA close)
 *   HA low   = min(raw low,  HA open, HA close)
 * First-bar HA open uses (open + close) / 2 since there is no predecessor.
 *
 * Props:
 *   bars            Array<{open, high, low, close}> oldest-first. Raw
 *                   values may be strings (decimals) or numbers; coerced
 *                   via Number().
 *   referencePrice  Optional. Dashed horizontal reference line. Same
 *                   price scale as the bars (e.g. underlying-at-entry).
 *   height          Pixel height. Default 140.
 *   loading         Boolean. When true and no bars yet, shows skeleton.
 *
 * The chart is responsive: viewBox is fixed (1000 nominal units wide)
 * and scales to its container. The reference line is folded into the
 * y-axis range so it can't be clipped at the edge.
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

  // Normalize raw OHLC to numbers — the API returns strings to preserve
  // Decimal precision; the chart only cares about pixel positions so
  // float is fine.
  const raw = bars.map((b) => ({
    open: Number(b.open),
    high: Number(b.high),
    low: Number(b.low),
    close: Number(b.close),
  }))

  // Heikin-Ashi transform. Each HA bar depends on the previous HA bar's
  // open/close, so this is a sequential reduce — not a flatMap.
  const ha = []
  for (let i = 0; i < raw.length; i++) {
    const r = raw[i]
    const haClose = (r.open + r.high + r.low + r.close) / 4
    const haOpen = i === 0
      ? (r.open + r.close) / 2
      : (ha[i - 1].open + ha[i - 1].close) / 2
    const haHigh = Math.max(r.high, haOpen, haClose)
    const haLow = Math.min(r.low, haOpen, haClose)
    ha.push({ open: haOpen, high: haHigh, low: haLow, close: haClose })
  }

  // Build the y-axis range from HA candle extremes PLUS the reference
  // line (if any) so the line can't fall off the chart edge. HA highs/
  // lows can extend slightly beyond raw highs/lows when haOpen or
  // haClose pushes outside the raw range, so we anchor on HA values.
  const allValues = ha.flatMap((b) => [b.high, b.low])
  if (Number.isFinite(referencePrice)) allValues.push(referencePrice)
  const yMin = Math.min(...allValues)
  const yMax = Math.max(...allValues)
  const yRange = yMax - yMin || 1

  // Layout — nominal coordinate space, scaled by viewBox.
  const VIEW_W = 1000
  const VIEW_H = height
  const PAD = 6  // top/bottom breathing room
  const innerH = VIEW_H - PAD * 2
  const barSpan = VIEW_W / ha.length
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

      {/* Heikin-Ashi candles */}
      {ha.map((b, i) => {
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
