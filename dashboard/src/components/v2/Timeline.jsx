import { fmtMoney, fmtPct, fmtRelative, pnlColorClass } from '../../util'

/**
 * Timeline — APDF dark cards, grouped by day.
 * Day eyebrows ("TODAY", "YESTERDAY", "MAY 11") anchor scrolling so the
 * timeline reads like a log instead of an undifferentiated stack.
 */
export default function Timeline({ items = [], showRejected, onToggleRejected }) {
  const visible = showRejected
    ? items
    : items.filter((i) => i.kind !== 'signal_rejected' && i.kind !== 'signal_unactionable')

  const groups = groupByDay(visible)

  return (
    <section>
      <header className="flex items-center justify-between px-1 mb-3">
        <h2 className="mono-label" style={{ fontSize: 11 }}>Timeline</h2>
        <label className="flex items-center gap-2 text-xs text-fg-muted cursor-pointer select-none">
          <input
            type="checkbox"
            checked={showRejected}
            onChange={(e) => onToggleRejected(e.target.checked)}
            className="accent-brand-purple w-3.5 h-3.5"
          />
          <span>Show skipped</span>
        </label>
      </header>

      {visible.length === 0 && (
        <div className="card px-6 py-8 text-center text-sm text-fg-dim">
          {items.length === 0
            ? 'No signals yet.'
            : `${items.length} skipped — toggle "Show skipped" to view.`}
        </div>
      )}

      <div className="space-y-6">
        {groups.map((group) => (
          <DayGroup key={group.key} label={group.label} count={group.items.length}>
            {group.items.map((item) => (
              <TimelineEntry key={item.x_post_id} item={item} />
            ))}
          </DayGroup>
        ))}
      </div>
    </section>
  )
}


function DayGroup({ label, count, children }) {
  return (
    <div>
      <div className="flex items-baseline justify-between px-1 pb-2 mb-2 border-b border-border">
        <span
          className="mono-label text-fg-muted"
          style={{ fontSize: 10, letterSpacing: '0.18em' }}
        >
          {label}
        </span>
        <span
          className="font-mono text-fg-faint"
          style={{ fontSize: 10, letterSpacing: '0.08em' }}
        >
          {count}
        </span>
      </div>
      <div className="space-y-3">{children}</div>
    </div>
  )
}


function TimelineEntry({ item }) {
  switch (item.kind) {
    case 'trade_closed': return <TradeCard item={item} />
    case 'position_open': return <PositionOpenCard item={item} />
    case 'signal_rejected': return <RejectedCard item={item} />
    case 'signal_unactionable': return <UnactionableCard item={item} />
    default: return null
  }
}


/**
 * Tinted-outline card.
 *   - tone: 'win' | 'loss' | 'open' | 'default'
 *   - dim:  fade overall opacity for skipped entries
 */
function CardShell({ tone = 'default', dim = false, children }) {
  const ring = {
    win:     'rgba(34,197,94,0.45)',
    loss:    'rgba(239,68,68,0.45)',
    open:    'rgba(245,158,11,0.45)',
    default: 'var(--border)',
  }[tone]

  const glow = {
    win:     'rgba(34,197,94,0.08)',
    loss:    'rgba(239,68,68,0.08)',
    open:    'rgba(245,158,11,0.08)',
    default: 'transparent',
  }[tone]

  return (
    <article
      className="rounded-card p-4"
      style={{
        background: 'var(--card)',
        border: `1px solid ${ring}`,
        boxShadow: glow !== 'transparent' ? `0 0 0 1px ${glow}` : 'none',
        opacity: dim ? 0.75 : 1,
      }}
    >
      {children}
    </article>
  )
}


function Dot({ color }) {
  return (
    <span
      className="inline-block w-1.5 h-1.5 rounded-full mr-1.5 align-middle shrink-0"
      style={{ background: color }}
      aria-hidden="true"
    />
  )
}


function TradeCard({ item }) {
  const pnlPct = Number(item.trade.pnl_pct)
  const pnl = Number(item.trade.gross_pnl)
  const isWin = pnl > 0
  return (
    <CardShell tone={isWin ? 'win' : 'loss'}>
      <div className="flex items-baseline justify-between gap-3">
        <div className="mono-label" style={{ fontSize: 10 }}>
          <Dot color={isWin ? 'var(--positive)' : 'var(--negative)'} />
          {isWin ? 'closed · win' : 'closed · loss'} · {item.trade.exit_reason}
        </div>
        <div className="text-fg-dim" style={{ fontSize: 11 }}>
          {fmtRelative(item.trade.closed_at)}
        </div>
      </div>
      <div className="mt-1.5 flex items-baseline gap-3">
        <span className={`text-lg font-bold tracking-tight ${pnlColorClass(pnlPct)}`}>
          {fmtPct(pnlPct)}
        </span>
        <span className={`text-sm font-mono ${pnlColorClass(pnl)}`}>{fmtMoney(pnl)}</span>
        <span className="text-xs text-fg-dim ml-auto font-mono">{item.trade.hold_minutes}m hold</span>
      </div>
      <p className="mt-2.5 text-sm text-fg-muted leading-snug">"{item.post_text}"</p>
      <div className="mt-2.5 flex items-center gap-3 text-xs text-fg-dim font-mono">
        <span className="text-fg">
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
    <CardShell tone="open">
      <div className="flex items-baseline justify-between gap-3">
        <div className="mono-label text-warning" style={{ fontSize: 10 }}>
          <Dot color="var(--warning)" />
          in trade
        </div>
        <div className="text-fg-dim" style={{ fontSize: 11 }}>
          {fmtRelative(item.signal.parsed_at)}
        </div>
      </div>
      <p className="mt-2.5 text-sm text-fg-muted leading-snug">"{item.post_text}"</p>
      <div className="mt-2.5 flex items-center gap-3 text-xs text-fg-dim font-mono">
        <span className="text-fg">
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
    <CardShell tone="default" dim>
      <div className="flex items-baseline justify-between gap-3">
        <div className="mono-label" style={{ fontSize: 10 }}>
          <Dot color="var(--fg-faint)" />
          skipped · {item.signal.rejection_reason}
        </div>
        <div className="text-fg-dim" style={{ fontSize: 11 }}>
          {fmtRelative(item.signal.parsed_at)}
        </div>
      </div>
      <p className="mt-2.5 text-sm text-fg-muted leading-snug">"{item.post_text}"</p>
      <div className="mt-2.5 flex items-center gap-3 text-xs text-fg-dim font-mono">
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
      className="rounded-card px-4 py-3"
      style={{
        background: 'transparent',
        border: '1px dashed var(--border)',
      }}
    >
      <div className="flex items-baseline justify-between gap-3">
        <div className="mono-label text-fg-faint" style={{ fontSize: 10 }}>not a signal</div>
        <div className="text-fg-faint" style={{ fontSize: 11 }}>{fmtRelative(item.posted_at)}</div>
      </div>
      <p className="text-xs text-fg-dim mt-1 leading-snug">"{item.post_text}"</p>
    </article>
  )
}


/* -------------------------------------------------------------------------
   Grouping helpers
   ----------------------------------------------------------------------- */

function itemTimestamp(item) {
  if (item.kind === 'trade_closed') return item.trade?.closed_at
  if (item.kind === 'position_open' || item.kind === 'signal_rejected') return item.signal?.parsed_at
  if (item.kind === 'signal_unactionable') return item.posted_at
  return null
}

function groupByDay(items, now = new Date()) {
  const today = startOfDay(now)
  const yesterday = startOfDay(new Date(today.getTime() - 86_400_000))

  const groups = []
  let current = null

  for (const item of items) {
    const ts = itemTimestamp(item)
    if (!ts) continue
    const d = new Date(ts)
    if (!Number.isFinite(d.getTime())) continue
    const day = startOfDay(d)
    const key = day.toISOString().slice(0, 10)
    if (!current || current.key !== key) {
      current = {
        key,
        label: dayLabel(day, today, yesterday),
        items: [],
      }
      groups.push(current)
    }
    current.items.push(item)
  }
  return groups
}

function startOfDay(date) {
  const d = new Date(date)
  d.setHours(0, 0, 0, 0)
  return d
}

function dayLabel(day, today, yesterday) {
  if (day.getTime() === today.getTime()) return 'TODAY'
  if (day.getTime() === yesterday.getTime()) return 'YESTERDAY'
  return day
    .toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
    .toUpperCase()
}
