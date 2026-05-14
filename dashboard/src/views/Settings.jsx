import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import BrandMark from '../components/v2/BrandMark.jsx'
import ToggleSwitch from '../components/v2/ToggleSwitch.jsx'
import { apiUrl } from '../config.js'

/**
 * /settings — operator-facing runtime config.
 *
 *   - max_position_spend_usd     dollar cap per entry (derives contract qty)
 *   - max_qty_per_position       ceiling on contracts so a cheap option
 *                                doesn't blow up size
 *   - daily_loss_kill_pct        kill-switch threshold as a fraction
 *                                (e.g. 0.03 = 3%)
 *   - disable_x_stream           live pause/resume of signal entries
 *
 * Reads from GET /config, persists via PATCH /config. The bot reads the
 * same row at the top of every signal so changes apply on the next event
 * with no restart.
 */
export default function Settings() {
  const [loaded, setLoaded] = useState(false)
  const [loadError, setLoadError] = useState(null)
  const [saveError, setSaveError] = useState(null)
  const [saving, setSaving] = useState(false)
  const [savedAt, setSavedAt] = useState(null)

  const [spend, setSpend] = useState('')          // string while editing
  const [maxQty, setMaxQty] = useState('')
  const [killPct, setKillPct] = useState('')      // entered as %, stored as fraction
  const [disableXStream, setDisableXStream] = useState(false)

  const load = useCallback(async () => {
    setLoadError(null)
    try {
      const r = await fetch(apiUrl('/config'))
      if (!r.ok) throw new Error(`GET /config returned ${r.status}`)
      const body = await r.json()
      setSpend(String(body.max_position_spend_usd))
      setMaxQty(String(body.max_qty_per_position))
      setKillPct((Number(body.daily_loss_kill_pct) * 100).toFixed(2))
      setDisableXStream(Boolean(body.disable_x_stream))
      setLoaded(true)
    } catch (err) {
      setLoadError(err.message ?? String(err))
    }
  }, [])

  useEffect(() => { load() }, [load])

  const onSubmit = async (e) => {
    e.preventDefault()
    setSaving(true)
    setSaveError(null)
    setSavedAt(null)
    try {
      const payload = {
        max_position_spend_usd: spend,
        max_qty_per_position: Number(maxQty),
        daily_loss_kill_pct: (Number(killPct) / 100).toFixed(4),
        disable_x_stream: disableXStream,
      }
      const r = await fetch(apiUrl('/config'), {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
      if (!r.ok) {
        const body = await r.json().catch(() => ({}))
        throw new Error(body.detail || `PATCH /config returned ${r.status}`)
      }
      const body = await r.json()
      setSpend(String(body.max_position_spend_usd))
      setMaxQty(String(body.max_qty_per_position))
      setKillPct((Number(body.daily_loss_kill_pct) * 100).toFixed(2))
      setDisableXStream(Boolean(body.disable_x_stream))
      setSavedAt(new Date())
    } catch (err) {
      setSaveError(err.message ?? String(err))
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="min-h-screen flex flex-col max-w-3xl mx-auto">
      <header className="sticky top-0 z-10 bg-bg/90 backdrop-blur border-b border-border">
        <div className="px-4 lg:px-6 pt-4 pb-2 flex items-center gap-2.5">
          <BrandMark size={28} />
          <span
            className="font-display font-semibold text-fg tracking-tight truncate"
            style={{ fontSize: 15, letterSpacing: '-0.01em' }}
          >
            x-alpaca-trading-bot
          </span>
          <span
            className="mono-label ml-auto shrink-0"
            style={{ fontSize: 10, letterSpacing: '0.18em' }}
          >
            settings
          </span>
        </div>
        <div className="px-4 lg:px-6 pb-3.5">
          <Link
            to="/"
            className="text-xs text-fg-dim hover:text-fg transition-colors font-mono uppercase tracking-wider"
            style={{ fontSize: 10, letterSpacing: '0.16em' }}
          >
            ← back to dashboard
          </Link>
        </div>
      </header>

      <main className="flex-1 px-4 lg:px-6 py-6">
        {loadError && (
          <div
            className="rounded-card p-4 mb-4 text-sm text-negative"
            style={{ background: 'var(--card)', border: '1px solid rgba(239,68,68,0.45)' }}
          >
            Failed to load settings — {loadError}
          </div>
        )}

        {!loaded && !loadError && (
          <div className="card p-6 text-center text-sm text-fg-dim">Loading…</div>
        )}

        {loaded && (
          <form onSubmit={onSubmit} className="space-y-4">
            <Field
              label="Max position spend"
              hint="Dollar cap per entry. Contract qty is derived from this and the live ask. Allowed: $1–$100,000."
              suffix="USD"
              value={spend}
              onChange={setSpend}
              inputMode="decimal"
              min="1"
              max="100000"
              step="0.01"
            />
            <Field
              label="Max qty per position"
              hint="Ceiling on contracts so a cheap option doesn't blow up size. Allowed: 1–100."
              value={maxQty}
              onChange={setMaxQty}
              type="number"
              min="1"
              max="100"
              step="1"
            />
            <Field
              label="Daily loss kill threshold"
              hint="Stops new entries when realized + unrealized P&L drops by this fraction of starting equity. Allowed: 0.1%–50%."
              suffix="%"
              value={killPct}
              onChange={setKillPct}
              inputMode="decimal"
              min="0.1"
              max="50"
              step="0.1"
            />

            <ToggleField
              label="Pause new entries"
              hint="When on, incoming X posts are dropped before parsing. The X stream stays connected (no restart needed), and the x_stream_disconnected kill switch is suppressed."
              checked={disableXStream}
              onChange={setDisableXStream}
            />

            <div className="flex items-center gap-3 pt-2">
              <button
                type="submit"
                disabled={saving}
                className="rounded-md px-4 py-2 font-mono uppercase tracking-wider transition-opacity disabled:opacity-50"
                style={{
                  background: 'var(--brand-orange)',
                  color: '#0F0F0F',
                  fontSize: 11,
                  letterSpacing: '0.16em',
                  fontWeight: 600,
                }}
              >
                {saving ? 'Saving…' : 'Save changes'}
              </button>
              {savedAt && !saveError && (
                <span className="text-xs text-positive font-mono">
                  saved {savedAt.toLocaleTimeString([], { hour12: false })}
                </span>
              )}
              {saveError && (
                <span className="text-xs text-negative">{saveError}</span>
              )}
            </div>
          </form>
        )}
      </main>
    </div>
  )
}


function Field({ label, hint, suffix, ...inputProps }) {
  return (
    <label className="card p-4 block cursor-text">
      <div className="flex items-baseline justify-between gap-3 mb-2">
        <span className="text-sm font-display font-semibold text-fg">{label}</span>
      </div>
      <div className="flex items-center gap-2">
        <input
          {...inputProps}
          onChange={(e) => inputProps.onChange(e.target.value)}
          className="flex-1 bg-elevated border border-border rounded-md px-3 py-2 text-fg font-mono text-sm outline-none focus:border-border-hover transition-colors"
        />
        {suffix && (
          <span
            className="font-mono text-fg-dim uppercase tracking-wider"
            style={{ fontSize: 11, letterSpacing: '0.14em' }}
          >
            {suffix}
          </span>
        )}
      </div>
      <p className="text-xs text-fg-dim mt-2 leading-relaxed">{hint}</p>
    </label>
  )
}


function ToggleField({ label, hint, checked, onChange }) {
  return (
    <div className="card p-4">
      <div className="flex items-baseline justify-between gap-3 mb-2">
        <span className="text-sm font-display font-semibold text-fg">{label}</span>
        <ToggleSwitch checked={checked} onChange={onChange} label="" />
      </div>
      <p className="text-xs text-fg-dim leading-relaxed">{hint}</p>
    </div>
  )
}
