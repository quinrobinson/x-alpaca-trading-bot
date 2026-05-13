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

// Color a P&L number red/green. Returns a Tailwind utility class.
export function pnlColorClass(value) {
  if (value === null || value === undefined || value === '') return 'text-slate-400'
  const n = typeof value === 'number' ? value : Number(value)
  if (!Number.isFinite(n) || n === 0) return 'text-slate-400'
  return n > 0 ? 'text-emerald-400' : 'text-rose-400'
}
