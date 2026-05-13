import { BrowserRouter, Routes, Route } from 'react-router-dom'
import Home from './views/Home.jsx'
import Details from './views/Details.jsx'

/**
 * App shell — routes only.
 *   /         — primary mobile-first view (Home)
 *   /details  — original 5-panel detailed view (Details)
 */
export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Home />} />
        <Route path="/details" element={<Details />} />
      </Routes>
    </BrowserRouter>
  )
}
