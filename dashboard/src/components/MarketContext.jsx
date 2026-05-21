import { fmtPct } from '../util'

/**
 * Market context — APDF dark tokens. Used in both Home (inside a
 * CollapsibleSection) and the legacy Details view.
 */
export default function MarketContext({ snapshot, latestSectorString }) {
  const vix = snapshot?.vix
  const spy = snapshot?.spy_vs_ema21
  const sectors = parseSectorString(latestSectorString || snapshot?.sector_etf_trend)

  return (
    <div>
      <div className="grid grid-cols-2 gap-3">
        {/* `vix` actually carries the VIXY ETF price — a volatility proxy.
            See data_service._fetch_vix. Labeled VIXY so the number isn't
            misread as the VIX index level. */}
        <BigStat label="VIXY" value={vix ?? '—'} />
        <BigStat
          label="SPY vs EMA21"
          value={spy ?? '—'}
          accent={spy === 'above' ? 'positive' : spy === 'below' ? 'negative' : null}
        />
      </div>

      <div className="mt-4">
        <div className="mono-label mb-2" style={{ fontSize: 10 }}>Sector heatmap</div>
        {sectors.length === 0 && (
          <div className="text-xs text-fg-dim">no recent data</div>
        )}
        <div className="grid grid-cols-3 gap-1.5">
          {sectors.map(({ symbol, pct }) => (
            <div
              key={symbol}
              className="px-2 py-1.5 rounded-md text-xs font-mono"
              style={{
                background: pct >= 0
                  ? 'rgba(34,197,94,0.10)'
                  : 'rgba(239,68,68,0.10)',
                color: pct >= 0 ? 'var(--positive)' : 'var(--negative)',
                border: '1px solid var(--border)',
              }}
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
    : 'text-fg'
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
