import { fmtTime } from '../util'

/**
 * Legacy signal feed (used in /details). Hyper light tokens.
 */
export default function SignalFeed({ signals = [] }) {
  return (
    <div
      className="bg-surface rounded-card p-5 h-full flex flex-col min-h-0"
      style={{ boxShadow: 'var(--shadow-card)' }}
    >
      <div className="flex items-center justify-between mb-3">
        <h2 className="mono-label" style={{ fontSize: 11 }}>Signal feed</h2>
        <span className="text-xs text-ink-500">{signals.length} recent</span>
      </div>
      <div className="flex-1 overflow-y-auto space-y-2 min-h-0">
        {signals.length === 0 && (
          <div className="text-sm text-ink-500 py-4">No signals yet.</div>
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

  let accent, label, statusTone
  if (isTaken) {
    accent = 'var(--green-500)'
    label = 'TAKEN'
    statusTone = 'text-positive'
  } else if (isRejected) {
    accent = 'var(--amber-500)'
    label = `SKIP · ${signal.rejection_reason}`
    statusTone = 'text-warning'
  } else {
    accent = 'var(--ink-300)'
    label = 'pending'
    statusTone = 'text-ink-500'
  }

  return (
    <div
      className="rounded-xl bg-surface-2 px-3 py-2.5 text-xs relative overflow-hidden border border-hairline"
    >
      <span
        aria-hidden="true"
        className="absolute left-0 top-0 bottom-0 w-1"
        style={{ background: accent }}
      />
      <div className="pl-2">
        <div className="flex items-center justify-between">
          <span className="font-mono text-ink-700">
            {signal.ticker} {signal.option_type?.toUpperCase()?.[0]} ${signal.strike} {signal.expiration}
          </span>
          <span className="text-ink-500">{fmtTime(signal.parsed_at)}</span>
        </div>
        <div className="mt-1 flex items-center justify-between" style={{ fontSize: 11 }}>
          <span className="text-ink-600">
            posted {signal.posted_price} → live {signal.live_ask ?? '—'}
          </span>
          <span className={statusTone}>{label}</span>
        </div>
      </div>
    </div>
  )
}
