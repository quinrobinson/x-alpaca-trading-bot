import { useState } from 'react'
import TimelineFeed from '../components/v2/Timeline.jsx'
import { useAppData } from '../AppShell.jsx'

/**
 * Timeline — the "what happened" feed.
 * Tweets, signals, trades, skipped signals — everything the bot did
 * (or chose not to do) in reverse chronological order. The show-
 * rejected toggle hangs off the feed header; AppShell owns the data.
 */
export default function Timeline() {
  const { timeline } = useAppData()
  const [showRejected, setShowRejected] = useState(false)

  return (
    <TimelineFeed
      items={timeline}
      showRejected={showRejected}
      onToggleRejected={setShowRejected}
    />
  )
}
