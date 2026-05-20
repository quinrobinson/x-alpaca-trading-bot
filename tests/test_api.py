"""Integration tests for the FastAPI app (Phase 8).

Covers:
  - REST endpoints (/healthz, /positions, /signals, /performance)
  - WebSocket endpoint (/ws) — connect, receive broadcast, multi-client,
    reconnect, ping/echo

Skips DB-touching tests when DATABASE_URL is unset. WS tests run regardless.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient

from api.main import create_app
from x_alpaca_trading_bot import db, journal


# ---- Fake orchestrator ---------------------------------------------------

@dataclass
class _FakeStrategyPosition:
    stop_price: Decimal
    ratchet_level: int


@dataclass
class _FakePosition:
    signal_id: int
    ticker: str
    contract_symbol: str
    option_type: str
    strike: Decimal
    expiration: date
    qty: int
    entry_price: Decimal
    opened_at: datetime
    strategy_position: _FakeStrategyPosition
    stop_order_id: str | None


@dataclass
class _FakeOrchState:
    active_switches: frozenset[str] = frozenset()


class FakeOrchestrator:
    """In-memory stand-in. The real Orchestrator has the same _open_positions
    and _state shapes, so the FastAPI routes are agnostic to which is wired."""

    def __init__(self) -> None:
        import queue
        self._open_positions: dict[int, _FakePosition] = {}
        self._state = _FakeOrchState()
        self._broadcast = lambda _e, _p: None  # replaced by lifespan
        self._post_queue: queue.Queue = queue.Queue()
        self.injected_posts: list[tuple[str, str, datetime]] = []
        self.shutdown_requested = False

    def _on_x_post(self, post_id: str, post_text: str, posted_at: datetime) -> None:
        """Records the call so tests can assert on it."""
        self.injected_posts.append((post_id, post_text, posted_at))
        self._post_queue.put((post_id, post_text, posted_at))

    def request_shutdown(self) -> None:
        self.shutdown_requested = True


# ---- DB helpers / fixtures ----------------------------------------------

def _db_url() -> str | None:
    load_dotenv(override=True)
    return os.environ.get("DATABASE_URL") or None


@pytest.fixture
def conn() -> Iterator[psycopg.Connection]:
    url = _db_url()
    if not url:
        pytest.skip("DATABASE_URL not set")
    c = db.connect(url)
    db.run_migrations(c, Path(__file__).resolve().parent.parent / "deploy")
    with c.cursor() as cur:
        cur.execute("DELETE FROM indicator_snapshots")
        cur.execute("DELETE FROM trades")
        cur.execute("DELETE FROM fills")
        cur.execute("DELETE FROM orders")
        cur.execute("DELETE FROM events")
        cur.execute("DELETE FROM signals")
        cur.execute("DELETE FROM x_posts")
    c.commit()
    yield c
    c.close()


def _seed_x_post(conn: psycopg.Connection, *, post_id: str = "seed-1") -> int:
    return journal.insert_raw_post(
        conn,
        post_id=post_id,
        post_text="$AAPL 6/20 $185c @ 2.50",
        posted_at=datetime(2026, 5, 13, 13, 30, tzinfo=timezone.utc),
        received_at=datetime(2026, 5, 13, 13, 30, 1, tzinfo=timezone.utc),
        parse_result=None,
        actionable=True,
    )


def _seed_signal(conn: psycopg.Connection, x_post_id: int, *, taken: bool = True) -> int:
    return journal.insert_signal(
        conn,
        x_post_id=x_post_id,
        parsed_at=datetime(2026, 5, 13, 13, 30, tzinfo=timezone.utc),
        ticker="AAPL",
        option_type="call",
        strike=Decimal("185.00"),
        expiration=date(2026, 6, 20),
        posted_price=Decimal("2.50"),
        live_ask=Decimal("2.55"),
        taken=taken,
        rejection_reason=None if taken else "time_age",
        gate_results={"accepted": taken, "gates": []},
    )


def _seed_trade(
    conn: psycopg.Connection,
    *,
    signal_id: int | None,
    gross_pnl: Decimal,
    pnl_pct: Decimal,
    closed_at: datetime | None = None,
    exit_reason: str = "stop_loss",
) -> int:
    closed = closed_at or datetime(2026, 5, 13, 14, 0, tzinfo=timezone.utc)
    return journal.insert_trade(
        conn,
        signal_id=signal_id,
        opened_at=closed - timedelta(minutes=30),
        closed_at=closed,
        ticker="AAPL",
        option_type="call",
        strike=Decimal("185.00"),
        expiration=date(2026, 6, 20),
        entry_price=Decimal("2.50"),
        exit_price=Decimal("2.50") + gross_pnl,
        qty=1,
        exit_reason=exit_reason,
    )


# ---- /healthz -------------------------------------------------------------

def test_healthz_returns_ok() -> None:
    orch = FakeOrchestrator()
    app = create_app(conn=None, orchestrator=orch)
    with TestClient(app) as client:
        r = client.get("/healthz")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["open_positions"] == 0


# ---- /positions -----------------------------------------------------------

def test_positions_empty_returns_empty_list() -> None:
    orch = FakeOrchestrator()
    app = create_app(conn=None, orchestrator=orch)
    with TestClient(app) as client:
        r = client.get("/positions")
        assert r.status_code == 200
        assert r.json() == []


def test_positions_returns_orchestrator_state() -> None:
    orch = FakeOrchestrator()
    pos = _FakePosition(
        signal_id=42, ticker="AAPL", contract_symbol="AAPL260620C00185000",
        option_type="call", strike=Decimal("185.00"),
        expiration=date(2026, 6, 20),
        qty=1, entry_price=Decimal("2.55"),
        opened_at=datetime(2026, 5, 13, 13, 30, tzinfo=timezone.utc),
        strategy_position=_FakeStrategyPosition(stop_price=Decimal("2.04"), ratchet_level=0),
        stop_order_id="fake-stop-1",
    )
    orch._open_positions[42] = pos

    app = create_app(conn=None, orchestrator=orch)
    with TestClient(app) as client:
        r = client.get("/positions")
        assert r.status_code == 200
        body = r.json()
        assert len(body) == 1
        p = body[0]
        assert p["signal_id"] == 42
        assert p["ticker"] == "AAPL"
        assert p["strike"] == "185.00"
        assert p["entry_price"] == "2.55"
        assert p["current_stop_price"] == "2.04"
        assert p["ratchet_level"] == 0
        assert p["stop_order_id"] == "fake-stop-1"


# ---- /signals -------------------------------------------------------------

def test_signals_returns_recent_history(conn: psycopg.Connection) -> None:
    xid = _seed_x_post(conn)
    _seed_signal(conn, xid, taken=True)
    _seed_signal(conn, xid, taken=False)
    orch = FakeOrchestrator()
    app = create_app(conn=conn, orchestrator=orch)
    with TestClient(app) as client:
        r = client.get("/signals")
        assert r.status_code == 200
        rows = r.json()
        assert len(rows) == 2
        # Most recent first → rejected one first (was inserted second).
        assert rows[0]["taken"] is False
        assert rows[0]["rejection_reason"] == "time_age"
        assert rows[1]["taken"] is True


def test_signals_respects_limit(conn: psycopg.Connection) -> None:
    xid = _seed_x_post(conn)
    for _ in range(5):
        _seed_signal(conn, xid)
    orch = FakeOrchestrator()
    app = create_app(conn=conn, orchestrator=orch)
    with TestClient(app) as client:
        r = client.get("/signals?limit=2")
        assert r.status_code == 200
        assert len(r.json()) == 2


# ---- /performance --------------------------------------------------------

def test_performance_empty_returns_zero_stats(conn: psycopg.Connection) -> None:
    orch = FakeOrchestrator()
    app = create_app(conn=conn, orchestrator=orch)
    with TestClient(app) as client:
        r = client.get("/performance")
        assert r.status_code == 200
        body = r.json()
        assert body["trades"] == []
        assert body["stats"]["total_trades"] == 0
        assert body["stats"]["win_rate"] is None


def test_performance_aggregates_correctly(conn: psycopg.Connection) -> None:
    xid = _seed_x_post(conn)
    signal_id = _seed_signal(conn, xid)
    # 3 wins, 2 losses
    _seed_trade(conn, signal_id=signal_id, gross_pnl=Decimal("0.30"),
                pnl_pct=Decimal("0.12"))
    _seed_trade(conn, signal_id=signal_id, gross_pnl=Decimal("0.50"),
                pnl_pct=Decimal("0.20"))
    _seed_trade(conn, signal_id=signal_id, gross_pnl=Decimal("0.20"),
                pnl_pct=Decimal("0.08"))
    _seed_trade(conn, signal_id=signal_id, gross_pnl=Decimal("-0.50"),
                pnl_pct=Decimal("-0.20"))
    _seed_trade(conn, signal_id=signal_id, gross_pnl=Decimal("-0.20"),
                pnl_pct=Decimal("-0.08"))

    orch = FakeOrchestrator()
    app = create_app(conn=conn, orchestrator=orch)
    with TestClient(app) as client:
        r = client.get("/performance")
        body = r.json()
        stats = body["stats"]
        assert stats["total_trades"] == 5
        assert stats["wins"] == 3
        assert stats["losses"] == 2
        assert stats["win_rate"] == pytest.approx(0.6)
        # Per-share deltas 0.30+0.50+0.20-0.50-0.20 = 0.30, ×100 shares = $30.
        assert Decimal(stats["total_pnl"]) == Decimal("30")
        assert Decimal(stats["avg_win_pct"]) == Decimal("0.40") / Decimal(3)
        # profit_factor: ratio is multiplier-invariant — wins 100 / losses 70 ≈ 1.43
        assert stats["profit_factor"] == pytest.approx(0.40 / 0.28, rel=0.01)


# ---- WebSocket -----------------------------------------------------------

def test_ws_echoes_pings() -> None:
    orch = FakeOrchestrator()
    app = create_app(conn=None, orchestrator=orch, heartbeat_seconds=999)
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_text("hello")
            msg = ws.receive_json()
            assert msg["event"] == "pong"
            assert msg["payload"]["echo"] == "hello"


def test_ws_receives_broadcast_from_manager() -> None:
    orch = FakeOrchestrator()
    app = create_app(conn=None, orchestrator=orch, heartbeat_seconds=999)
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            ws_manager = app.state.ws_manager
            # Schedule a broadcast through dispatch_threadsafe — this is
            # exactly the path the orchestrator uses.
            ws_manager.dispatch_threadsafe("trade.entered", {"signal_id": 9})
            msg = ws.receive_json()
            assert msg["event"] == "trade.entered"
            assert msg["payload"] == {"signal_id": 9}


def test_ws_multiple_clients_each_receive_broadcast() -> None:
    orch = FakeOrchestrator()
    app = create_app(conn=None, orchestrator=orch, heartbeat_seconds=999)
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws1, \
             client.websocket_connect("/ws") as ws2:
            ws_manager = app.state.ws_manager
            ws_manager.dispatch_threadsafe("signal.validated", {"signal_id": 5})
            m1 = ws1.receive_json()
            m2 = ws2.receive_json()
            assert m1["event"] == "signal.validated"
            assert m2["event"] == "signal.validated"


def test_ws_disconnect_removes_from_clients() -> None:
    orch = FakeOrchestrator()
    app = create_app(conn=None, orchestrator=orch, heartbeat_seconds=999)
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_text("ping")
            ws.receive_json()
            assert app.state.ws_manager.client_count == 1
        # After context exits the client disconnects; manager should drain.
        # Give the server loop one tick to process the close.
        import time
        for _ in range(20):
            if app.state.ws_manager.client_count == 0:
                break
            time.sleep(0.05)
        assert app.state.ws_manager.client_count == 0


def test_ws_reconnect_then_receives() -> None:
    """Disconnect, reconnect, broadcast — second client still gets it."""
    orch = FakeOrchestrator()
    app = create_app(conn=None, orchestrator=orch, heartbeat_seconds=999)
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws1:
            ws1.send_text("x")
            ws1.receive_json()
        with client.websocket_connect("/ws") as ws2:
            app.state.ws_manager.dispatch_threadsafe("trade.stop_moved", {"new_stop": "2.55"})
            msg = ws2.receive_json()
            assert msg["event"] == "trade.stop_moved"


# ---- Orchestrator wiring -------------------------------------------------

# ---- /timeline -------------------------------------------------------------

def test_timeline_empty(conn: psycopg.Connection) -> None:
    orch = FakeOrchestrator()
    app = create_app(conn=conn, orchestrator=orch, heartbeat_seconds=999)
    with TestClient(app) as client:
        r = client.get("/timeline")
        assert r.status_code == 200
        assert r.json() == []


def test_timeline_classifies_kinds_correctly(conn: psycopg.Connection) -> None:
    """All four `kind` values should round-trip from a populated DB."""
    # 1. signal_unactionable: post with no signal row
    journal.insert_raw_post(
        conn,
        post_id="post-noop",
        post_text="just commentary, not a signal",
        posted_at=datetime(2026, 5, 10, 13, 0, tzinfo=timezone.utc),
        received_at=datetime(2026, 5, 10, 13, 0, 1, tzinfo=timezone.utc),
        parse_result={"signal": None, "parse_version": "v2"},
        actionable=False,
    )

    # 2. signal_rejected: post + signal taken=False
    xid_rej = journal.insert_raw_post(
        conn, post_id="post-rej",
        post_text="$AAPL 6/20 $185c @ 2.50",
        posted_at=datetime(2026, 5, 11, 14, 0, tzinfo=timezone.utc),
        received_at=datetime(2026, 5, 11, 14, 0, 1, tzinfo=timezone.utc),
        parse_result={"signal": {"ticker": "AAPL"}, "parse_version": "v2"},
        actionable=True,
    )
    journal.insert_signal(
        conn, x_post_id=xid_rej,
        parsed_at=datetime(2026, 5, 11, 14, 0, 2, tzinfo=timezone.utc),
        ticker="AAPL", option_type="call",
        strike=Decimal("185.00"), expiration=date(2026, 6, 20),
        posted_price=Decimal("2.50"), live_ask=Decimal("8.00"),
        taken=False, rejection_reason="price_deviation",
        gate_results={"accepted": False, "gates": []},
    )

    # 3. trade_closed: post + signal taken + trade row
    xid_done = journal.insert_raw_post(
        conn, post_id="post-done",
        post_text="$TSLA 7/18 $230p @ 4.20",
        posted_at=datetime(2026, 5, 12, 15, 0, tzinfo=timezone.utc),
        received_at=datetime(2026, 5, 12, 15, 0, 1, tzinfo=timezone.utc),
        parse_result={"signal": {"ticker": "TSLA"}, "parse_version": "v2"},
        actionable=True,
    )
    sig_done = journal.insert_signal(
        conn, x_post_id=xid_done,
        parsed_at=datetime(2026, 5, 12, 15, 0, 2, tzinfo=timezone.utc),
        ticker="TSLA", option_type="put",
        strike=Decimal("230.00"), expiration=date(2026, 7, 18),
        posted_price=Decimal("4.20"), live_ask=Decimal("4.25"),
        taken=True, rejection_reason=None,
        gate_results={"accepted": True, "gates": []},
    )
    journal.insert_trade(
        conn, signal_id=sig_done,
        opened_at=datetime(2026, 5, 12, 15, 1, tzinfo=timezone.utc),
        closed_at=datetime(2026, 5, 12, 16, 30, tzinfo=timezone.utc),
        ticker="TSLA", option_type="put",
        strike=Decimal("230.00"), expiration=date(2026, 7, 18),
        entry_price=Decimal("4.25"), exit_price=Decimal("5.50"),
        qty=1, exit_reason="stop_loss",
        max_gain_pct=Decimal("0.40"), max_loss_pct=Decimal("-0.05"),
    )

    # 4. position_open: post + signal taken + NO trade row yet
    xid_open = journal.insert_raw_post(
        conn, post_id="post-open",
        post_text="$NVDA 6/27 $130c @ 3.10",
        posted_at=datetime(2026, 5, 13, 16, 0, tzinfo=timezone.utc),
        received_at=datetime(2026, 5, 13, 16, 0, 1, tzinfo=timezone.utc),
        parse_result={"signal": {"ticker": "NVDA"}, "parse_version": "v2"},
        actionable=True,
    )
    journal.insert_signal(
        conn, x_post_id=xid_open,
        parsed_at=datetime(2026, 5, 13, 16, 0, 2, tzinfo=timezone.utc),
        ticker="NVDA", option_type="call",
        strike=Decimal("130.00"), expiration=date(2026, 6, 27),
        posted_price=Decimal("3.10"), live_ask=Decimal("3.15"),
        taken=True, rejection_reason=None,
        gate_results={"accepted": True, "gates": []},
    )

    orch = FakeOrchestrator()
    app = create_app(conn=conn, orchestrator=orch, heartbeat_seconds=999)
    with TestClient(app) as client:
        rows = client.get("/timeline").json()

    # Most recent first → NVDA, TSLA, AAPL, then commentary
    assert [r["kind"] for r in rows] == [
        "position_open", "trade_closed", "signal_rejected", "signal_unactionable",
    ]
    # NVDA card carries the originating tweet + signal context
    nvda = rows[0]
    assert nvda["post_text"] == "$NVDA 6/27 $130c @ 3.10"
    assert nvda["signal"]["ticker"] == "NVDA"
    assert nvda["signal"]["taken"] is True
    assert nvda["trade"] is None
    # TSLA closed trade carries the trade summary
    tsla = rows[1]
    # (5.50 - 4.25) per share × 1 qty × 100 shares = $125.00
    assert tsla["trade"]["gross_pnl"] == "125.0000"
    assert tsla["trade"]["exit_reason"] == "stop_loss"
    assert tsla["trade"]["max_gain_pct"] == "0.4000"
    # AAPL was rejected
    aapl = rows[2]
    assert aapl["signal"]["taken"] is False
    assert aapl["signal"]["rejection_reason"] == "price_deviation"
    # Non-signal post
    noop = rows[3]
    assert noop["signal"] is None
    assert noop["post_text"] == "just commentary, not a signal"


def test_timeline_excludes_rejected_when_requested(conn: psycopg.Connection) -> None:
    xid = journal.insert_raw_post(
        conn, post_id="rej-1",
        post_text="$X 6/20 $5c @ 100",
        posted_at=datetime(2026, 5, 11, 14, 0, tzinfo=timezone.utc),
        received_at=datetime(2026, 5, 11, 14, 0, 1, tzinfo=timezone.utc),
        parse_result=None, actionable=True,
    )
    journal.insert_signal(
        conn, x_post_id=xid,
        parsed_at=datetime(2026, 5, 11, 14, 0, 2, tzinfo=timezone.utc),
        ticker="X", option_type="call",
        strike=Decimal("5.00"), expiration=date(2026, 6, 20),
        posted_price=Decimal("100.00"), live_ask=None,
        taken=False, rejection_reason="contract_exists",
        gate_results={"accepted": False, "gates": []},
    )
    orch = FakeOrchestrator()
    app = create_app(conn=conn, orchestrator=orch, heartbeat_seconds=999)
    with TestClient(app) as client:
        with_rej = client.get("/timeline?include_rejected=true").json()
        without = client.get("/timeline?include_rejected=false").json()
    assert len(with_rej) == 1 and with_rej[0]["kind"] == "signal_rejected"
    assert without == []


# ---- /positions includes originating tweet --------------------------------

def test_positions_includes_source_post(conn: psycopg.Connection) -> None:
    xid = journal.insert_raw_post(
        conn, post_id="open-1",
        post_text="$AAPL 6/20 $185c @ 2.50",
        posted_at=datetime(2026, 5, 13, 13, 30, tzinfo=timezone.utc),
        received_at=datetime(2026, 5, 13, 13, 30, 1, tzinfo=timezone.utc),
        parse_result=None, actionable=True,
    )
    signal_id = journal.insert_signal(
        conn, x_post_id=xid,
        parsed_at=datetime(2026, 5, 13, 13, 30, 2, tzinfo=timezone.utc),
        ticker="AAPL", option_type="call",
        strike=Decimal("185.00"), expiration=date(2026, 6, 20),
        posted_price=Decimal("2.50"), live_ask=Decimal("2.55"),
        taken=True, rejection_reason=None,
        gate_results={"accepted": True, "gates": []},
    )

    orch = FakeOrchestrator()
    orch._open_positions[signal_id] = _FakePosition(
        signal_id=signal_id, ticker="AAPL", contract_symbol="AAPL260620C00185000",
        option_type="call", strike=Decimal("185.00"), expiration=date(2026, 6, 20),
        qty=1, entry_price=Decimal("2.55"),
        opened_at=datetime(2026, 5, 13, 13, 30, 5, tzinfo=timezone.utc),
        strategy_position=_FakeStrategyPosition(stop_price=Decimal("2.04"), ratchet_level=0),
        stop_order_id="fake-1",
    )

    app = create_app(conn=conn, orchestrator=orch, heartbeat_seconds=999)
    with TestClient(app) as client:
        body = client.get("/positions").json()
    assert len(body) == 1
    assert body[0]["source_post"]["post_text"] == "$AAPL 6/20 $185c @ 2.50"
    # Same instant regardless of TZ rendering — parse and compare epoch
    from datetime import datetime as _dt
    parsed = _dt.fromisoformat(body[0]["source_post"]["posted_at"])
    assert parsed == datetime(2026, 5, 13, 13, 30, tzinfo=timezone.utc)


# ---- /debug/inject-post ----------------------------------------------------

def test_inject_post_disabled_when_token_unset(monkeypatch) -> None:
    monkeypatch.delenv("DEBUG_INJECT_TOKEN", raising=False)
    orch = FakeOrchestrator()
    app = create_app(conn=None, orchestrator=orch, heartbeat_seconds=999)
    with TestClient(app) as client:
        r = client.post("/debug/inject-post", json={"post_text": "x"})
        assert r.status_code == 503
        assert "disabled" in r.json()["detail"]


def test_inject_post_requires_auth_header(monkeypatch) -> None:
    monkeypatch.setenv("DEBUG_INJECT_TOKEN", "supersecret")
    orch = FakeOrchestrator()
    app = create_app(conn=None, orchestrator=orch, heartbeat_seconds=999)
    with TestClient(app) as client:
        r = client.post("/debug/inject-post", json={"post_text": "x"})
        assert r.status_code == 401


def test_inject_post_rejects_wrong_token(monkeypatch) -> None:
    monkeypatch.setenv("DEBUG_INJECT_TOKEN", "supersecret")
    orch = FakeOrchestrator()
    app = create_app(conn=None, orchestrator=orch, heartbeat_seconds=999)
    with TestClient(app) as client:
        r = client.post(
            "/debug/inject-post",
            headers={"Authorization": "Bearer wrong"},
            json={"post_text": "x"},
        )
        assert r.status_code == 401


def test_inject_post_pushes_to_orchestrator(monkeypatch) -> None:
    monkeypatch.setenv("DEBUG_INJECT_TOKEN", "supersecret")
    orch = FakeOrchestrator()
    app = create_app(conn=None, orchestrator=orch, heartbeat_seconds=999)
    with TestClient(app) as client:
        r = client.post(
            "/debug/inject-post",
            headers={"Authorization": "Bearer supersecret"},
            json={
                "post_text": "$AAPL 6/20 $185c @ 2.50",
                "post_id": "manual-test-1",
                "posted_at": "2026-05-13T13:30:00Z",
            },
        )
        assert r.status_code == 200, r.json()
        body = r.json()
        assert body["queued"] is True
        assert body["post_id"] == "manual-test-1"
        assert body["queue_depth"] == 1

    assert len(orch.injected_posts) == 1
    pid, text, posted_at = orch.injected_posts[0]
    assert pid == "manual-test-1"
    assert text == "$AAPL 6/20 $185c @ 2.50"
    assert posted_at.isoformat() == "2026-05-13T13:30:00+00:00"


def test_inject_post_generates_post_id_when_missing(monkeypatch) -> None:
    monkeypatch.setenv("DEBUG_INJECT_TOKEN", "supersecret")
    orch = FakeOrchestrator()
    app = create_app(conn=None, orchestrator=orch, heartbeat_seconds=999)
    with TestClient(app) as client:
        r = client.post(
            "/debug/inject-post",
            headers={"Authorization": "Bearer supersecret"},
            json={"post_text": "$AAPL test"},
        )
        assert r.status_code == 200
        assert r.json()["post_id"].startswith("manual-")


def test_inject_post_rejects_missing_post_text(monkeypatch) -> None:
    monkeypatch.setenv("DEBUG_INJECT_TOKEN", "supersecret")
    orch = FakeOrchestrator()
    app = create_app(conn=None, orchestrator=orch, heartbeat_seconds=999)
    with TestClient(app) as client:
        r = client.post(
            "/debug/inject-post",
            headers={"Authorization": "Bearer supersecret"},
            json={"post_id": "x-only"},
        )
        assert r.status_code == 400


def test_inject_post_rejects_invalid_posted_at(monkeypatch) -> None:
    monkeypatch.setenv("DEBUG_INJECT_TOKEN", "supersecret")
    orch = FakeOrchestrator()
    app = create_app(conn=None, orchestrator=orch, heartbeat_seconds=999)
    with TestClient(app) as client:
        r = client.post(
            "/debug/inject-post",
            headers={"Authorization": "Bearer supersecret"},
            json={"post_text": "x", "posted_at": "not-a-date"},
        )
        assert r.status_code == 400


def test_orchestrator_broadcast_wired_to_ws_manager() -> None:
    """After app startup, orchestrator._broadcast should dispatch to clients."""
    orch = FakeOrchestrator()
    app = create_app(conn=None, orchestrator=orch, heartbeat_seconds=999)
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            # Simulate the orchestrator firing an event from its thread.
            orch._broadcast("trade.exited", {"signal_id": 1, "reason": "stop_loss"})
            msg = ws.receive_json()
            assert msg["event"] == "trade.exited"
            assert msg["payload"]["reason"] == "stop_loss"
