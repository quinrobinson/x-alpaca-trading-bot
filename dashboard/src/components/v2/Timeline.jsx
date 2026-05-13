import { fmtMoney, fmtPct, fmtRelative, pnlColorClass } from '../../util'

/**
 * Timeline — Hyper light cards, each pairing a tweet with its outcome.
 * A 3px colored bar on the left indicates the entry kind.
 */
export default function Timeline({ items = [], showRejected, onToggleRejected }) {
  const visible = showRejected
    ? items
    : items.filter((i) => i.kind !== 'signal_rejected' && i.kind !== 'signal_unactionable')

  return (
    <section className="space-y-3">
      <header className="flex items-center justify-between px-1">
        <h2 className="mono-label" style={{ fontSize: 11 }}>Timeline</h2>
        <label className="flex items-center gap-2 text-xs text-ink-600 cursor-pointer select-none">
          <input
            type="checkbox"
            checked={showRejected}
            onChange={(e) => onToggleRejected(e.target.checked)}
            className="accent-brand w-3.5 h-3.5"
          />
          <span>Show skipped</span>
        </label>
      </header>

      {visible.length === 0 && (
        <div
          className="bg-surface rounded-card px-6 py-8 text-center text-sm text-ink-500"
          style={{ boxShadow: 'var(--shadow-card)' }}
        >
          {items.length === 0
            ? 'No signals yet.'
            : `${items.length} skipped — toggle "Show skipped" to view.`}
        </div>
      )}

      {visible.map((item) => (
        <TimelineCard key={item.x_post_id} item={item} />
      ))}
    </section>
  )
}


function TimelineCard({ item }) {
  switch (item.kind) {
    case 'trade_closed': return <TradeCard item={item} />
    case 'position_open': return <PositionOpenCard item={item} />
    case 'signal_rejected': return <RejectedCard item={item} />
    case 'signal_unactionable': return <UnactionableCard item={item} />
    default: return null
  }
}


function CardShell({ accent, children, dim = false }) {
  return (
    <article
      className="bg-surface rounded-card p-4 relative overflow-hidden"
      style={{
        boxShadow: 'var(--shadow-card)',
        opacity: dim ? 0.85 : 1,
      }}
    >
      <span
        aria-hidden="true"
        className="absolute left-0 top-0 bottom-0 w-1"
        style={{ background: accent }}
      />
      <div className="pl-2">{children}</div>
    </article>
  )
}


function TradeCard({ item }) {
  const pnlPct = Number(item.trade.pnl_pct)
  const pnl = Number(item.trade.gross_pnl)
  const isWin = pnl > 0
  return (
    <CardShell accent={isWin ? 'var(--green-500)' : 'var(--danger)'}>
      <div className="flex items-baseline justify-between gap-3">
        <div className="mono-label" style={{ fontSize: 10 }}>
          {isWin ? '✓ closed · win' : '✗ closed · loss'} · {item.trade.exit_reason}
        </div>
        <div className="text-ink-500" style={{ fontSize: 11 }}>
          {fmtRelative(item.trade.closed_at)}
        </div>
      </div>
      <div className="mt-1 flex items-baseline gap-3">
        <span className={`text-lg font-bold tracking-tight ${pnlColorClass(pnlPct)}`}>
          {fmtPct(pnlPct)}
        </span>
        <span className={`text-sm font-mono ${pnlColorClass(pnl)}`}>{fmtMoney(pnl)}</span>
        <span className="text-xs text-ink-500 ml-auto font-mono">{item.trade.hold_minutes}m hold</span>
      </div>
      <p className="mt-2 text-sm text-ink-700 leading-snug">"{item.post_text}"</p>
      <div className="mt-2 flex items-center gap-3 text-xs text-ink-500 font-mono">
        <span className="text-ink-700">
          {item.signal.ticker} ${item.signal.strike} {item.signal.option_type?.[0]?.toUpperCase()}
        </span>
        <span>{item.signal.expiration}</span>
        <span className="ml-auto">
          entry {item.trade.entry_price} → exit {item.trade.exit_price}
        </span>
      </div>
    </CardShell>
  )
}


function PositionOpenCard({ item }) {
  return (
    <CardShell accent="var(--amber-500)">
      <div className="flex items-baseline justify-between gap-3">
        <div className="mono-label text-warning" style={{ fontSize: 10 }}>◐ in trade</div>
        <div className="text-ink-500" style={{ fontSize: 11 }}>
          {fmtRelative(item.signal.parsed_at)}
        </div>
      </div>
      <p className="mt-2 text-sm text-ink-700 leading-snug">"{item.post_text}"</p>
      <div className="mt-2 flex items-center gap-3 text-xs text-ink-500 font-mono">
        <span className="text-ink-700">
          {item.signal.ticker} ${item.signal.strike} {item.signal.option_type?.[0]?.toUpperCase()}
        </span>
        <span>{item.signal.expiration}</span>
        <span className="ml-auto">
          posted {item.signal.posted_price} → live {item.signal.live_ask ?? '—'}
        </span>
      </div>
    </CardShell>
  )
}


function RejectedCard({ item }) {
  return (
    <CardShell accent="var(--ink-300)" dim>
      <div className="flex items-baseline justify-between gap-3">
        <div className="mono-label" style={{ fontSize: 10 }}>
          ✗ skipped · {item.signal.rejection_reason}
        </div>
        <div className="text-ink-500" style={{ fontSize: 11 }}>
          {fmtRelative(item.signal.parsed_at)}
        </div>
      </div>
      <p className="mt-2 text-sm text-ink-600 leading-snug">"{item.post_text}"</p>
      <div className="mt-2 flex items-center gap-3 text-xs text-ink-500 font-mono">
        <span>
          {item.signal.ticker} ${item.signal.strike} {item.signal.option_type?.[0]?.toUpperCase()}
        </span>
        <span>{item.signal.expiration}</span>
        {item.signal.live_ask && (
          <span className="ml-auto">
            posted {item.signal.posted_price} vs live {item.signal.live_ask}
          </span>
        )}
      </div>
    </CardShell>
  )
}


function UnactionableCard({ item }) {
  return (
    <article
      className="rounded-card px-4 py-3 bg-surface-2 border border-hairline"
    >
      <div className="flex items-baseline justify-between gap-3">
        <div className="mono-label text-ink-400" style={{ fontSize: 10 }}>not a signal</div>
        <div className="text-ink-400" style={{ fontSize: 11 }}>{fmtRelative(item.posted_at)}</div>
      </div>
      <p className="text-xs text-ink-500 mt-1 leading-snug">"{item.post_text}"</p>
    </article>
  )
}
