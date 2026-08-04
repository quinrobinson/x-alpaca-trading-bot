import { useCallback, useEffect, useState } from 'react'
import OpenPositionCard from '../components/v2/OpenPositionCard.jsx'
import CollapsibleSection from '../components/v2/CollapsibleSection.jsx'
import MarketContext from '../components/MarketContext.jsx'
import { useAppData } from '../AppShell.jsx'
import { apiUrl } from '../config.js'
import { fmtRelative } from '../util.js'

/**
 * Dashboard — the "right now" view for the scanner program.
 *
 * Open S2 equity shorts lead (that's the live book), any legacy X-options
 * positions render below them (that book is retired but a straggler
 * position would still need watching), then market context.
 */
export default function Dashboard() {
  const { positions, marketCtx } = useAppData()
  const [book, setBook] = useState(null)
  const [scannerStatus, setScannerStatus] = useState(null)

  const fetchScanner = useCallback(async () => {
    try {
      const [t, s] = await Promise.all([
        fetch(apiUrl('/scanner/trades?limit=10')).then(r => r.ok ? r.json() : null),
        fetch(apiUrl('/scanner/status')).then(r => r.ok ? r.json() : null),
      ])
      if (t) setBook(t)
      if (s) setScannerStatus(s)
    } catch { /* swallow */ }
  }, [])

  useEffect(() => {
    fetchScanner()
    const id = setInterval(fetchScanner, 30_000)
    return () => clearInterval(id)
  }, [fetchScanner])

  const openShorts = (book?.trades || []).filter(t => t.is_open)
  const armed = scannerStatus?.trading?.enabled === true
  const nothingOpen = openShorts.length === 0 && positions.length === 0

  return (
    <div className="space-y-4">
      {openShorts.map(t => (
        <ScannerShortCard key={t.id} trade={t} />
      ))}

      {positions.map(p => (
        <OpenPositionCard
          key={p.signal_id}
          position={p}
          livePrice={p.live_mid != null ? Number(p.live_mid) : undefined}
          snapshot={p.snapshot}
        />
      ))}

      {nothingOpen && (
        <section className="card p-6 text-center text-sm text-fg-dim">
          {armed
            ? 'No open positions. The scanner book enters on qualifying failed-breakout events (vol ≥ 1.0×, failure before 10:30 ET).'
            : 'No open positions. The scanner book is disarmed — Phase S1 logs detections only. See the Scanner tab for today’s events.'}
        </section>
      )}

      <CollapsibleSection title="Market context">
        <MarketContext
          snapshot={marketCtx}
          latestSectorString={marketCtx?.sector_etf_trend}
        />
      </CollapsibleSection>
    </div>
  )
}

/**
 * One open S2 short. The stop is entry × 1.01 by spec (the resting
 * buy-stop's exact fill can differ by a cent of rounding); the time
 * exit is 60 minutes after entry, so "opened Xm ago" doubles as a
 * countdown the operator can read at a glance.
 */
function ScannerShortCard({ trade }) {
  const entry = Number(trade.entry_price)
  const stop = Number.isFinite(entry) ? entry * 1.01 : null
  return (
    <section
      className="rounded-card p-4"
      style={{
        background: 'var(--card)',
        border: '1px solid rgba(245,158,11,0.45)',
        boxShadow: '0 0 0 1px rgba(245,158,11,0.08)',
      }}
    >
      <div className="flex items-baseline justify-between gap-3">
        <div className="mono-label text-warning" style={{ fontSize: 10 }}>
          scanner short · open
        </div>
        <div className="text-fg-dim" style={{ fontSize: 11 }}>
          opened {fmtRelative(trade.opened_at)}
        </div>
      </div>
      <div className="mt-2 flex flex-wrap items-baseline gap-x-5 gap-y-2">
        <span className="font-mono text-fg font-bold" style={{ fontSize: 16 }}>
          {trade.ticker}
        </span>
        <span className="font-mono text-fg-muted" style={{ fontSize: 12 }}>
          short × {trade.qty}
        </span>
        <span className="ml-auto inline-flex items-baseline gap-4 font-mono" style={{ fontSize: 12 }}>
          <span>
            <span className="text-fg-faint uppercase" style={{ fontSize: 9, letterSpacing: '0.16em' }}>entry </span>
            {Number.isFinite(entry) ? entry.toFixed(2) : '—'}
          </span>
          <span>
            <span className="text-fg-faint uppercase" style={{ fontSize: 9, letterSpacing: '0.16em' }}>stop </span>
            {stop != null ? stop.toFixed(2) : '—'}
          </span>
        </span>
      </div>
      <div className="mt-2 text-xs text-fg-dim">
        Time exit 60 min after entry · 15:55 ET failsafe
      </div>
    </section>
  )
}
