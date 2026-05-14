import { BrowserRouter, Routes, Route } from 'react-router-dom'
import Home from './views/Home.jsx'
import Details from './views/Details.jsx'
import Settings from './views/Settings.jsx'

/**
 * App shell — routes only.
 *   /         — primary mobile-first view (Home)
 *   /details  — original 5-panel detailed view (Details)
 *   /settings — runtime bot config (spend cap, kill thresholds, pause)
 */
export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Home />} />
        <Route path="/details" element={<Details />} />
        <Route path="/settings" element={<Settings />} />
      </Routes>
    </BrowserRouter>
  )
}
