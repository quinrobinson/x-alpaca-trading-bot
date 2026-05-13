import { useState } from 'react'

/** Single-line collapsible "drawer" used for Market context, etc. */
export default function CollapsibleSection({ title, children, defaultOpen = false }) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <section className="bg-slate-900 border border-slate-800 rounded-lg">
      <button
        onClick={() => setOpen((o) => !o)}
        className="w-full flex items-center justify-between px-4 py-3 text-left"
      >
        <span className="text-xs uppercase text-slate-400">{title}</span>
        <svg className={`w-3 h-3 text-slate-400 transition-transform ${open ? 'rotate-180' : ''}`}
             viewBox="0 0 12 12">
          <path d="M3 4.5l3 3 3-3" stroke="currentColor" strokeWidth="1.5" fill="none" />
        </svg>
      </button>
      {open && <div className="px-4 pb-4 border-t border-slate-800 pt-3">{children}</div>}
    </section>
  )
}
