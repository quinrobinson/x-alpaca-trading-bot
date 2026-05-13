import { useState } from 'react'
import { fmtMoney, fmtPct, pnlColorClass } from '../../util'
import BrandMark from './BrandMark.jsx'

/**
 * Sticky header — APDF dark.
 *
 *   [X]  x-alpaca-trading-bot                ● running   +$0.50   ▾
 *
 * Brand mark + wordmark on the left, status dot + P&L + details
 * toggle on the right. Sits flush against the page background — no
 * white strip — separated from content by a thin border.
 */
export default function Header({
  wsStatus,
  health,
  performance,
  killSwitches = [],
  startingEquity = 100000,
  dailyLossKillPct = 0.03,
}) {
  const [open, setOpen] = useState(false)

  const xDisabled = health?.x_stream_disabled === true
  const fatalSwitches = killSwitches.filter(
    (s) => !(xDisabled && s === 'x_stream_disconnected'),
  )

  let dotColor, statusText, glow
  if (fatalSwitches.length > 0) {
    dotColor = 'var(--negative)'; statusText = 'paused'
    glow = 'rgba(239,68,68,0.20)'
  } else if (wsStatus !== 'open') {
    dotColor = 'var(--warning)'; statusText = 'connecting'
    glow = 'rgba(245,158,11,0.20)'
  } else {
    dotColor = 'var(--positive)'; statusText = 'running'
    glow = 'rgba(34,197,94,0.20)'
  }

  const totalPnl = performance?.stats?.total_pnl
  const pnlNum = totalPnl != null ? Number(totalPnl) : null

  return (
    <header
      className="sticky top-0 z-10 bg-bg/90 backdrop-blur border-b border-border"
    >
      <div className="px-4 lg:px-6 py-3.5 flex items-center gap-3">
        {/* Brand */}
        <div className="flex items-center gap-2.5 min-w-0">
          <BrandMark size={28} />
          <div className="flex flex-col leading-none min-w-0">
            <span
              className="font-display font-semibold text-fg tracking-tight truncate"
              style={{ fontSize: 14, letterSpacing: '-0.01em' }}
            >
              x-alpaca-trading-bot
            </span>
            <span
              className="mono-label mt-1 truncate"
              style={{ fontSize: 9, letterSpacing: '0.18em' }}
            >
              tweets · options · paper
            </span>
          </div>
        </div>

        {/* Right cluster */}
        <div className="ml-auto flex items-center gap-3 shrink-0">
          <div className="flex items-center gap-2">
            <span
              className="inline-block w-2 h-2 rounded-full"
              style={{
                background: dotColor,
                boxShadow: `0 0 0 3px ${glow}`,
              }}
            />
            <span
              className="font-mono uppercase tracking-wider text-fg-muted"
              style={{ fontSize: 10, letterSpacing: '0.16em' }}
            >
              {statusText}
            </span>
          </div>

          {pnlNum !== null && (
            <span className={`text-sm font-mono ${pnlColorClass(pnlNum)}`}>
              {fmtMoney(pnlNum)}
            </span>
          )}

          <button
            onClick={() => setOpen((o) => !o)}
            className="text-fg-dim hover:text-fg text-xs flex items-center gap-1 transition-colors"
            aria-expanded={open}
            aria-label="Toggle system status details"
          >
            <span
              className="font-mono uppercase tracking-wider"
              style={{ fontSize: 10, letterSpacing: '0.16em' }}
            >
              details
            </span>
            <svg
              className={`w-3 h-3 transition-transform ${open ? 'rotate-180' : ''}`}
              viewBox="0 0 12 12"
            >
              <path d="M3 4.5l3 3 3-3" stroke="currentColor" strokeWidth="1.5" fill="none" />
            </svg>
          </button>
        </div>
      </div>

      {open && (
        <SystemStatus
          wsStatus={wsStatus}
          health={health}
          performance={performance}
          killSwitches={killSwitches}
          xDisabled={xDisabled}
          startingEquity={startingEquity}
          dailyLossKillPct={dailyLossKillPct}
        />
      )}
    </header>
  )
}

function SystemStatus({
  wsStatus, health, performance, killSwitches,
  xDisabled, startingEquity, dailyLossKillPct,
}) {
  const xLabel = xDisabled
    ? { text: 'disabled', tone: 'text-warning' }
    : killSwitches.includes('x_stream_disconnected')
    ? { text: 'down', tone: 'text-negative' }
    : { text: 'connected', tone: 'text-positive' }

  const alpacaDown = killSwitches.includes('alpaca_disconnected')
  const realizedPnl = Number(performance?.stats?.total_pnl ?? 0)
  const dailyLossLimit = startingEquity * dailyLossKillPct
  const progressPct = dailyLossLimit > 0
    ? Math.min(100, Math.max(0, (Math.max(0, -realizedPnl) / dailyLossLimit) * 100))
    : 0

  return (
    <div className="px-4 lg:px-6 pb-4 border-t border-border grid grid-cols-2 sm:grid-cols-4 gap-3 pt-3">
      <Stat label="X stream" value={xLabel.text} tone={xLabel.tone} />
      <Stat
        label="Alpaca"
        value={alpacaDown ? 'down' : 'connected'}
        tone={alpacaDown ? 'text-negative' : 'text-positive'}
      />
      <Stat label="Market" value={health?.market_open ? 'open' : 'closed'} />
      <Stat
        label="WebSocket"
        value={wsStatus}
        tone={wsStatus === 'open' ? 'text-positive' : 'text-warning'}
      />

      <div className="col-span-2 sm:col-span-4 mt-1">
        <div className="flex justify-between mono-label mb-1.5" style={{ fontSize: 10 }}>
          <span>Daily loss</span>
          <span className="normal-case tracking-normal text-fg-dim">
            limit {fmtMoney(-dailyLossLimit)} ({fmtPct(-dailyLossKillPct)})
          </span>
        </div>
        <div className="h-1.5 rounded-full bg-elevated overflow-hidden">
          <div
            className={`h-full transition-all ${progressPct >= 80 ? 'bg-negative' : 'bg-warning'}`}
            style={{ width: `${progressPct}%` }}
          />
        </div>
      </div>
    </div>
  )
}

function Stat({ label, value, tone = 'text-fg' }) {
  return (
    <div>
      <div className="mono-label" style={{ fontSize: 10 }}>{label}</div>
      <div className={`text-sm font-medium mt-0.5 ${tone}`}>{value}</div>
    </div>
  )
}
