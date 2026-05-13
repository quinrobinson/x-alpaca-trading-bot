import { fmtMoney, fmtPct, fmtRelative, pnlColorClass } from '../util'

/**
 * Legacy detailed status bar (used in /details view). APDF dark tokens.
 */
export default function StatusBar({
  wsStatus,
  health,
  performance,
  lastSignalTs,
  killSwitches = [],
  dailyLossKillPct = 0.03,
  startingEquity = 100000,
}) {
  const realizedPnl = Number(performance?.stats?.total_pnl ?? 0)
  const dailyLossLimit = startingEquity * dailyLossKillPct
  const progressPct = dailyLossLimit > 0
    ? Math.min(100, Math.max(0, (Math.max(0, -realizedPnl) / dailyLossLimit) * 100))
    : 0

  const xDisabled = health?.x_stream_disabled === true
  const fatalSwitches = killSwitches.filter(
    (s) => !(xDisabled && s === 'x_stream_disconnected'),
  )
  const switchesOn = fatalSwitches.length > 0
  const botStatus = switchesOn ? 'paused (kill switch)' : 'running'
  const botColor = switchesOn ? 'text-negative' : 'text-positive'

  const xStreamLabel = xDisabled
    ? { text: 'disabled', tone: 'text-warning' }
    : killSwitches.includes('x_stream_disconnected')
    ? { text: 'down', tone: 'text-negative' }
    : { text: 'connected', tone: 'text-positive' }

  return (
    <div className="bg-surface border-b border-border px-4 py-4">
      <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-7 gap-4 items-center">
        <Cell label="Bot" value={botStatus} tone={botColor} />
        <Cell label="Market" value={health?.market_open ? 'open' : 'closed'} />
        <Cell label="X stream" value={xStreamLabel.text} tone={xStreamLabel.tone} />
        <Cell
          label="Alpaca"
          value={killSwitches.includes('alpaca_disconnected') ? 'down' : 'connected'}
          tone={killSwitches.includes('alpaca_disconnected') ? 'text-negative' : 'text-positive'}
        />
        <Cell
          label="WS"
          value={wsStatus}
          tone={wsStatus === 'open' ? 'text-positive' : 'text-warning'}
        />
        <Cell
          label="P&L today"
          value={`${fmtMoney(realizedPnl)} (${fmtPct(realizedPnl / startingEquity)})`}
          tone={pnlColorClass(realizedPnl)}
        />
        <Cell label="Last signal" value={fmtRelative(lastSignalTs)} />
      </div>

      <div className="mt-4 flex items-center gap-3">
        <div className="mono-label w-24" style={{ fontSize: 10 }}>Daily loss</div>
        <div className="flex-1 h-2 rounded-full bg-elevated overflow-hidden">
          <div
            className={`h-full transition-all ${progressPct >= 80 ? 'bg-negative' : 'bg-warning'}`}
            style={{ width: `${progressPct}%` }}
          />
        </div>
        <div className="text-xs text-fg-dim w-44 text-right font-mono">
          limit: {fmtMoney(-dailyLossLimit)} ({fmtPct(-dailyLossKillPct)})
        </div>
      </div>
    </div>
  )
}

function Cell({ label, value, tone = 'text-fg' }) {
  return (
    <div>
      <div className="mono-label" style={{ fontSize: 10 }}>{label}</div>
      <div className={`text-sm font-medium mt-0.5 ${tone}`}>{value}</div>
    </div>
  )
}
