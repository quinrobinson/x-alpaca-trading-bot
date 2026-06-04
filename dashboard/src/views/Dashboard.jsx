import OpenPositionCard from '../components/v2/OpenPositionCard.jsx'
import CollapsibleSection from '../components/v2/CollapsibleSection.jsx'
import MarketContext from '../components/MarketContext.jsx'
import { useAppData } from '../AppShell.jsx'

/**
 * Dashboard — the "right now" view.
 * Open positions and live market context. Stats live on Performance,
 * the signal feed lives on Timeline; this tab stays focused on what's
 * actionable in the next 5 minutes.
 */
export default function Dashboard() {
  const { positions, marketCtx } = useAppData()

  return (
    <div className="space-y-4">
      {positions.length === 0 ? (
        <section className="card p-6 text-center text-sm text-fg-dim">
          No open positions.
        </section>
      ) : (
        positions.map(p => (
          <OpenPositionCard
            key={p.signal_id}
            position={p}
            livePrice={p.live_mid != null ? Number(p.live_mid) : undefined}
            snapshot={p.snapshot}
          />
        ))
      )}

      <CollapsibleSection title="Market context">
        <MarketContext
          snapshot={marketCtx}
          latestSectorString={marketCtx?.sector_etf_trend}
        />
      </CollapsibleSection>
    </div>
  )
}
