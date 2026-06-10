// Single seam for "where's the API?".
//
// The dashboard is served by FastAPI from the same origin as the API, so
// every fetch + WS is relative — no env var, no CORS, and the Cloudflare
// Access cookie attaches automatically.
//
// Every API path is namespaced under /api so SPA routes like /timeline
// and /performance don't collide with API endpoints of the same name
// (a cold load on /timeline used to hit the API and dump JSON into the
// browser instead of rendering the dashboard).
//
// In `npm run dev` the Vite proxy in vite.config.js forwards /api/* to
// the local FastAPI on :8000.

const API_PREFIX = '/api'

export function apiUrl(path) {
  // Path is expected to start with "/", e.g. "/positions"
  return `${API_PREFIX}${path}`
}

export function wsUrl(path = '/ws') {
  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
  return `${proto}://${window.location.host}${API_PREFIX}${path}`
}
