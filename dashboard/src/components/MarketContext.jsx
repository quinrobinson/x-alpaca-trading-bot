import { fmtPct } from '../util'

/**
 * Market context — Hyper light tokens. Used in both Home (collapsible)
 * and Details views.
 */
export default function MarketContext({ snapshot, latestSectorString }) {
  const vix = snapshot?.vix
  const spy = snapshot?.spy_vs_ema21
  const sectors = parseSectorString(latestSectorString || snapshot?.sector_etf_trend)

  return (
    <div>
      <div className="grid grid-cols-2 gap-3">
        <BigStat label="VIX" value={vix ?? '—'} />
        <BigStat
          label="SPY vs EMA21"
          value={spy ?? '—'}
          accent={spy === 'above' ? 'positive' : spy === 'below' ? 'negative' : null}
        />
      </div>

      <div className="mt-4">
        <div className="mono-label mb-2" style={{ fontSize: 10 }}>Sector heatmap</div>
        {sectors.length === 0 && (
          <div className="text-xs text-ink-500">no recent data</div>
        )}
        <div className="grid grid-cols-3 gap-1.5">
          {sectors.map(({ symbol, pct }) => (
            <div
              key={symbol}
              className={`px-2 py-1.5 rounded-lg text-xs font-mono ${
                pct >= 0
                  ? 'bg-[var(--green-500)]/10 text-positive'
                  : 'bg-[var(--danger)]/10 text-negative'
              }`}
            >
              <span className="font-semibold">{symbol}</span>{' '}
              <span>{fmtPct(pct)}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}

function BigStat({ label, value, accent }) {
  const tone =
    accent === 'positive' ? 'text-positive'
    : accent === 'negative' ? 'text-negative'
    : 'text-ink-900'
  return (
    <div className="bg-surface-2 rounded-xl px-3 py-2.5 border border-hairline">
      <div className="mono-label" style={{ fontSize: 10 }}>{label}</div>
      <div className={`text-base font-mono font-medium mt-0.5 ${tone}`}>{value}</div>
    </div>
  )
}

function parseSectorString(s) {
  if (!s || typeof s !== 'string') return []
  return s
    .split(',')
    .map((chunk) => chunk.trim())
    .filter(Boolean)
    .map((chunk) => {
      const m = chunk.match(/^([A-Z]+)([+-]?\d+(?:\.\d+)?)%$/)
      if (!m) return null
      return { symbol: m[1], pct: Number(m[2]) / 100 }
    })
    .filter(Boolean)
}
