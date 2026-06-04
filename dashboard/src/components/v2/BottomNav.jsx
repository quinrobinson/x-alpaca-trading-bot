import { NavLink } from 'react-router-dom'

/**
 * Fixed bottom tab bar — APDF dark.
 *   [ ☐ Dashboard ]  [ ☰ Timeline ]  [ ⟋ Performance ]  [ ⚙ Settings ]
 *
 * Stays anchored to the bottom of the viewport. Uses safe-area-inset-
 * bottom for the iOS home indicator on standalone PWAs. Active tab
 * shows the BrandMark orange; inactive uses fg-dim and lifts to fg
 * on hover/press.
 */
export default function BottomNav() {
  return (
    <nav
      className="fixed bottom-0 inset-x-0 z-20 border-t border-border"
      style={{
        background: 'var(--bg)',
        paddingBottom: 'env(safe-area-inset-bottom)',
      }}
      aria-label="Primary"
    >
      <div className="max-w-3xl lg:max-w-6xl mx-auto px-2 flex">
        <Tab to="/" label="Dashboard" icon={<DashboardIcon />} end />
        <Tab to="/timeline" label="Timeline" icon={<TimelineIcon />} />
        <Tab to="/performance" label="Performance" icon={<ChartIcon />} />
        <Tab to="/settings" label="Settings" icon={<GearIcon />} />
      </div>
    </nav>
  )
}

function Tab({ to, label, icon, end = false }) {
  return (
    <NavLink
      to={to}
      end={end}
      className={({ isActive }) =>
        [
          'flex-1 flex flex-col items-center justify-center gap-1 py-2.5 transition-colors',
          isActive ? 'text-fg' : 'text-fg-dim hover:text-fg',
        ].join(' ')
      }
      style={({ isActive }) => ({
        color: isActive ? 'var(--accent-amber, #f59e0b)' : undefined,
      })}
    >
      <span aria-hidden="true">{icon}</span>
      <span
        className="font-mono uppercase tracking-wider"
        style={{ fontSize: 9, letterSpacing: '0.16em' }}
      >
        {label}
      </span>
    </NavLink>
  )
}

// --- Icons (inline SVG, no extra dependency) -------------------------------

function DashboardIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round">
      <rect x="3" y="3" width="8" height="11" rx="1.5" />
      <rect x="13" y="3" width="8" height="6" rx="1.5" />
      <rect x="13" y="11" width="8" height="10" rx="1.5" />
      <rect x="3" y="16" width="8" height="5" rx="1.5" />
    </svg>
  )
}

function TimelineIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="6" cy="6" r="1.5" />
      <circle cx="6" cy="12" r="1.5" />
      <circle cx="6" cy="18" r="1.5" />
      <line x1="10" y1="6" x2="20" y2="6" />
      <line x1="10" y1="12" x2="20" y2="12" />
      <line x1="10" y1="18" x2="20" y2="18" />
    </svg>
  )
}

function ChartIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="3 17 8 12 12 16 21 6" />
      <polyline points="15 6 21 6 21 12" />
    </svg>
  )
}

function GearIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
    </svg>
  )
}
