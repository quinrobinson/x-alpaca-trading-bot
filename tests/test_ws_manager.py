"""Unit tests for the WSManager.

Uses a hand-rolled FakeWebSocket so we exercise the manager's logic
without going through FastAPI's TestClient.
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import pytest

from api.ws_manager import WSManager, _jsonable, EVENT_NAMES


class FakeWebSocket:
    """Implements just enough of fastapi.WebSocket for WSManager."""

    def __init__(self, *, fail_after: int | None = None) -> None:
        self.accepted = False
        self.sent: list[dict[str, Any]] = []
        self._fail_after = fail_after  # raise on the Nth send, if set

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, payload: dict[str, Any]) -> None:
        if self._fail_after is not None and len(self.sent) >= self._fail_after:
            raise RuntimeError("simulated client disconnect")
        self.sent.append(payload)


# ---- connect / disconnect ------------------------------------------------

@pytest.mark.asyncio
async def test_connect_accepts_and_tracks() -> None:
    mgr = WSManager()
    ws = FakeWebSocket()
    await mgr.connect(ws)
    assert ws.accepted is True
    assert mgr.client_count == 1


@pytest.mark.asyncio
async def test_disconnect_removes() -> None:
    mgr = WSManager()
    ws = FakeWebSocket()
    await mgr.connect(ws)
    await mgr.disconnect(ws)
    assert mgr.client_count == 0


@pytest.mark.asyncio
async def test_disconnect_unknown_ws_is_noop() -> None:
    mgr = WSManager()
    await mgr.disconnect(FakeWebSocket())  # no exception


# ---- broadcast -----------------------------------------------------------

@pytest.mark.asyncio
async def test_broadcast_delivers_to_all_clients() -> None:
    mgr = WSManager()
    clients = [FakeWebSocket() for _ in range(3)]
    for c in clients:
        await mgr.connect(c)

    delivered = await mgr.broadcast("trade.entered", {"signal_id": 1})
    assert delivered == 3
    for c in clients:
        assert len(c.sent) == 1
        msg = c.sent[0]
        assert msg["event"] == "trade.entered"
        assert msg["payload"] == {"signal_id": 1}
        assert "ts" in msg


@pytest.mark.asyncio
async def test_broadcast_drops_dead_client_keeps_others() -> None:
    """If one client raises on send, the others still receive."""
    mgr = WSManager()
    good_a = FakeWebSocket()
    dead = FakeWebSocket(fail_after=0)  # raises on first send
    good_b = FakeWebSocket()
    for c in (good_a, dead, good_b):
        await mgr.connect(c)

    delivered = await mgr.broadcast("signal.received", {"foo": "bar"})
    # good_a + good_b succeeded, dead failed
    assert delivered == 2
    assert mgr.client_count == 2  # dead was discarded
    assert len(good_a.sent) == 1
    assert len(good_b.sent) == 1
    assert len(dead.sent) == 0


@pytest.mark.asyncio
async def test_broadcast_serializes_decimals_and_datetimes() -> None:
    mgr = WSManager()
    ws = FakeWebSocket()
    await mgr.connect(ws)
    ts = datetime(2026, 5, 13, 13, 30, tzinfo=timezone.utc)
    await mgr.broadcast("trade.updated", {
        "fill_price": Decimal("2.55"),
        "filled_at": ts,
        "nested": {"strike": Decimal("185")},
        "list": [Decimal("1.0"), Decimal("2.0")],
    })
    payload = ws.sent[0]["payload"]
    assert payload["fill_price"] == "2.55"
    assert payload["filled_at"] == "2026-05-13T13:30:00+00:00"
    assert payload["nested"]["strike"] == "185"
    assert payload["list"] == ["1.0", "2.0"]


@pytest.mark.asyncio
async def test_broadcast_with_no_clients_returns_zero() -> None:
    mgr = WSManager()
    assert (await mgr.broadcast("system.heartbeat", {})) == 0


# ---- dispatch_threadsafe -------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_threadsafe_schedules_on_attached_loop() -> None:
    """Calling from another thread should deliver to clients."""
    mgr = WSManager()
    ws = FakeWebSocket()
    await mgr.connect(ws)

    loop = asyncio.get_running_loop()
    mgr.attach_loop(loop)

    done = threading.Event()

    def worker() -> None:
        mgr.dispatch_threadsafe("trade.exited", {"signal_id": 7})
        done.set()

    threading.Thread(target=worker, daemon=True).start()
    done.wait(timeout=2)
    # Let the scheduled coroutine run.
    await asyncio.sleep(0.05)
    assert any(m["event"] == "trade.exited" for m in ws.sent)


def test_dispatch_threadsafe_drops_when_no_loop_attached() -> None:
    """No exception leaks back into the orchestrator if FastAPI isn't up."""
    mgr = WSManager()
    mgr.dispatch_threadsafe("trade.entered", {"signal_id": 1})  # no raise


# ---- Event-name catalogue ------------------------------------------------

def test_event_names_match_spec() -> None:
    assert EVENT_NAMES == {
        "signal.received", "signal.validated",
        "trade.entered", "trade.updated", "trade.stop_moved", "trade.exited",
        "killswitch.tripped", "market.status", "system.heartbeat",
    }


# ---- _jsonable helper ----------------------------------------------------

def test_jsonable_primitive_passthrough() -> None:
    assert _jsonable(1) == 1
    assert _jsonable("x") == "x"
    assert _jsonable(True) is True
    assert _jsonable(None) is None
    assert _jsonable(2.5) == 2.5
