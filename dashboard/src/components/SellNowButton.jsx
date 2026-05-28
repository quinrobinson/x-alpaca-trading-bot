import { useState } from 'react'
import { apiUrl } from '../config.js'

/**
 * Sell now button — manual force-close for an open position.
 *
 * States:
 *   idle    → small amber pill, "Sell now"
 *   confirm → "Sell at market?"  [Confirm] [Cancel]
 *   sending → spinner, "Submitting…", disabled
 *   closing → "Closing…" pill, disabled (driven by closing_in_progress from /positions)
 *   error   → red error line under the row; resets on next tap
 *
 * The closing state can also arrive externally: once the orchestrator
 * accepts the close, /positions returns closing_in_progress=true. We
 * honor that prop so the card stays "Closing…" until the row disappears
 * from /positions (when the fill is booked).
 */
export default function SellNowButton({ signalId, closingInProgress = false, ticker }) {
  const [stage, setStage] = useState('idle')   // 'idle' | 'confirm' | 'sending'
  const [error, setError] = useState(null)

  // Server-side flag wins — once it's true, this button is "Closing…".
  const isClosing = closingInProgress || stage === 'sending'

  const submit = async () => {
    setStage('sending')
    setError(null)
    try {
      const r = await fetch(apiUrl(`/positions/${signalId}/close`), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
      })
      if (!r.ok && r.status !== 202) {
        const body = await r.json().catch(() => ({}))
        throw new Error(body.reason || `HTTP ${r.status}`)
      }
      // Success: stay in 'sending' visually. The next /positions poll
      // (or WS event) will flip closingInProgress=true, then the row
      // disappears when the fill is booked. Don't reset stage here.
    } catch (err) {
      setError(err.message || 'Failed')
      setStage('idle')
    }
  }

  if (isClosing) {
    return (
      <div className="flex items-center justify-end gap-1.5 text-xs">
        <span
          className="inline-block w-3 h-3 rounded-full border-2 border-current border-t-transparent animate-spin"
          style={{ color: 'var(--accent-amber, #f59e0b)' }}
        />
        <span
          className="font-mono uppercase tracking-wider"
          style={{ fontSize: 10, letterSpacing: '0.16em', color: 'var(--accent-amber, #f59e0b)' }}
        >
          Closing{ticker ? ` ${ticker}` : ''}…
        </span>
      </div>
    )
  }

  if (stage === 'confirm') {
    return (
      <div className="flex items-center justify-end gap-2 text-xs">
        <span className="text-fg-muted">Sell at market?</span>
        <button
          onClick={submit}
          className="px-2.5 py-1 rounded font-mono uppercase tracking-wider transition-colors"
          style={{
            fontSize: 10,
            letterSpacing: '0.16em',
            background: 'var(--accent-amber, #f59e0b)',
            color: '#0F0F0F',
          }}
        >
          Confirm
        </button>
        <button
          onClick={() => { setStage('idle'); setError(null) }}
          className="px-2.5 py-1 rounded font-mono uppercase tracking-wider text-fg-dim hover:text-fg transition-colors"
          style={{ fontSize: 10, letterSpacing: '0.16em' }}
        >
          Cancel
        </button>
      </div>
    )
  }

  return (
    <div className="flex flex-col items-end gap-1">
      <button
        onClick={() => setStage('confirm')}
        className="px-2.5 py-1 rounded font-mono uppercase tracking-wider transition-colors hover:opacity-80"
        style={{
          fontSize: 10,
          letterSpacing: '0.16em',
          border: '1px solid var(--accent-amber, #f59e0b)',
          color: 'var(--accent-amber, #f59e0b)',
          background: 'transparent',
        }}
      >
        Sell now
      </button>
      {error && (
        <span className="text-xs text-negative" style={{ fontSize: 11 }}>
          {error}
        </span>
      )}
    </div>
  )
}
