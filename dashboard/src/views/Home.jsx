import { useCallback, useEffect, useState } from 'react'
import Header from '../components/v2/Header.jsx'
import OpenPositionCard from '../components/v2/OpenPositionCard.jsx'
import Timeline from '../components/v2/Timeline.jsx'
import StatsBar from '../components/v2/StatsBar.jsx'
import CollapsibleSection from '../components/v2/CollapsibleSection.jsx'
import MarketContext from '../components/MarketContext.jsx'
import { useWebSocket } from '../hooks/useWebSocket.js'
import { apiUrl, wsUrl } from '../config.js'

const POLL_MS = 30_000

export default function Home() {
  const [health, setHealth] = useState(null)
  const [positions, setPositions] = useState([])
  const [timeline, setTimeline] = useState([])
  const [performance, setPerformance] = useState(null)
  const [latestSnapshot, setLatestSnapshot] = useState({})
  const [livePrices, setLivePrices] = useState({})
  const [killSwitches, setKillSwitches] = useState([])
  const [marketSectorString, setMarketSectorString] = useState(null)
  const [showRejected, setShowRejected] = useState(false)

  // ---- REST polling ---------------------------------------------------

  const fetchTimeline = useCallback(async () => {
    try {
      const r = await fetch(apiUrl('/timeline?limit=50'))
      if (r.ok) setTimeline(await r.json())
    } catch (err) { /* swallow */ }
  }, [])

  const fetchPositions = useCallback(async () => {
    try {
      const r = await fetch(apiUrl('/positions'))
      if (r.ok) setPositions(await r.json())
    } catch (err) { /* swallow */ }
  }, [])

  const fetchPerformance = useCallback(async () => {
    try {
      const r = await fetch(apiUrl('/performance'))
      if (r.ok) setPerformance(await r.json())
    } catch (err) { /* swallow */ }
  }, [])

  const fetchAll = useCallback(async () => {
    try {
      const [h] = await Promise.all([
        fetch(apiUrl('/healthz')).then(r => r.ok ? r.json() : null),
        fetchTimeline(),
        fetchPositions(),
        fetchPerformance(),
      ])
      if (h) {
        setHealth(h)
        if (Array.isArray(h.active_switches)) setKillSwitches(h.active_switches)
      }
    } catch (err) { /* swallow */ }
  }, [fetchTimeline, fetchPositions, fetchPerformance])

  useEffect(() => {
    fetchAll()
    const id = setInterval(fetchAll, POLL_MS)
    return () => clearInterval(id)
  }, [fetchAll])

  // ---- WebSocket -------------------------------------------------------

  const handleWs = useCallback((msg) => {
    switch (msg.event) {
      case 'signal.received':
      case 'signal.validated':
        fetchTimeline()
        break
      case 'trade.entered':
      case 'trade.stop_moved':
        fetchPositions()
        break
      case 'trade.exited':
        fetchPositions()
        fetchPerformance()
        fetchTimeline()
        break
      case 'trade.updated':
        if (msg.payload?.signal_id != null) {
          setLatestSnapshot(prev => ({ ...prev, [msg.payload.signal_id]: msg.payload }))
          if (msg.payload.option_mid) {
            setLivePrices(prev => ({ ...prev, [msg.payload.signal_id]: Number(msg.payload.option_mid) }))
          }
          if (msg.payload.sector_etf_trend) setMarketSectorString(msg.payload.sector_etf_trend)
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
  }, [fetchTimeline, fetchPositions, fetchPerformance])

  const { status: wsStatus } = useWebSocket(wsUrl('/ws'), { onEvent: handleWs })

  // ---- Render ----------------------------------------------------------

  const firstSnapshot = Object.values(latestSnapshot)[0]

  return (
    <div className="min-h-screen flex flex-col max-w-3xl mx-auto lg:max-w-6xl">
      <Header
        wsStatus={wsStatus}
        health={health}
        performance={performance}
        killSwitches={killSwitches}
      />

      <main className="flex-1 px-4 py-5 lg:px-6 lg:py-6 lg:grid lg:grid-cols-3 lg:gap-5 space-y-4 lg:space-y-0">
        {/* Left column (mobile: stacked first) — open position + stats */}
        <aside className="lg:col-span-1 space-y-4">
          {positions.length === 0 ? (
            <section className="card p-6 text-center text-sm text-fg-dim">
              No open positions.
            </section>
          ) : (
            positions.map(p => (
              <OpenPositionCard
                key={p.signal_id}
                position={p}
                livePrice={livePrices[p.signal_id]}
                snapshot={latestSnapshot[p.signal_id]}
              />
            ))
          )}

          <StatsBar performance={performance} />

          <CollapsibleSection title="Market context">
            <MarketContext
              snapshot={firstSnapshot}
              latestSectorString={marketSectorString}
            />
          </CollapsibleSection>
        </aside>

        {/* Right column (mobile: stacked second) — timeline */}
        <section className="lg:col-span-2">
          <Timeline
            items={timeline}
            showRejected={showRejected}
            onToggleRejected={setShowRejected}
          />
        </section>
      </main>

      <footer className="px-4 lg:px-6 py-4 text-xs text-fg-dim flex items-center justify-between border-t border-border mt-4">
        <span
          className="font-mono uppercase tracking-wider"
          style={{ fontSize: 10, letterSpacing: '0.16em' }}
        >
          x-alpaca-trading-bot
        </span>
        <span
          className="font-mono uppercase tracking-wider text-fg-faint"
          style={{ fontSize: 10, letterSpacing: '0.16em' }}
        >
          paper · alpaca
        </span>
      </footer>
    </div>
  )
}
