import { createContext, useCallback, useContext, useEffect, useState } from 'react'
import { Outlet } from 'react-router-dom'
import Header from './components/v2/Header.jsx'
import BottomNav from './components/v2/BottomNav.jsx'
import { useWebSocket } from './hooks/useWebSocket.js'
import { apiUrl, wsUrl } from './config.js'

/**
 * App shell — owns the polling + WebSocket state once and shares it
 * with every routed view via context. Without this, each tab would
 * remount and refetch on every navigation; with it, tab switches are
 * instant and the in-flight WS connection is preserved.
 *
 * Layout:
 *   ┌─────────────────────────┐
 *   │ Header (sticky top)     │
 *   ├─────────────────────────┤
 *   │ <Outlet />              │  ← active route's content, scrolls
 *   │                         │
 *   ├─────────────────────────┤
 *   │ BottomNav (fixed)       │
 *   └─────────────────────────┘
 */

const POLL_MS = 30_000

const AppDataContext = createContext(null)

export function useAppData() {
  const v = useContext(AppDataContext)
  if (v === null) throw new Error('useAppData must be used inside <AppShell>')
  return v
}

export default function AppShell() {
  const [health, setHealth] = useState(null)
  const [positions, setPositions] = useState([])
  const [timeline, setTimeline] = useState([])
  const [performance, setPerformance] = useState(null)
  const [marketCtx, setMarketCtx] = useState(null)
  const [killSwitches, setKillSwitches] = useState([])

  // ---- REST polling ---------------------------------------------------

  const fetchTimeline = useCallback(async () => {
    try {
      const r = await fetch(apiUrl('/timeline?limit=50'))
      if (r.ok) setTimeline(await r.json())
    } catch { /* swallow */ }
  }, [])

  const fetchPositions = useCallback(async () => {
    try {
      const r = await fetch(apiUrl('/positions'))
      if (r.ok) setPositions(await r.json())
    } catch { /* swallow */ }
  }, [])

  const fetchPerformance = useCallback(async () => {
    try {
      const r = await fetch(apiUrl('/performance'))
      if (r.ok) setPerformance(await r.json())
    } catch { /* swallow */ }
  }, [])

  const fetchMarket = useCallback(async () => {
    try {
      const r = await fetch(apiUrl('/market'))
      if (r.ok) setMarketCtx(await r.json())
    } catch { /* swallow */ }
  }, [])

  const fetchAll = useCallback(async () => {
    try {
      const [h] = await Promise.all([
        fetch(apiUrl('/healthz')).then(r => r.ok ? r.json() : null),
        fetchTimeline(),
        fetchPositions(),
        fetchPerformance(),
        fetchMarket(),
      ])
      if (h) {
        setHealth(h)
        if (Array.isArray(h.active_switches)) setKillSwitches(h.active_switches)
      }
    } catch { /* swallow */ }
  }, [fetchTimeline, fetchPositions, fetchPerformance, fetchMarket])

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
      case 'position.closing':
        fetchPositions()
        break
      case 'trade.exited':
        fetchPositions()
        fetchPerformance()
        fetchTimeline()
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

  const ctx = {
    health, positions, timeline, performance, marketCtx,
    killSwitches, wsStatus,
    refresh: { fetchAll, fetchTimeline, fetchPositions, fetchPerformance, fetchMarket },
  }

  return (
    <AppDataContext.Provider value={ctx}>
      <div className="min-h-screen flex flex-col max-w-3xl mx-auto lg:max-w-6xl">
        <Header
          wsStatus={wsStatus}
          health={health}
          performance={performance}
          killSwitches={killSwitches}
        />
        {/* pb-28 keeps the last bit of scrollable content from hiding
            behind the bottom nav. The nav itself adds safe-area-inset-
            bottom + a 14px buffer for the iOS home indicator, so we
            need a slightly taller reserve here than the nav's nominal
            height alone. */}
        <main className="flex-1 px-4 py-5 lg:px-6 lg:py-6 pb-28">
          <Outlet />
        </main>
        <BottomNav />
      </div>
    </AppDataContext.Provider>
  )
}
