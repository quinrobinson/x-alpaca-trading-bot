import { useState } from 'react'
import { fmtMoney, fmtPct, pnlColorClass } from '../../util'

/**
 * Sticky header. One row, one answerable question: is the bot running?
 *
 *   ● running        +$0.50  ▾
 *
 * Tapping the chevron reveals the system-status drawer.
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

  let dotClass, statusText
  if (fatalSwitches.length > 0) {
    dotClass = 'bg-negative'
    statusText = 'paused'
  } else if (wsStatus !== 'open') {
    dotClass = 'bg-warning'
    statusText = 'connecting'
  } else {
    dotClass = 'bg-positive'
    statusText = 'running'
  }

  const totalPnl = performance?.stats?.total_pnl
  const pnlNum = totalPnl != null ? Number(totalPnl) : null

  return (
    <header className="sticky top-0 z-10 bg-surface/95 backdrop-blur border-b border-hairline">
      <div className="px-4 py-3 flex items-center gap-3">
        <span
          className={`inline-block w-2.5 h-2.5 rounded-full ${dotClass}`}
          style={{
            boxShadow: `0 0 0 3px ${
              fatalSwitches.length > 0
                ? 'rgba(229,72,77,0.18)'
                : wsStatus !== 'open'
                ? 'rgba(214,154,10,0.18)'
                : 'rgba(31,167,74,0.18)'
            }`,
          }}
        />
        <span className="text-sm font-semibold text-ink-900 tracking-tight">{statusText}</span>

        <div className="ml-auto flex items-center gap-3">
          {pnlNum !== null && (
            <span className={`text-sm font-mono ${pnlColorClass(pnlNum)}`}>
              {fmtMoney(pnlNum)}
            </span>
          )}
          <button
            onClick={() => setOpen((o) => !o)}
            className="text-ink-500 hover:text-ink-900 text-xs flex items-center gap-1 transition-colors"
            aria-expanded={open}
            aria-label="Toggle system status details"
          >
            <span className="font-mono uppercase tracking-wider text-[10px]">details</span>
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
    <div className="px-4 pb-4 border-t border-hairline grid grid-cols-2 sm:grid-cols-4 gap-3 pt-3">
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
          <span className="normal-case tracking-normal text-ink-500">
            limit {fmtMoney(-dailyLossLimit)} ({fmtPct(-dailyLossKillPct)})
          </span>
        </div>
        <div className="h-1.5 rounded-full bg-ink-100 overflow-hidden">
          <div
            className={`h-full transition-all ${progressPct >= 80 ? 'bg-negative' : 'bg-warning'}`}
            style={{ width: `${progressPct}%` }}
          />
        </div>
      </div>
    </div>
  )
}

function Stat({ label, value, tone = 'text-ink-900' }) {
  return (
    <div>
      <div className="mono-label" style={{ fontSize: 10 }}>{label}</div>
      <div className={`text-sm font-medium mt-0.5 ${tone}`}>{value}</div>
    </div>
  )
}
