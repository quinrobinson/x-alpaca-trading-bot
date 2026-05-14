// Small formatting helpers shared across panels.

export function fmtPct(value) {
  if (value === null || value === undefined || value === '') return '—'
  const n = typeof value === 'number' ? value : Number(value)
  if (!Number.isFinite(n)) return '—'
  const sign = n > 0 ? '+' : ''
  return `${sign}${(n * 100).toFixed(2)}%`
}

export function fmtMoney(value, { decimals = 2 } = {}) {
  if (value === null || value === undefined || value === '') return '—'
  const n = typeof value === 'number' ? value : Number(value)
  if (!Number.isFinite(n)) return '—'
  const sign = n < 0 ? '-' : ''
  return `${sign}$${Math.abs(n).toFixed(decimals)}`
}

export function fmtTime(iso) {
  if (!iso) return '—'
  try {
    return new Date(iso).toLocaleTimeString([], { hour12: false })
  } catch {
    return iso
  }
}

// Render an option expiration as M/D (e.g. "2026-05-16" → "5/16").
// Parse from the ISO string directly to avoid UTC → local timezone drift.
export function fmtExpiration(value) {
  if (!value) return '—'
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(value))
  if (m) return `${Number(m[2])}/${Number(m[3])}`
  const d = new Date(value)
  if (!Number.isFinite(d.getTime())) return String(value)
  return `${d.getMonth() + 1}/${d.getDate()}`
}

export function fmtRelative(iso, now = Date.now()) {
  if (!iso) return '—'
  const t = new Date(iso).getTime()
  if (!Number.isFinite(t)) return '—'
  const diff = Math.round((now - t) / 1000)
  if (diff < 5) return 'just now'
  if (diff < 60) return `${diff}s ago`
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`
  return `${Math.floor(diff / 3600)}h ago`
}

// Color a P&L number red/green. Maps to APDF semantic tokens.
export function pnlColorClass(value) {
  if (value === null || value === undefined || value === '') return 'text-fg-dim'
  const n = typeof value === 'number' ? value : Number(value)
  if (!Number.isFinite(n) || n === 0) return 'text-fg-dim'
  return n > 0 ? 'text-positive' : 'text-negative'
}
