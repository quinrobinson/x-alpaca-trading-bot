import { fmtPct } from '../util'

/**
 * Panel 4 — Market Context (right column).
 *
 * VIX, SPY/QQQ trend vs EMA21, sector ETF heatmap. Sourced from the most
 * recent indicator_snapshots row plus aggregated sector data.
 *
 * The bot's data_service summarizes the sector top movers into a single
 * comma-packed string (e.g. "XLK+1.23%,XLE-0.45%"). We just render it.
 */
export default function MarketContext({ snapshot, latestSectorString }) {
  const vix = snapshot?.vix
  const spy = snapshot?.spy_vs_ema21
  // Parse the packed sector string into chips
  const sectors = parseSectorString(latestSectorString || snapshot?.sector_etf_trend)

  return (
    <div className="bg-slate-900 border border-slate-800 rounded-lg p-4 h-full">
      <h2 className="text-sm font-semibold uppercase tracking-wide mb-3">Market context</h2>

      <div className="grid grid-cols-2 gap-3">
        <BigStat label="VIX" value={vix ?? '—'} />
        <BigStat
          label="SPY vs EMA21"
          value={spy ?? '—'}
          accent={spy === 'above' ? 'positive' : spy === 'below' ? 'negative' : null}
        />
      </div>

      <div className="mt-4">
        <div className="text-[10px] uppercase text-slate-500 mb-2">Sector heatmap</div>
        {sectors.length === 0 && (
          <div className="text-xs text-slate-500">no recent data</div>
        )}
        <div className="grid grid-cols-3 gap-1">
          {sectors.map(({ symbol, pct }) => (
            <div
              key={symbol}
              className={`px-2 py-1 rounded text-xs font-mono ${
                pct >= 0
                  ? 'bg-emerald-500/15 text-emerald-300'
                  : 'bg-rose-500/15 text-rose-300'
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
    accent === 'positive' ? 'text-emerald-400'
    : accent === 'negative' ? 'text-rose-400'
    : 'text-slate-100'
  return (
    <div className="bg-slate-950/40 rounded p-2">
      <div className="text-[10px] uppercase text-slate-500">{label}</div>
      <div className={`text-base font-mono mt-0.5 ${tone}`}>{value}</div>
    </div>
  )
}

/**
 * Accepts either:
 *  - a string from snapshot (e.g. "XLK+1.23%,XLE-0.45%")
 *  - a fallback null/undefined
 * Returns [{symbol, pct}]
 */
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
