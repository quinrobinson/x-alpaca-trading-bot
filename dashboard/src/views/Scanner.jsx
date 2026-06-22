import { useEffect, useState, useCallback } from 'react'
import { apiUrl } from '../config.js'

/**
 * Scanner — observability for the Phase-A failed-breakout scanner.
 *
 * The scanner is log-only in Phase A: detected events land in
 * scanner_events for review, but no trades are placed. This page is
 * the operator's window into what the scanner is seeing so they can
 * decide whether to promote it to Phase B (paper trading) later.
 *
 * Layout:
 *   - Status row: enabled? universe size? events today? phase badge?
 *   - 14-day events-per-day mini bar chart
 *   - Universe chip list (collapsed by default)
 *   - Recent events table (sortable by detected_at desc; window selector)
 */
export default function Scanner() {
  const [status, setStatus] = useState(null)
  const [events, setEvents] = useState([])
  const [daily, setDaily] = useState([])
  const [windowDays, setWindowDays] = useState(7)
  const [tickerFilter, setTickerFilter] = useState('')
  const [showUniverse, setShowUniverse] = useState(false)
  const [loading, setLoading] = useState(true)

  const fetchAll = useCallback(async () => {
    setLoading(true)
    try {
      const [s, e, d] = await Promise.all([
        fetch(apiUrl('/scanner/status')).then(r => r.ok ? r.json() : null),
        fetch(apiUrl(
          `/scanner?since_days=${windowDays}&limit=100${
            tickerFilter ? `&ticker=${encodeURIComponent(tickerFilter)}` : ''
          }`
        )).then(r => r.ok ? r.json() : []),
        fetch(apiUrl('/scanner/daily?days=14')).then(r => r.ok ? r.json() : []),
      ])
      setStatus(s)
      setEvents(e || [])
      setDaily(d || [])
    } finally {
      setLoading(false)
    }
  }, [windowDays, tickerFilter])

  useEffect(() => {
    fetchAll()
    const id = setInterval(fetchAll, 30_000)
    return () => clearInterval(id)
  }, [fetchAll])

  const maxDaily = Math.max(1, ...daily.map(d => d.events))
  const eventsTickers = Array.from(new Set(events.map(e => e.ticker))).sort()

  return (
    <div className="space-y-4">
      {/* Status row */}
      <section className="card p-5">
        <div className="flex items-baseline justify-between mb-3">
          <h2 className="mono-label" style={{ fontSize: 11, letterSpacing: '0.16em' }}>
            Failed-breakout scanner
          </h2>
          <PhaseBadge phase={status?.phase} enabled={status?.enabled} />
        </div>
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mt-2">
          <Stat label="Status" value={status?.enabled ? 'Running' : 'Disabled'} />
          <Stat label="Universe" value={status ? `${status.universe_size} tickers` : '—'} />
          <Stat label="Events today" value={status?.events_today ?? '—'} />
          <Stat label="Events total" value={status?.events_total ?? '—'} />
        </div>
        {status?.last_event_at && (
          <div className="mt-3 text-xs" style={{ color: 'var(--fg-dim)' }}>
            Last event {new Date(status.last_event_at).toLocaleString()} ·
            scan every {Math.round((status?.interval_seconds || 300) / 60)} min
          </div>
        )}
      </section>

      {/* 14-day bar chart */}
      {daily.length > 0 && (
        <section className="card p-5">
          <h2 className="mono-label mb-3" style={{ fontSize: 11, letterSpacing: '0.16em' }}>
            Events per day · last 14 days
          </h2>
          <div className="flex items-end gap-1 h-24">
            {daily.map(d => {
              const h = maxDaily > 0 ? (d.events / maxDaily) * 100 : 0
              return (
                <div
                  key={d.day}
                  className="flex-1 flex flex-col justify-end items-center"
                  title={`${d.day}: ${d.events} event${d.events === 1 ? '' : 's'}`}
                >
                  <div
                    style={{
                      height: `${h}%`,
                      minHeight: d.events > 0 ? 4 : 1,
                      width: '100%',
                      background: d.events > 0 ? 'var(--accent-amber, #f59e0b)' : 'var(--border)',
                      borderRadius: 2,
                    }}
                  />
                </div>
              )
            })}
          </div>
          <div className="flex justify-between mt-2 text-xs" style={{ color: 'var(--fg-dim)' }}>
            <span>{daily[0]?.day}</span>
            <span>{daily[daily.length - 1]?.day}</span>
          </div>
        </section>
      )}

      {/* Universe (collapsible) */}
      <section className="card p-5">
        <button
          onClick={() => setShowUniverse(s => !s)}
          className="w-full flex items-center justify-between text-left"
        >
          <h2 className="mono-label" style={{ fontSize: 11, letterSpacing: '0.16em' }}>
            Universe ({status?.universe_size || 0})
          </h2>
          <span style={{ color: 'var(--fg-dim)' }}>{showUniverse ? '−' : '+'}</span>
        </button>
        {showUniverse && status?.universe && (
          <div className="mt-3 flex flex-wrap gap-2">
            {status.universe.map(t => {
              const hasEvent = eventsTickers.includes(t)
              return (
                <span
                  key={t}
                  className="font-mono text-xs px-2 py-1 rounded"
                  style={{
                    background: hasEvent
                      ? 'rgba(245,158,11,0.15)'
                      : 'var(--border)',
                    color: hasEvent ? 'var(--accent-amber, #f59e0b)' : 'var(--fg-dim)',
                    border: hasEvent
                      ? '1px solid var(--accent-amber, #f59e0b)'
                      : '1px solid transparent',
                  }}
                >
                  {t}
                </span>
              )
            })}
          </div>
        )}
      </section>

      {/* Events table */}
      <section className="card p-5">
        <div className="flex flex-wrap items-baseline justify-between gap-2 mb-3">
          <h2 className="mono-label" style={{ fontSize: 11, letterSpacing: '0.16em' }}>
            Detected events
          </h2>
          <div className="flex items-center gap-2 text-xs">
            <WindowButton label="Today" value={1} cur={windowDays} setCur={setWindowDays} />
            <WindowButton label="7d" value={7} cur={windowDays} setCur={setWindowDays} />
            <WindowButton label="30d" value={30} cur={windowDays} setCur={setWindowDays} />
            <input
              type="text"
              value={tickerFilter}
              onChange={e => setTickerFilter(e.target.value.toUpperCase().slice(0, 8))}
              placeholder="Ticker"
              className="px-2 py-1 rounded font-mono text-xs"
              style={{
                background: 'var(--bg)',
                border: '1px solid var(--border)',
                color: 'var(--fg)',
                width: 80,
              }}
            />
          </div>
        </div>

        {loading && events.length === 0 ? (
          <div className="text-sm py-8 text-center" style={{ color: 'var(--fg-dim)' }}>
            Loading…
          </div>
        ) : events.length === 0 ? (
          <div className="text-sm py-8 text-center" style={{ color: 'var(--fg-dim)' }}>
            No events in this window. The scanner runs every 5 min during RTH —
            real failed-breakouts are rare (1-5/day at most).
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr
                  className="mono-label text-left"
                  style={{ color: 'var(--fg-dim)', borderBottom: '1px solid var(--border)' }}
                >
                  <th className="py-2 pr-3">Time</th>
                  <th className="py-2 pr-3">Ticker</th>
                  <th className="py-2 pr-3 text-right">Prior high</th>
                  <th className="py-2 pr-3 text-right">Breakout</th>
                  <th className="py-2 pr-3 text-right">Failure</th>
                  <th className="py-2 pr-3 text-right">Depth</th>
                  <th className="py-2 pr-3 text-right">Vol×</th>
                </tr>
              </thead>
              <tbody className="font-mono">
                {events.map(e => (
                  <tr key={e.id} style={{ borderBottom: '1px solid var(--border)' }}>
                    <td className="py-2 pr-3" style={{ color: 'var(--fg-dim)' }}>
                      {fmtTime(e.detected_at)}
                    </td>
                    <td className="py-2 pr-3 font-semibold">{e.ticker}</td>
                    <td className="py-2 pr-3 text-right">{fmtPrice(e.prior_high)}</td>
                    <td className="py-2 pr-3 text-right">{fmtPrice(e.breakout_price)}</td>
                    <td className="py-2 pr-3 text-right">{fmtPrice(e.failure_price)}</td>
                    <td className="py-2 pr-3 text-right">
                      {e.failure_depth_pct != null ? `${(e.failure_depth_pct * 100).toFixed(2)}%` : '—'}
                    </td>
                    <td className="py-2 pr-3 text-right">
                      {e.volume_ratio != null ? `${Number(e.volume_ratio).toFixed(2)}` : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {/* Helper note */}
      <div className="text-xs px-1" style={{ color: 'var(--fg-dim)' }}>
        Phase A: events are observed only. The orchestrator does not place
        trades based on these detections yet. Once event quality looks
        right (volume ratios populated, meaningful failure depths,
        reasonable rate), we move to Phase B and wire scanner signals
        into the same pipeline as X-stream signals.
      </div>
    </div>
  )
}

// ---- subcomponents -----------------------------------------------------

function PhaseBadge({ phase, enabled }) {
  const label = phase ? `Phase ${phase}` : 'Phase —'
  const sub = enabled ? 'log-only' : 'disabled'
  return (
    <div
      className="font-mono text-xs px-2 py-1 rounded"
      style={{
        background: enabled ? 'rgba(245,158,11,0.15)' : 'var(--border)',
        color: enabled ? 'var(--accent-amber, #f59e0b)' : 'var(--fg-dim)',
      }}
    >
      {label} · {sub}
    </div>
  )
}

function Stat({ label, value }) {
  return (
    <div>
      <div className="mono-label" style={{ fontSize: 10, color: 'var(--fg-dim)' }}>
        {label}
      </div>
      <div className="text-xl font-bold mt-1">{value ?? '—'}</div>
    </div>
  )
}

function WindowButton({ label, value, cur, setCur }) {
  const active = cur === value
  return (
    <button
      onClick={() => setCur(value)}
      className="px-2 py-1 rounded font-mono"
      style={{
        background: active ? 'var(--accent-amber, #f59e0b)' : 'var(--border)',
        color: active ? 'var(--bg)' : 'var(--fg)',
      }}
    >
      {label}
    </button>
  )
}

function fmtTime(iso) {
  if (!iso) return '—'
  const d = new Date(iso)
  return `${d.getMonth() + 1}/${d.getDate()} ${d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`
}

function fmtPrice(s) {
  if (s == null) return '—'
  const n = Number(s)
  if (!Number.isFinite(n)) return '—'
  return n.toFixed(2)
}
