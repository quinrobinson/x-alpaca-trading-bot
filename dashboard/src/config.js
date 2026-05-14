// Single seam for "where's the API?".
//
// The dashboard is served by FastAPI from the same origin as the API, so
// every fetch + WS is relative — no env var, no CORS, and the Cloudflare
// Access cookie attaches automatically.
//
// In `npm run dev` the Vite proxy in vite.config.js forwards /healthz,
// /positions, /signals, /performance, /timeline, /config, /ws to the
// local FastAPI on :8000.

export function apiUrl(path) {
  // Path is expected to start with "/", e.g. "/positions"
  return path
}

export function wsUrl(path = '/ws') {
  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
  return `${proto}://${window.location.host}${path}`
}
