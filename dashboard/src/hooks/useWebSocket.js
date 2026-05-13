import { useEffect, useRef, useState } from 'react'

/**
 * useWebSocket — connect to a WS URL with exponential-backoff reconnect.
 *
 * Returns { status, lastEvent, send }. Pass `onEvent(event)` to get
 * per-message callbacks (event = { event: string, payload: any, ts: string }).
 *
 * Backoff: 500ms → 1s → 2s → 4s → 8s (capped), reset on successful connect.
 *
 * The hook never throws — connection failures are surfaced via the `status`
 * state ('connecting' | 'open' | 'closed').
 */
export function useWebSocket(url, { onEvent } = {}) {
  const [status, setStatus] = useState('connecting')
  const [lastEvent, setLastEvent] = useState(null)
  const wsRef = useRef(null)
  const reconnectTimer = useRef(null)
  const backoffMs = useRef(500)
  const onEventRef = useRef(onEvent)
  const closedByUserRef = useRef(false)

  // Keep the latest onEvent without re-running the connect effect.
  useEffect(() => { onEventRef.current = onEvent }, [onEvent])

  useEffect(() => {
    closedByUserRef.current = false

    const connect = () => {
      if (closedByUserRef.current) return
      setStatus('connecting')
      const ws = new WebSocket(url)
      wsRef.current = ws

      ws.onopen = () => {
        setStatus('open')
        backoffMs.current = 500 // reset on successful connect
      }
      ws.onmessage = (msg) => {
        try {
          const parsed = JSON.parse(msg.data)
          setLastEvent(parsed)
          onEventRef.current?.(parsed)
        } catch (err) {
          // Malformed JSON; ignore so a bad server message can't crash the UI
          // eslint-disable-next-line no-console
          console.warn('useWebSocket: bad JSON', err)
        }
      }
      ws.onerror = () => { /* error → onclose will fire next */ }
      ws.onclose = () => {
        setStatus('closed')
        if (closedByUserRef.current) return
        const delay = Math.min(backoffMs.current, 8000)
        backoffMs.current = Math.min(backoffMs.current * 2, 8000)
        reconnectTimer.current = setTimeout(connect, delay)
      }
    }

    connect()

    return () => {
      closedByUserRef.current = true
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current)
      if (wsRef.current && wsRef.current.readyState <= 1) {
        wsRef.current.close()
      }
    }
  }, [url])

  const send = (data) => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(typeof data === 'string' ? data : JSON.stringify(data))
    }
  }

  return { status, lastEvent, send }
}
