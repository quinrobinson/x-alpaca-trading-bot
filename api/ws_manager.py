"""WSManager — fans out events to connected dashboard clients (Phase 8).

The orchestrator runs in a background thread and calls
`WSManager.dispatch_threadsafe(event, payload)` whenever something
interesting happens. dispatch_threadsafe schedules an async coroutine onto
the FastAPI event loop, which then sends the JSON message to every
connected WebSocket client. A slow / disconnected client is dropped from
the set rather than allowed to block other clients.

Event schema:

    {
      "event": "trade.entered",   # one of the names below
      "payload": { ... },         # arbitrary JSON-safe dict
      "ts": "2026-05-13T18:30:00+00:00"
    }

Defined event names (matching x-alpaca-trading-bot-architecture.md §"WebSocket Events"):
    signal.received, signal.validated, trade.entered, trade.updated,
    trade.stop_moved, trade.exited, killswitch.tripped, market.status,
    system.heartbeat
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)

EVENT_NAMES: frozenset[str] = frozenset({
    "signal.received", "signal.validated",
    "trade.entered", "trade.updated", "trade.stop_moved", "trade.exited",
    "killswitch.tripped", "market.status", "system.heartbeat",
})


class WSManager:
    """Tracks connected dashboard clients and broadcasts events to all."""

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    # ---- Lifecycle wiring (called from FastAPI lifespan) -------------

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Remember the FastAPI event loop so background threads can schedule
        coroutines onto it via asyncio.run_coroutine_threadsafe."""
        self._loop = loop

    @property
    def client_count(self) -> int:
        return len(self._clients)

    # ---- Client lifecycle ---------------------------------------------

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._clients.add(websocket)
        logger.info("ws client connected; total=%d", len(self._clients))

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(websocket)
        logger.info("ws client disconnected; total=%d", len(self._clients))

    # ---- Broadcast ----------------------------------------------------

    async def broadcast(self, event: str, payload: dict[str, Any]) -> int:
        """Send the event to every connected client. Drops dead clients.

        Returns the number of clients the message was delivered to.
        """
        msg = {
            "event": event,
            "payload": _jsonable(payload),
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        delivered = 0
        dead: list[WebSocket] = []

        # Snapshot the client set under the lock; release before sending
        # so a slow socket doesn't hold up other broadcasts.
        async with self._lock:
            clients = list(self._clients)

        for ws in clients:
            try:
                await ws.send_json(msg)
                delivered += 1
            except Exception as exc:  # noqa: BLE001
                logger.info("ws client dropped on broadcast: %s", exc)
                dead.append(ws)

        if dead:
            async with self._lock:
                for ws in dead:
                    self._clients.discard(ws)

        return delivered

    def dispatch_threadsafe(self, event: str, payload: dict[str, Any]) -> None:
        """Schedule a broadcast from a sync thread (the orchestrator).

        If the FastAPI loop isn't running yet (boot, or tests without
        a server), the event is dropped with a log line — no exception
        leaks back into the orchestrator.
        """
        if self._loop is None or not self._loop.is_running():
            logger.debug("ws loop unavailable; dropping event %s", event)
            return
        try:
            asyncio.run_coroutine_threadsafe(self.broadcast(event, payload), self._loop)
        except RuntimeError as exc:
            logger.warning("ws dispatch failed: %s", exc)


# ---- JSON safety -----------------------------------------------------------

def _jsonable(value: Any) -> Any:
    """Coerce Decimals/datetimes to JSON-friendly primitives recursively."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value
