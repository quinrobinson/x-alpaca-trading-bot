// Single seam for "where's the API?".
//
// - In dev: VITE_API_BASE is empty/unset, we use same-origin which Vite's
//   proxy forwards to the FastAPI on localhost:8000.
// - In prod: VITE_API_BASE points at the droplet, e.g.
//   "https://api.x-alpaca-bot.example.com" (or "http://1.2.3.4:8000").

const RAW = (import.meta.env.VITE_API_BASE || '').replace(/\/$/, '')

export const API_BASE = RAW

export function apiUrl(path) {
  // Path is expected to start with "/", e.g. "/positions"
  return `${API_BASE}${path}`
}

export function wsUrl(path = '/ws') {
  if (API_BASE) {
    // Convert https://… → wss://…, http://… → ws://…
    const base = API_BASE.replace(/^http/, 'ws')
    return `${base}${path}`
  }
  // Same-origin fallback for dev
  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
  return `${proto}://${window.location.host}${path}`
}
