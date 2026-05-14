/**
 * Pill-style toggle. Off: dim track, knob on left. On: brand-purple
 * track with a soft glow, knob on right.
 *
 * Used in the Timeline header ("Show skipped") and on the Settings page.
 */
export default function ToggleSwitch({ checked, onChange, label }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      onClick={() => onChange(!checked)}
      className="flex items-center gap-2 text-xs text-fg-muted hover:text-fg transition-colors select-none cursor-pointer"
    >
      {label && <span>{label}</span>}
      <span
        className="relative inline-block transition-colors"
        style={{
          width: 30,
          height: 18,
          borderRadius: 999,
          background: checked ? 'var(--brand-purple)' : 'var(--elevated)',
          border: `1px solid ${checked ? 'rgba(134,59,255,0.55)' : 'var(--border)'}`,
          boxShadow: checked ? '0 0 12px rgba(134,59,255,0.30)' : 'none',
        }}
      >
        <span
          className="absolute top-1/2 transition-all"
          style={{
            width: 12,
            height: 12,
            borderRadius: 999,
            background: checked ? '#FFFFFF' : 'var(--fg-dim)',
            left: checked ? 14 : 2,
            transform: 'translateY(-50%)',
            boxShadow: '0 1px 2px rgba(0,0,0,0.5)',
          }}
        />
      </span>
    </button>
  )
}
