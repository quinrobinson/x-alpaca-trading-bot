import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import StatusBar from '../components/StatusBar.jsx'
import SignalFeed from '../components/SignalFeed.jsx'
import PositionCard from '../components/PositionCard.jsx'
import MarketContext from '../components/MarketContext.jsx'
import PerformanceHistory from '../components/PerformanceHistory.jsx'
import { useWebSocket } from '../hooks/useWebSocket.js'
import { apiUrl, wsUrl } from '../config.js'

/**
 * Details view (`/details`) — original 5-panel dashboard kept for power-
 * user inspection. The primary view at `/` is in Home.jsx — mobile-first
 * and pairs tweets with their outcomes.
 */

const POLL_MS = 30_000
const SIGNAL_FEED_MAX = 50

export default function Details() {
  const [health, setHealth] = useState(null)
  const [positions, setPositions] = useState([])
  const [signals, setSignals] = useState([])
  const [performance, setPerformance] = useState(null)
  const [killSwitches, setKillSwitches] = useState([])
  const [marketCtx, setMarketCtx] = useState(null)
  const [lastSignalTs, setLastSignalTs] = useState(null)

  // ---- REST polling -----------------------------------------------------

  const fetchAll = useCallback(async () => {
    try {
      const [h, p, s, perf, mkt] = await Promise.all([
        fetch(apiUrl('/healthz')).then(r => r.ok ? r.json() : null),
        fetch(apiUrl('/positions')).then(r => r.ok ? r.json() : []),
        fetch(apiUrl('/signals?limit=50')).then(r => r.ok ? r.json() : []),
        fetch(apiUrl('/performance')).then(r => r.ok ? r.json() : null),
        fetch(apiUrl('/market')).then(r => r.ok ? r.json() : null),
      ])
      if (h) setHealth(h)
      if (Array.isArray(p)) setPositions(p)
      if (Array.isArray(s)) setSignals(s)
      if (perf) setPerformance(perf)
      if (mkt) setMarketCtx(mkt)
    } catch (err) {
      // eslint-disable-next-line no-console
      console.warn('poll failed', err)
    }
  }, [])

  useEffect(() => {
    fetchAll()
    const id = setInterval(fetchAll, POLL_MS)
    return () => clearInterval(id)
  }, [fetchAll])

  // ---- WebSocket event handling -----------------------------------------

  const handleWs = useCallback((msg) => {
    switch (msg.event) {
      case 'signal.received':
      case 'signal.validated':
        // Trigger a /signals refresh so the row's `taken` flag is current.
        // Bumps the "last signal" badge in the status bar immediately.
        setLastSignalTs(msg.ts)
        fetch(apiUrl('/signals?limit=50')).then(r => r.ok && r.json()).then(rows => {
          if (Array.isArray(rows)) setSignals(rows.slice(0, SIGNAL_FEED_MAX))
        }).catch(() => {})
        break
      case 'trade.entered':
        // Refresh positions; new card should appear.
        fetch(apiUrl('/positions')).then(r => r.ok && r.json()).then(rows => {
          if (Array.isArray(rows)) setPositions(rows)
        }).catch(() => {})
        break
      case 'trade.stop_moved':
      case 'position.closing':
        fetch(apiUrl('/positions')).then(r => r.ok && r.json()).then(rows => {
          if (Array.isArray(rows)) setPositions(rows)
        }).catch(() => {})
        break
      case 'trade.exited':
        // Position closed — refresh both positions + performance.
        Promise.all([
          fetch(apiUrl('/positions')).then(r => r.ok && r.json()),
          fetch(apiUrl('/performance')).then(r => r.ok && r.json()),
        ]).then(([p, perf]) => {
          if (Array.isArray(p)) setPositions(p)
          if (perf) setPerformance(perf)
        }).catch(() => {})
        break
      case 'killswitch.tripped':
        setKillSwitches(msg.payload?.tripped ?? [])
        break
      case 'system.heartbeat':
        setKillSwitches(msg.payload?.active_switches ?? [])
        break
      default:
        break
    }
  }, [])

  const { status: wsStatus } = useWebSocket(wsUrl('/ws'), { onEvent: handleWs })

  // ---- Layout -----------------------------------------------------------

  return (
    <div className="min-h-screen flex flex-col">
      <StatusBar
        wsStatus={wsStatus}
        health={health}
        performance={performance}
        lastSignalTs={lastSignalTs ?? signals[0]?.parsed_at}
        killSwitches={killSwitches}
      />

      <main className="flex-1 grid grid-cols-1 lg:grid-cols-12 gap-4 p-4 min-h-0">
        <section className="lg:col-span-3 min-h-0">
          <SignalFeed signals={signals} />
        </section>

        <section className="lg:col-span-6 flex flex-col gap-4 min-h-0">
          {positions.length === 0 && (
            <div className="card p-6 text-sm text-fg-dim">
              No open positions.
            </div>
          )}
          {positions.map(pos => (
            <PositionCard
              key={pos.signal_id}
              position={pos}
              livePrice={pos.live_mid != null ? Number(pos.live_mid) : undefined}
              snapshot={pos.snapshot}
            />
          ))}
        </section>

        <section className="lg:col-span-3 min-h-0">
          <div className="card p-5 h-full">
            <h2 className="mono-label mb-3" style={{ fontSize: 11 }}>Market context</h2>
            <MarketContext
              snapshot={marketCtx}
              latestSectorString={marketCtx?.sector_etf_trend}
            />
          </div>
        </section>
      </main>

      <div className="px-4 pb-4">
        <PerformanceHistory performance={performance} />
      </div>

      <footer className="px-4 py-4 text-xs text-fg-dim flex items-center justify-between border-t border-border">
        <span
          className="font-mono uppercase tracking-wider"
          style={{ fontSize: 10, letterSpacing: '0.16em' }}
        >
          advanced view
        </span>
        <Link to="/" className="hover:text-fg transition-colors">← back to dashboard</Link>
      </footer>
    </div>
  )
}
