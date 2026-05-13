import { useState } from 'react'
import { fmtMoney, fmtPct, pnlColorClass } from '../../util'

/**
 * Sticky header. One row, one answerable question: is the bot running?
 *
 *   ● running        +$0.50  ▾
 *
 * Tapping the chevron reveals the system-status drawer (X stream, Alpaca,
 * WS, daily-loss progress). Everything that USED to live across the top
 * bar is one tap away but not in your face.
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

  // Operator-paused via DISABLE_X_STREAM doesn't count as "kill switch"
  const xDisabled = health?.x_stream_disabled === true
  const fatalSwitches = killSwitches.filter(
    (s) => !(xDisabled && s === 'x_stream_disconnected'),
  )

  let dotClass, statusText
  if (fatalSwitches.length > 0) {
    dotClass = 'bg-rose-400'
    statusText = 'paused'
  } else if (wsStatus !== 'open') {
    dotClass = 'bg-amber-400'
    statusText = 'connecting'
  } else {
    dotClass = 'bg-emerald-400'
    statusText = 'running'
  }

  const totalPnl = performance?.stats?.total_pnl
  const pnlNum = totalPnl != null ? Number(totalPnl) : null

  return (
    <header className="sticky top-0 z-10 bg-slate-950/95 backdrop-blur border-b border-slate-800">
      <div className="px-4 py-3 flex items-center gap-3">
        <span className={`inline-block w-2.5 h-2.5 rounded-full ${dotClass}`} />
        <span className="text-sm font-medium">{statusText}</span>

        <div className="ml-auto flex items-center gap-3">
          {pnlNum !== null && (
            <span className={`text-sm font-mono ${pnlColorClass(pnlNum)}`}>
              {fmtMoney(pnlNum)}
            </span>
          )}
          <button
            onClick={() => setOpen((o) => !o)}
            className="text-slate-400 hover:text-slate-100 text-xs flex items-center gap-1"
            aria-expanded={open}
            aria-label="Toggle system status details"
          >
            details
            <svg
              className={`w-3 h-3 transition-transform ${open ? 'rotate-180' : ''}`}
              viewBox="0 0 12 12" fill="currentColor"
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
    ? { text: 'disabled', tone: 'text-amber-400' }
    : killSwitches.includes('x_stream_disconnected')
    ? { text: 'down', tone: 'text-rose-400' }
    : { text: 'connected', tone: 'text-emerald-400' }

  const alpacaDown = killSwitches.includes('alpaca_disconnected')
  const realizedPnl = Number(performance?.stats?.total_pnl ?? 0)
  const dailyLossLimit = startingEquity * dailyLossKillPct
  const progressPct = dailyLossLimit > 0
    ? Math.min(100, Math.max(0, (Math.max(0, -realizedPnl) / dailyLossLimit) * 100))
    : 0

  return (
    <div className="px-4 pb-3 border-t border-slate-800/60 grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs pt-3">
      <Stat label="X stream" value={xLabel.text} tone={xLabel.tone} />
      <Stat label="Alpaca" value={alpacaDown ? 'down' : 'connected'}
            tone={alpacaDown ? 'text-rose-400' : 'text-emerald-400'} />
      <Stat label="Market" value={health?.market_open ? 'open' : 'closed'} />
      <Stat label="WebSocket" value={wsStatus}
            tone={wsStatus === 'open' ? 'text-emerald-400' : 'text-amber-400'} />

      <div className="col-span-2 sm:col-span-4 mt-1">
        <div className="flex justify-between text-[10px] uppercase text-slate-500 mb-1">
          <span>Daily loss</span>
          <span>limit {fmtMoney(-dailyLossLimit)} ({fmtPct(-dailyLossKillPct)})</span>
        </div>
        <div className="h-1.5 rounded bg-slate-800 overflow-hidden">
          <div
            className={`h-full ${progressPct >= 80 ? 'bg-rose-500' : 'bg-amber-400'} transition-all`}
            style={{ width: `${progressPct}%` }}
          />
        </div>
      </div>
    </div>
  )
}

function Stat({ label, value, tone = 'text-slate-100' }) {
  return (
    <div>
      <div className="text-[10px] uppercase text-slate-500">{label}</div>
      <div className={`text-sm font-medium ${tone}`}>{value}</div>
    </div>
  )
}
