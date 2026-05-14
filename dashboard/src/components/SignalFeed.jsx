import { fmtExpiration, fmtTime } from '../util'

/**
 * Legacy signal feed (used in /details). APDF dark tokens — tinted
 * outline instead of a left accent bar.
 */
export default function SignalFeed({ signals = [] }) {
  return (
    <div className="card p-5 h-full flex flex-col min-h-0">
      <div className="flex items-center justify-between mb-3">
        <h2 className="mono-label" style={{ fontSize: 11 }}>Signal feed</h2>
        <span className="text-xs text-fg-dim">{signals.length} recent</span>
      </div>
      <div className="flex-1 overflow-y-auto space-y-2 min-h-0">
        {signals.length === 0 && (
          <div className="text-sm text-fg-dim py-4">No signals yet.</div>
        )}
        {signals.map((s) => (
          <SignalRow key={s.id} signal={s} />
        ))}
      </div>
    </div>
  )
}

function SignalRow({ signal }) {
  const isTaken = signal.taken === true
  const isRejected = signal.taken === false && signal.rejection_reason

  let ring, label, statusTone, dotColor
  if (isTaken) {
    ring = 'rgba(34,197,94,0.40)'
    dotColor = 'var(--positive)'
    label = 'TAKEN'
    statusTone = 'text-positive'
  } else if (isRejected) {
    ring = 'rgba(245,158,11,0.40)'
    dotColor = 'var(--warning)'
    label = `SKIP · ${signal.rejection_reason}`
    statusTone = 'text-warning'
  } else {
    ring = 'var(--border)'
    dotColor = 'var(--fg-faint)'
    label = 'pending'
    statusTone = 'text-fg-dim'
  }

  return (
    <div
      className="rounded-md px-3 py-2.5 text-xs"
      style={{
        background: 'var(--surface)',
        border: `1px solid ${ring}`,
      }}
    >
      <div className="flex items-center justify-between">
        <span className="font-mono text-fg">
          <span
            className="inline-block w-1.5 h-1.5 rounded-full mr-1.5 align-middle"
            style={{ background: dotColor }}
          />
          {signal.ticker} ${signal.strike}{signal.option_type?.toUpperCase()?.[0]} {fmtExpiration(signal.expiration)}
        </span>
        <span className="text-fg-dim">{fmtTime(signal.parsed_at)}</span>
      </div>
      <div className="mt-1 flex items-center justify-between" style={{ fontSize: 11 }}>
        <span className="text-fg-muted">
          posted {signal.posted_price} → live {signal.live_ask ?? '—'}
        </span>
        <span className={statusTone}>{label}</span>
      </div>
    </div>
  )
}
