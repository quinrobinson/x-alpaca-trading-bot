import { useState } from 'react'
import { fmtMoney, fmtPct, fmtRelative, pnlColorClass } from '../../util'

/**
 * Timeline — the main view. Each card pairs a tweet with its outcome.
 *
 * Cards adapt to the `kind`:
 *   trade_closed       — green/red P&L, exit reason, hold duration
 *   position_open      — "in trade" badge, posted price → live ask
 *   signal_rejected    — gray, shows rejection reason inline
 *   signal_unactionable — minimal, just the tweet + "not a signal"
 */
export default function Timeline({ items = [], showRejected, onToggleRejected }) {
  const visible = showRejected
    ? items
    : items.filter((i) => i.kind !== 'signal_rejected' && i.kind !== 'signal_unactionable')

  return (
    <section className="space-y-3">
      <header className="flex items-center justify-between">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-300">
          Timeline
        </h2>
        <label className="flex items-center gap-2 text-xs text-slate-400 cursor-pointer">
          <input
            type="checkbox"
            checked={showRejected}
            onChange={(e) => onToggleRejected(e.target.checked)}
            className="accent-slate-400"
          />
          Show skipped
        </label>
      </header>

      {visible.length === 0 && (
        <div className="bg-slate-900 border border-slate-800 rounded-lg p-6 text-sm text-slate-500 text-center">
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


function TradeCard({ item }) {
  const pnlPct = Number(item.trade.pnl_pct)
  const pnl = Number(item.trade.gross_pnl)
  const isWin = pnl > 0
  return (
    <article className={`bg-slate-900 border-l-2 ${isWin ? 'border-l-emerald-500' : 'border-l-rose-500'} border border-slate-800 rounded-lg p-3`}>
      <div className="flex items-baseline justify-between gap-3">
        <div className="text-[10px] uppercase font-medium text-slate-400">
          {isWin ? '✓ closed (win)' : '✗ closed (loss)'} · {item.trade.exit_reason}
        </div>
        <div className="text-[10px] text-slate-500">{fmtRelative(item.trade.closed_at)}</div>
      </div>
      <div className="mt-1 flex items-baseline gap-3">
        <span className={`text-base font-bold ${pnlColorClass(pnlPct)}`}>{fmtPct(pnlPct)}</span>
        <span className={`text-xs ${pnlColorClass(pnl)}`}>{fmtMoney(pnl)}</span>
        <span className="text-xs text-slate-400 ml-auto">{item.trade.hold_minutes}m hold</span>
      </div>
      <p className="mt-2 text-sm italic text-slate-200 truncate">"{item.post_text}"</p>
      <div className="mt-1 flex items-center gap-3 text-[11px] text-slate-400 font-mono">
        <span>{item.signal.ticker} ${item.signal.strike} {item.signal.option_type?.[0]?.toUpperCase()}</span>
        <span>{item.signal.expiration}</span>
        <span className="ml-auto">
          entry {item.trade.entry_price} → exit {item.trade.exit_price}
        </span>
      </div>
    </article>
  )
}


function PositionOpenCard({ item }) {
  return (
    <article className="bg-slate-900 border-l-2 border-l-amber-400 border border-slate-800 rounded-lg p-3">
      <div className="flex items-baseline justify-between gap-3">
        <div className="text-[10px] uppercase font-medium text-amber-300">
          ◐ in trade
        </div>
        <div className="text-[10px] text-slate-500">{fmtRelative(item.signal.parsed_at)}</div>
      </div>
      <p className="mt-2 text-sm italic text-slate-200 truncate">"{item.post_text}"</p>
      <div className="mt-1 flex items-center gap-3 text-[11px] text-slate-400 font-mono">
        <span>{item.signal.ticker} ${item.signal.strike} {item.signal.option_type?.[0]?.toUpperCase()}</span>
        <span>{item.signal.expiration}</span>
        <span className="ml-auto">
          posted {item.signal.posted_price} → live {item.signal.live_ask ?? '—'}
        </span>
      </div>
    </article>
  )
}


function RejectedCard({ item }) {
  return (
    <article className="bg-slate-900/60 border-l-2 border-l-slate-700 border border-slate-800 rounded-lg p-3">
      <div className="flex items-baseline justify-between gap-3">
        <div className="text-[10px] uppercase font-medium text-slate-500">
          ✗ skipped · {item.signal.rejection_reason}
        </div>
        <div className="text-[10px] text-slate-500">{fmtRelative(item.signal.parsed_at)}</div>
      </div>
      <p className="mt-2 text-sm italic text-slate-400 truncate">"{item.post_text}"</p>
      <div className="mt-1 flex items-center gap-3 text-[11px] text-slate-500 font-mono">
        <span>{item.signal.ticker} ${item.signal.strike} {item.signal.option_type?.[0]?.toUpperCase()}</span>
        <span>{item.signal.expiration}</span>
        {item.signal.live_ask && (
          <span className="ml-auto">posted {item.signal.posted_price} vs live {item.signal.live_ask}</span>
        )}
      </div>
    </article>
  )
}


function UnactionableCard({ item }) {
  return (
    <article className="bg-slate-900/40 border border-slate-800/60 rounded-lg px-3 py-2">
      <div className="flex items-baseline justify-between gap-3">
        <div className="text-[10px] uppercase text-slate-600">not a signal</div>
        <div className="text-[10px] text-slate-600">{fmtRelative(item.posted_at)}</div>
      </div>
      <p className="text-xs italic text-slate-500 truncate mt-1">"{item.post_text}"</p>
    </article>
  )
}
