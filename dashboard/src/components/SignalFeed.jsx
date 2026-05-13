import { fmtTime } from '../util'

/**
 * Panel 2 — Signal Feed (left column).
 *
 * Live stream of recent X posts with parse + validation + execution
 * status. Color coding (architecture brief):
 *   green  = trade taken
 *   yellow = parsed but skipped (validation failed)
 *   gray   = not a trade signal
 */
export default function SignalFeed({ signals = [] }) {
  return (
    <div className="bg-slate-900 border border-slate-800 rounded-lg p-4 h-full flex flex-col min-h-0">
      <div className="flex items-center justify-between mb-3">
        <h2 className="text-sm font-semibold uppercase tracking-wide">Signal feed</h2>
        <span className="text-xs text-slate-500">{signals.length} recent</span>
      </div>
      <div className="flex-1 overflow-y-auto space-y-2 min-h-0">
        {signals.length === 0 && (
          <div className="text-sm text-slate-500 py-4">No signals yet.</div>
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
  const accentClass = isTaken
    ? 'border-l-emerald-500 bg-emerald-500/5'
    : isRejected
    ? 'border-l-amber-400 bg-amber-400/5'
    : 'border-l-slate-700'
  const statusLabel = isTaken
    ? 'TAKEN'
    : isRejected
    ? `SKIP · ${signal.rejection_reason}`
    : 'pending'

  return (
    <div className={`rounded border-l-2 ${accentClass} px-3 py-2 text-xs`}>
      <div className="flex items-center justify-between">
        <span className="font-mono text-slate-300">{signal.ticker} {signal.option_type?.toUpperCase()?.[0]} ${signal.strike} {signal.expiration}</span>
        <span className="text-slate-500">{fmtTime(signal.parsed_at)}</span>
      </div>
      <div className="mt-1 flex items-center justify-between text-[11px]">
        <span className="text-slate-400">
          posted {signal.posted_price} → live {signal.live_ask ?? '—'}
        </span>
        <span className={isTaken ? 'text-emerald-400' : isRejected ? 'text-amber-400' : 'text-slate-500'}>
          {statusLabel}
        </span>
      </div>
    </div>
  )
}
