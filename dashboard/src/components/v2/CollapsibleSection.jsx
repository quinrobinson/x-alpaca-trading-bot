import { useState } from 'react'

/** APDF-styled collapsible drawer. */
export default function CollapsibleSection({ title, children, defaultOpen = false }) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <section className="card">
      <button
        onClick={() => setOpen((o) => !o)}
        className="w-full flex items-center justify-between px-5 py-4 text-left"
      >
        <span className="mono-label" style={{ fontSize: 11 }}>{title}</span>
        <svg
          className={`w-3 h-3 text-fg-dim transition-transform ${open ? 'rotate-180' : ''}`}
          viewBox="0 0 12 12"
        >
          <path d="M3 4.5l3 3 3-3" stroke="currentColor" strokeWidth="1.5" fill="none" />
        </svg>
      </button>
      {open && (
        <div className="px-5 pb-5 border-t border-border pt-4">{children}</div>
      )}
    </section>
  )
}
