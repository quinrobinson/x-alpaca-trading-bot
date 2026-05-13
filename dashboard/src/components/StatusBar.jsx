import { fmtMoney, fmtPct, fmtRelative, pnlColorClass } from '../util'

/**
 * Panel 1 — System Status Bar (top).
 *
 * Shows bot status, market status, X stream status, Alpaca connection,
 * today's P&L, daily-loss-limit progress bar, last signal received.
 * Per architecture brief §"Dashboard Layout" panel 1.
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

  const switchesOn = killSwitches.length > 0
  const botStatus = switchesOn ? 'paused (kill switch)' : 'running'
  const botColor = switchesOn ? 'text-rose-400' : 'text-emerald-400'

  return (
    <div className="bg-slate-900 border-b border-slate-800 px-4 py-3">
      <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-7 gap-4 items-center">
        <div>
          <div className="text-[10px] uppercase text-slate-500">Bot</div>
          <div className={`text-sm font-medium ${botColor}`}>{botStatus}</div>
        </div>
        <div>
          <div className="text-[10px] uppercase text-slate-500">Market</div>
          <div className="text-sm font-medium">
            {health?.market_open ? 'open' : 'closed'}
          </div>
        </div>
        <div>
          <div className="text-[10px] uppercase text-slate-500">X stream</div>
          <div className={`text-sm font-medium ${killSwitches.includes('x_stream_disconnected') ? 'text-rose-400' : 'text-emerald-400'}`}>
            {killSwitches.includes('x_stream_disconnected') ? 'down' : 'connected'}
          </div>
        </div>
        <div>
          <div className="text-[10px] uppercase text-slate-500">Alpaca</div>
          <div className={`text-sm font-medium ${killSwitches.includes('alpaca_disconnected') ? 'text-rose-400' : 'text-emerald-400'}`}>
            {killSwitches.includes('alpaca_disconnected') ? 'down' : 'connected'}
          </div>
        </div>
        <div>
          <div className="text-[10px] uppercase text-slate-500">WS</div>
          <div className={`text-sm font-medium ${wsStatus === 'open' ? 'text-emerald-400' : 'text-amber-400'}`}>
            {wsStatus}
          </div>
        </div>
        <div>
          <div className="text-[10px] uppercase text-slate-500">P&amp;L today</div>
          <div className={`text-sm font-medium ${pnlColorClass(realizedPnl)}`}>
            {fmtMoney(realizedPnl)} ({fmtPct(realizedPnl / startingEquity)})
          </div>
        </div>
        <div>
          <div className="text-[10px] uppercase text-slate-500">Last signal</div>
          <div className="text-sm font-medium">{fmtRelative(lastSignalTs)}</div>
        </div>
      </div>

      {/* Daily-loss-limit progress: empty when in profit; fills toward the kill threshold as loss grows. */}
      <div className="mt-3 flex items-center gap-3">
        <div className="text-[10px] uppercase text-slate-500 w-24">Daily loss</div>
        <div className="flex-1 h-2 rounded bg-slate-800 overflow-hidden">
          <div
            className={`h-full ${progressPct >= 80 ? 'bg-rose-500' : 'bg-amber-400'} transition-all`}
            style={{ width: `${progressPct}%` }}
          />
        </div>
        <div className="text-xs text-slate-400 w-32 text-right">
          limit: {fmtMoney(-dailyLossLimit)} ({(dailyLossKillPct * 100).toFixed(1)}%)
        </div>
      </div>
    </div>
  )
}
