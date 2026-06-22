import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import AppShell from './AppShell.jsx'
import Dashboard from './views/Dashboard.jsx'
import Timeline from './views/Timeline.jsx'
import Performance from './views/Performance.jsx'
import Scanner from './views/Scanner.jsx'
import Settings from './views/Settings.jsx'

/**
 * App routes — 5-tab bottom-nav layout.
 *
 *   /            Dashboard    open positions + market context
 *   /timeline    Timeline     signal/trade feed
 *   /scanner     Scanner      failed-breakout scanner observability
 *   /performance Performance  equity curve + stats
 *   /settings    Settings     runtime config
 *
 * Legacy /details and /home redirect to /. AppShell owns the polling
 * state, the WebSocket, the top header, and the bottom nav.
 */
export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<AppShell />}>
          <Route index element={<Dashboard />} />
          <Route path="timeline" element={<Timeline />} />
          <Route path="scanner" element={<Scanner />} />
          <Route path="performance" element={<Performance />} />
          <Route path="settings" element={<Settings />} />
        </Route>
        {/* Old bookmarks / PWA shortcuts -> redirect to Dashboard */}
        <Route path="/details" element={<Navigate to="/" replace />} />
        <Route path="/home" element={<Navigate to="/" replace />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  )
}
