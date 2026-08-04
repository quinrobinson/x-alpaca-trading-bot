import { useEffect, useState, useCallback } from 'react'
import { apiUrl } from '../config.js'

/**
 * Scanner — observability for the scanner program (SCANNER_PROGRAM.md).
 *
 * Phase S1: several hypotheses run side by side in the scanner lab,
 * each logging detections to scanner_events under its own scanner_name.
 * Phase S2: the equity book trades the validated failed_breakout slice
 * (ships disarmed) and records to scanner_trades.
 *
 * Layout:
 *   - Status row: enabled? universe? events today? phase badge
 *   - Per-hypothesis chips (today / total counts)
 *   - Paper trading card: arm state + open/closed trades + P&L
 *   - 14-day events-per-day mini bar chart
 *   - Universe chip list (collapsed by default)
 *   - Recent events table (hypothesis + ticker filters)
 */

const SCANNER_LABELS = {
  failed_breakout: 'Failed breakout',
  vwap_reject: 'VWAP reject',
  gap_fade: 'Gap fade',
  prior_low_break: 'Prior-low break',
}

export default function Scanner() {
  const [status, setStatus] = useState(null)
  const [events, setEvents] = useState([])
  const [daily, setDaily] = useState([])
  const [book, setBook] = useState(null)
  const [windowDays, setWindowDays] = useState(7)
  const [tickerFilter, setTickerFilter] = useState('')
  const [scannerFilter, setScannerFilter] = useState('')
  const [showUniverse, setShowUniverse] = useState(false)
  const [loading, setLoading] = useState(true)

  const fetchAll = useCallback(async () => {
    setLoading(true)
    try {
      const eventsQs = [
        `since_days=${windowDays}`,
        'limit=100',
        tickerFilter ? `ticker=${encodeURIComponent(tickerFilter)}` : '',
        scannerFilter ? `scanner=${encodeURIComponent(scannerFilter)}` : '',
      ].filter(Boolean).join('&')
      const [s, e, d, t] = await Promise.all([
        fetch(apiUrl('/scanner/status')).then(r => r.ok ? r.json() : null),
        fetch(apiUrl(`/scanner?${eventsQs}`)).then(r => r.ok ? r.json() : []),
        fetch(apiUrl('/scanner/daily?days=14')).then(r => r.ok ? r.json() : []),
        fetch(apiUrl('/scanner/trades?limit=50')).then(r => r.ok ? r.json() : null),
      ])
      setStatus(s)
      setEvents(e || [])
      setDaily(d || [])
      setBook(t)
    } finally {
      setLoading(false)
    }
  }, [windowDays, tickerFilter, scannerFilter])

  useEffect(() => {
    fetchAll()
    const id = setInterval(fetchAll, 30_000)
    return () => clearInterval(id)
  }, [fetchAll])

  const maxDaily = Math.max(1, ...daily.map(d => d.events))
  const eventsTickers = Array.from(new Set(events.map(e => e.ticker))).sort()
  const hypotheses = status?.hypotheses || Object.keys(SCANNER_LABELS)
  const byScanner = Object.fromEntries(
    (status?.by_scanner || []).map(b => [b.scanner_name, b])
  )

  return (
    <div className="space-y-4">
      {/* Status row */}
      <section className="card p-5">
        <div className="flex items-baseline justify-between mb-3">
          <h2 className="mono-label" style={{ fontSize: 11, letterSpacing: '0.16em' }}>
            Scanner lab
          </h2>
          <PhaseBadge status={status} />
        </div>
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mt-2">
          <Stat label="Status" value={status?.enabled ? 'Running' : 'Disabled'} />
          <Stat label="Universe" value={status ? `${status.universe_size} tickers` : '—'} />
          <Stat label="Events today" value={status?.events_today ?? '—'} />
          <Stat label="Events total" value={status?.events_total ?? '—'} />
        </div>

        {/* Per-hypothesis chips */}
        <div className="mt-4 flex flex-wrap gap-2">
          {hypotheses.map(name => {
            const b = byScanner[name]
            const active = scannerFilter === name
            return (
              <button
                key={name}
                onClick={() => setScannerFilter(active ? '' : name)}
                className="font-mono text-xs px-2 py-1 rounded"
                title={`${SCANNER_LABELS[name] || name}: ${b?.events_today ?? 0} today, ${b?.events_total ?? 0} total. Click to filter the events table.`}
                style={{
                  background: active ? 'var(--accent-amber, #f59e0b)' : 'var(--border)',
                  color: active ? 'var(--bg)' : 'var(--fg)',
                  border: '1px solid transparent',
                }}
              >
                {SCANNER_LABELS[name] || name}
                <span style={{ opacity: 0.7 }}>
                  {' '}· {b?.events_today ?? 0} today / {b?.events_total ?? 0}
                </span>
              </button>
            )
          })}
        </div>

        {status?.last_event_at && (
          <div className="mt-3 text-xs" style={{ color: 'var(--fg-dim)' }}>
            Last event {new Date(status.last_event_at).toLocaleString()} ·
            scan every {Math.round((status?.interval_seconds || 300) / 60)} min
          </div>
        )}
      </section>

      {/* Paper trading (S2) */}
      <TradingCard status={status} book={book} />

      {/* 14-day bar chart */}
      {daily.length > 0 && (
        <section className="card p-5">
          <h2 className="mono-label mb-3" style={{ fontSize: 11, letterSpacing: '0.16em' }}>
            Events per day · last 14 days · all hypotheses
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
            {scannerFilter && (
              <span className="normal-case" style={{ color: 'var(--fg-dim)' }}>
                {' '}· {SCANNER_LABELS[scannerFilter] || scannerFilter}
              </span>
            )}
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
            No events in this window. The lab scans every 5 min during RTH;
            with the 10:30 ET cutoff, expect a handful of events per day
            across all hypotheses.
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
                  <th className="py-2 pr-3">Scanner</th>
                  <th className="py-2 pr-3">Ticker</th>
                  <th className="py-2 pr-3 text-right">Ref level</th>
                  <th className="py-2 pr-3 text-right">Trigger</th>
                  <th className="py-2 pr-3 text-right">Confirm</th>
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
                    <td className="py-2 pr-3" style={{ color: 'var(--fg-dim)' }}>
                      {SCANNER_LABELS[e.scanner_name] || e.scanner_name}
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
        Phase S1: all hypotheses are log-only while forward returns
        accumulate. Phase S2 trades only the validated failed-breakout
        slice (vol ≥ 1.0×, failure before 10:30 ET) in shares and ships
        disarmed — it arms via SCANNER_TRADING_ENABLED after ~4 weeks of
        fresh volume-ratio data confirms the edge out-of-sample. See
        SCANNER_PROGRAM.md.
      </div>
    </div>
  )
}

// ---- subcomponents -----------------------------------------------------

function TradingCard({ status, book }) {
  const trading = status?.trading
  const armed = trading?.enabled === true
  const trades = book?.trades || []
  const stats = book?.stats
  const openTrades = trades.filter(t => t.is_open)
  const totalPnl = Number(stats?.total_pnl ?? 0)

  return (
    <section className="card p-5">
      <div className="flex items-baseline justify-between mb-3">
        <h2 className="mono-label" style={{ fontSize: 11, letterSpacing: '0.16em' }}>
          Paper trading · S2 equity book
        </h2>
        <div
          className="font-mono text-xs px-2 py-1 rounded"
          style={{
            background: armed ? 'rgba(34,197,94,0.15)' : 'var(--border)',
            color: armed ? 'var(--accent-green, #22c55e)' : 'var(--fg-dim)',
            border: armed ? '1px solid var(--accent-green, #22c55e)' : '1px solid transparent',
          }}
        >
          {armed ? 'ARMED' : 'DISARMED'}
        </div>
      </div>

      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <Stat label="Open" value={openTrades.length} />
        <Stat label="Closed" value={stats?.n_closed ?? '—'} />
        <Stat
          label="Win rate"
          value={
            stats && stats.n_closed > 0
              ? `${Math.round((stats.winners / stats.n_closed) * 100)}%`
              : '—'
          }
        />
        <Stat
          label="Total P&L"
          value={stats ? fmtMoney(totalPnl) : '—'}
          tone={totalPnl > 0 ? 'var(--accent-green, #22c55e)' : totalPnl < 0 ? 'var(--accent-red, #ef4444)' : undefined}
        />
      </div>

      {trades.length === 0 ? (
        <div className="mt-4 text-xs" style={{ color: 'var(--fg-dim)' }}>
          {armed
            ? 'Armed and waiting — no qualifying events yet today.'
            : `Disarmed — logging only. When armed: $${Number(trading?.notional || 1000).toLocaleString()} short per qualifying event, max ${trading?.max_concurrent ?? 3} concurrent, 60-min time exit, +1% stop.`}
        </div>
      ) : (
        <div className="overflow-x-auto mt-4">
          <table className="w-full text-xs">
            <thead>
              <tr
                className="mono-label text-left"
                style={{ color: 'var(--fg-dim)', borderBottom: '1px solid var(--border)' }}
              >
                <th className="py-2 pr-3">Opened</th>
                <th className="py-2 pr-3">Ticker</th>
                <th className="py-2 pr-3 text-right">Qty</th>
                <th className="py-2 pr-3 text-right">Entry</th>
                <th className="py-2 pr-3 text-right">Exit</th>
                <th className="py-2 pr-3 text-right">P&L</th>
                <th className="py-2 pr-3">Reason</th>
              </tr>
            </thead>
            <tbody className="font-mono">
              {trades.map(t => {
                const pnl = t.gross_pnl != null ? Number(t.gross_pnl) : null
                return (
                  <tr key={t.id} style={{ borderBottom: '1px solid var(--border)' }}>
                    <td className="py-2 pr-3" style={{ color: 'var(--fg-dim)' }}>
                      {fmtTime(t.opened_at)}
                    </td>
                    <td className="py-2 pr-3 font-semibold">{t.ticker}</td>
                    <td className="py-2 pr-3 text-right">{t.qty}</td>
                    <td className="py-2 pr-3 text-right">{fmtPrice(t.entry_price)}</td>
                    <td className="py-2 pr-3 text-right">
                      {t.is_open ? (
                        <span style={{ color: 'var(--accent-amber, #f59e0b)' }}>open</span>
                      ) : fmtPrice(t.exit_price)}
                    </td>
                    <td
                      className="py-2 pr-3 text-right"
                      style={{
                        color: pnl == null ? 'var(--fg-dim)'
                          : pnl > 0 ? 'var(--accent-green, #22c55e)'
                          : pnl < 0 ? 'var(--accent-red, #ef4444)'
                          : undefined,
                      }}
                    >
                      {pnl != null ? fmtMoney(pnl) : '—'}
                    </td>
                    <td className="py-2 pr-3" style={{ color: 'var(--fg-dim)' }}>
                      {t.exit_reason || '—'}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}

function PhaseBadge({ status }) {
  const enabled = status?.enabled
  const phase = status?.phase || 'S1'
  const sub = !enabled ? 'disabled' : phase === 'S2' ? 'trading' : 'log-only'
  return (
    <div
      className="font-mono text-xs px-2 py-1 rounded"
      style={{
        background: enabled ? 'rgba(245,158,11,0.15)' : 'var(--border)',
        color: enabled ? 'var(--accent-amber, #f59e0b)' : 'var(--fg-dim)',
      }}
    >
      Phase {phase} · {sub}
    </div>
  )
}

function Stat({ label, value, tone }) {
  return (
    <div>
      <div className="mono-label" style={{ fontSize: 10, color: 'var(--fg-dim)' }}>
        {label}
      </div>
      <div className="text-xl font-bold mt-1" style={tone ? { color: tone } : undefined}>
        {value ?? '—'}
      </div>
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

function fmtMoney(n) {
  if (!Number.isFinite(n)) return '—'
  const sign = n < 0 ? '-' : ''
  return `${sign}$${Math.abs(n).toFixed(2)}`
}
