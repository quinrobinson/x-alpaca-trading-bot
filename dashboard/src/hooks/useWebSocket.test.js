import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'
import { useWebSocket } from './useWebSocket.js'

/**
 * Hand-rolled MockWebSocket that mirrors enough of the browser API to drive
 * the hook. Instances are tracked so tests can grab the latest one and
 * trigger lifecycle events.
 */
class MockWebSocket {
  static OPEN = 1
  static CLOSED = 3
  static instances = []

  constructor(url) {
    this.url = url
    this.readyState = 0
    this.onopen = null
    this.onmessage = null
    this.onerror = null
    this.onclose = null
    this.sent = []
    MockWebSocket.instances.push(this)
  }

  send(data) { this.sent.push(data) }
  close() {
    this.readyState = MockWebSocket.CLOSED
    this.onclose?.()
  }
  // Test-only helpers
  _open() {
    this.readyState = MockWebSocket.OPEN
    this.onopen?.()
  }
  _message(data) {
    this.onmessage?.({ data: typeof data === 'string' ? data : JSON.stringify(data) })
  }
  _close() {
    this.readyState = MockWebSocket.CLOSED
    this.onclose?.()
  }
}

describe('useWebSocket', () => {
  beforeEach(() => {
    MockWebSocket.instances = []
    vi.stubGlobal('WebSocket', MockWebSocket)
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('starts in connecting state', () => {
    const { result } = renderHook(() => useWebSocket('ws://test/ws'))
    expect(result.current.status).toBe('connecting')
  })

  it('transitions to open when the socket opens', async () => {
    const { result } = renderHook(() => useWebSocket('ws://test/ws'))
    await act(async () => {
      MockWebSocket.instances[0]._open()
    })
    expect(result.current.status).toBe('open')
  })

  it('parses incoming JSON messages into lastEvent and onEvent callback', async () => {
    const onEvent = vi.fn()
    const { result } = renderHook(() => useWebSocket('ws://test/ws', { onEvent }))
    await act(async () => {
      MockWebSocket.instances[0]._open()
      MockWebSocket.instances[0]._message({
        event: 'trade.entered',
        payload: { signal_id: 1 },
        ts: '2026-05-13T13:30:00+00:00',
      })
    })
    expect(result.current.lastEvent?.event).toBe('trade.entered')
    expect(onEvent).toHaveBeenCalledWith(
      expect.objectContaining({ event: 'trade.entered' }),
    )
  })

  it('does not crash on malformed JSON', async () => {
    const consoleWarn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const { result } = renderHook(() => useWebSocket('ws://test/ws'))
    await act(async () => {
      MockWebSocket.instances[0]._open()
      MockWebSocket.instances[0]._message('not json')
    })
    expect(result.current.lastEvent).toBeNull()
    consoleWarn.mockRestore()
  })

  it('reconnects with backoff after the socket closes', async () => {
    renderHook(() => useWebSocket('ws://test/ws'))
    expect(MockWebSocket.instances.length).toBe(1)

    // First connection opens then closes — first reconnect at 500ms.
    await act(async () => {
      MockWebSocket.instances[0]._open()
      MockWebSocket.instances[0]._close()
    })
    expect(MockWebSocket.instances.length).toBe(1)
    await act(async () => { vi.advanceTimersByTime(500) })
    expect(MockWebSocket.instances.length).toBe(2)

    // Second close — next attempt at 1000ms (backoff doubles).
    await act(async () => { MockWebSocket.instances[1]._close() })
    await act(async () => { vi.advanceTimersByTime(999) })
    expect(MockWebSocket.instances.length).toBe(2)
    await act(async () => { vi.advanceTimersByTime(1) })
    expect(MockWebSocket.instances.length).toBe(3)
  })

  it('resets backoff after a successful open', async () => {
    renderHook(() => useWebSocket('ws://test/ws'))
    // First close → reconnect at 500ms (set backoff to 1000ms next)
    await act(async () => {
      MockWebSocket.instances[0]._open()
      MockWebSocket.instances[0]._close()
    })
    await act(async () => { vi.advanceTimersByTime(500) })
    // Second socket opens → backoff resets to 500ms
    await act(async () => { MockWebSocket.instances[1]._open() })
    // Second close should reconnect at 500ms again, not 1000ms
    await act(async () => { MockWebSocket.instances[1]._close() })
    await act(async () => { vi.advanceTimersByTime(500) })
    expect(MockWebSocket.instances.length).toBe(3)
  })

  it('does not reconnect after unmount', async () => {
    const { unmount } = renderHook(() => useWebSocket('ws://test/ws'))
    await act(async () => {
      MockWebSocket.instances[0]._open()
    })
    unmount()
    // Subsequent close shouldn't trigger a reconnect.
    await act(async () => { vi.advanceTimersByTime(10_000) })
    expect(MockWebSocket.instances.length).toBe(1)
  })

  it('send() forwards data when the socket is open', async () => {
    const { result } = renderHook(() => useWebSocket('ws://test/ws'))
    await act(async () => { MockWebSocket.instances[0]._open() })
    act(() => result.current.send('hello'))
    expect(MockWebSocket.instances[0].sent).toEqual(['hello'])
    act(() => result.current.send({ ping: 1 }))
    expect(MockWebSocket.instances[0].sent[1]).toBe('{"ping":1}')
  })
})
