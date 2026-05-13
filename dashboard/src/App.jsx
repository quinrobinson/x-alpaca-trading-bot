import { useCallback, useEffect, useState } from 'react'
import StatusBar from './components/StatusBar.jsx'
import SignalFeed from './components/SignalFeed.jsx'
import PositionCard from './components/PositionCard.jsx'
import MarketContext from './components/MarketContext.jsx'
import PerformanceHistory from './components/PerformanceHistory.jsx'
import { useWebSocket } from './hooks/useWebSocket.js'

/**
 * App — orchestrates the dashboard.
 *
 * - REST state: /healthz, /positions, /signals, /performance polled every
 *   30s. Initial render shows them as null until the first fetch lands.
 * - WS state: /ws events update local caches in real time so we don't
 *   need to wait for the next poll. Polls + WS together give us
 *   "eventually consistent during the poll interval, instantly consistent
 *   for visible events."
 *
 * The base URL defaults to same-origin so this works behind the Vite dev
 * proxy and behind Vercel's rewrite rules in production.
 */

const POLL_MS = 30_000
const SIGNAL_FEED_MAX = 50

export default function App() {
  const [health, setHealth] = useState(null)
  const [positions, setPositions] = useState([])
  const [signals, setSignals] = useState([])
  const [performance, setPerformance] = useState(null)
  const [latestSnapshot, setLatestSnapshot] = useState({})  // signal_id -> last trade.updated payload
  const [livePrices, setLivePrices] = useState({})          // signal_id -> last mid price seen
  const [killSwitches, setKillSwitches] = useState([])
  const [marketSectorString, setMarketSectorString] = useState(null)
  const [lastSignalTs, setLastSignalTs] = useState(null)

  // ---- REST polling -----------------------------------------------------

  const fetchAll = useCallback(async () => {
    try {
      const [h, p, s, perf] = await Promise.all([
        fetch('/healthz').then(r => r.ok ? r.json() : null),
        fetch('/positions').then(r => r.ok ? r.json() : []),
        fetch('/signals?limit=50').then(r => r.ok ? r.json() : []),
        fetch('/performance').then(r => r.ok ? r.json() : null),
      ])
      if (h) setHealth(h)
      if (Array.isArray(p)) setPositions(p)
      if (Array.isArray(s)) setSignals(s)
      if (perf) setPerformance(perf)
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
        fetch('/signals?limit=50').then(r => r.ok && r.json()).then(rows => {
          if (Array.isArray(rows)) setSignals(rows.slice(0, SIGNAL_FEED_MAX))
        }).catch(() => {})
        break
      case 'trade.entered':
        // Refresh positions; new card should appear.
        fetch('/positions').then(r => r.ok && r.json()).then(rows => {
          if (Array.isArray(rows)) setPositions(rows)
        }).catch(() => {})
        break
      case 'trade.stop_moved':
        fetch('/positions').then(r => r.ok && r.json()).then(rows => {
          if (Array.isArray(rows)) setPositions(rows)
        }).catch(() => {})
        break
      case 'trade.exited':
        // Position closed — refresh both positions + performance.
        Promise.all([
          fetch('/positions').then(r => r.ok && r.json()),
          fetch('/performance').then(r => r.ok && r.json()),
        ]).then(([p, perf]) => {
          if (Array.isArray(p)) setPositions(p)
          if (perf) setPerformance(perf)
        }).catch(() => {})
        break
      case 'trade.updated':
        // Periodic indicator snapshot — update livePrices + greeks panels.
        if (msg.payload?.signal_id != null) {
          setLatestSnapshot(prev => ({ ...prev, [msg.payload.signal_id]: msg.payload }))
          if (msg.payload.option_mid) {
            setLivePrices(prev => ({ ...prev, [msg.payload.signal_id]: Number(msg.payload.option_mid) }))
          }
          if (msg.payload.sector_etf_trend) {
            setMarketSectorString(msg.payload.sector_etf_trend)
          }
        }
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

  const wsUrl = `${window.location.protocol === 'https:' ? 'wss' : 'ws'}://${window.location.host}/ws`
  const { status: wsStatus } = useWebSocket(wsUrl, { onEvent: handleWs })

  // ---- Layout -----------------------------------------------------------

  const firstSnapshot = Object.values(latestSnapshot)[0]

  return (
    <div className="min-h-screen flex flex-col">
      <StatusBar
        wsStatus={wsStatus}
        health={health}
        performance={performance}
        lastSignalTs={lastSignalTs ?? signals[0]?.parsed_at}
        killSwitches={killSwitches}
      />

      <main className="flex-1 grid grid-cols-1 lg:grid-cols-12 gap-3 p-3 min-h-0">
        <section className="lg:col-span-3 min-h-0">
          <SignalFeed signals={signals} />
        </section>

        <section className="lg:col-span-6 flex flex-col gap-3 min-h-0">
          {positions.length === 0 && (
            <div className="bg-slate-900 border border-slate-800 rounded-lg p-6 text-sm text-slate-400">
              No open positions.
            </div>
          )}
          {positions.map(pos => (
            <PositionCard
              key={pos.signal_id}
              position={pos}
              livePrice={livePrices[pos.signal_id]}
              snapshot={latestSnapshot[pos.signal_id]}
            />
          ))}
        </section>

        <section className="lg:col-span-3 min-h-0">
          <MarketContext
            snapshot={firstSnapshot}
            latestSectorString={marketSectorString}
          />
        </section>
      </main>

      <footer className="p-3">
        <PerformanceHistory performance={performance} />
      </footer>
    </div>
  )
}
