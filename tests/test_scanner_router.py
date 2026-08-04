"""Integration tests for the scanner API router (scanner program UI).

Covers the S1 multi-hypothesis surface (per-scanner status counts,
scanner filter on events) and the S2 book endpoint (/scanner/trades).
Skips when DATABASE_URL is unset, same as the other API tests.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient

from api.main import create_app
from x_alpaca_trading_bot import db, journal


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
        cur.execute("DELETE FROM scanner_trades")
        cur.execute("DELETE FROM scanner_events")
    c.commit()
    yield c
    c.close()


# ---- Fakes mirroring the orchestrator attributes the router reads --------

@dataclass
class _FakeScannerCfg:
    scanner_enabled: bool = True
    scanner_interval_seconds: int = 300
    scanner_universe: tuple[str, ...] | None = ("RIVN", "SOFI")
    scanner_trading_enabled: bool = False
    scanner_trade_notional: Decimal = Decimal("1000")
    scanner_max_concurrent: int = 3
    scanner_min_volume_ratio: Decimal = Decimal("1.0")


@dataclass
class _FakeLab:
    _universe: tuple[str, ...] = ("RIVN", "SOFI")
    _hypotheses: dict = field(default_factory=lambda: {
        "failed_breakout": None, "vwap_reject": None,
        "gap_fade": None, "prior_low_break": None,
    })


class _FakeOrch:
    def __init__(self, *, armed: bool = False) -> None:
        import queue
        self._cfg = _FakeScannerCfg(scanner_trading_enabled=armed)
        self._scanner = _FakeLab()
        self._equity_positions: dict = {}
        self._open_positions: dict = {}
        self._state = type("S", (), {"active_switches": frozenset()})()
        self._post_queue = queue.Queue()
        self._broadcast = lambda _e, _p: None

    def request_shutdown(self) -> None:
        pass


def _seed_event(
    conn: psycopg.Connection, *, scanner: str, ticker: str,
    day: date = date(2026, 8, 4), vol: Decimal | None = Decimal("1.5"),
) -> None:
    ts = datetime(2026, 8, 4, 14, 0, tzinfo=timezone.utc)
    journal.insert_scanner_event(
        conn, scanner_name=scanner, ticker=ticker, detected_at=ts,
        event_day=day, breakout_ts=ts, breakout_price=Decimal("100.5"),
        failure_ts=ts, failure_price=Decimal("99.0"),
        prior_high=Decimal("100.0"), volume_ratio=vol,
    )


# ---- /scanner/status ------------------------------------------------------

def test_status_reports_hypotheses_and_per_scanner_counts(conn: psycopg.Connection) -> None:
    _seed_event(conn, scanner="failed_breakout", ticker="RIVN")
    _seed_event(conn, scanner="vwap_reject", ticker="RIVN")
    _seed_event(conn, scanner="vwap_reject", ticker="SOFI")

    app = create_app(conn=conn, orchestrator=_FakeOrch())
    with TestClient(app) as client:
        r = client.get("/api/scanner/status")
        assert r.status_code == 200
        body = r.json()

    assert body["phase"] == "S1"
    assert set(body["hypotheses"]) == {
        "failed_breakout", "vwap_reject", "gap_fade", "prior_low_break",
    }
    counts = {b["scanner_name"]: b["events_total"] for b in body["by_scanner"]}
    assert counts == {"failed_breakout": 1, "vwap_reject": 2}
    assert body["events_total"] == 3
    assert body["trading"]["enabled"] is False
    assert body["trading"]["max_concurrent"] == 3


def test_status_phase_s2_when_armed(conn: psycopg.Connection) -> None:
    app = create_app(conn=conn, orchestrator=_FakeOrch(armed=True))
    with TestClient(app) as client:
        body = client.get("/api/scanner/status").json()
    assert body["phase"] == "S2"
    assert body["trading"]["enabled"] is True


def test_status_survives_orchestrator_without_scanner_attrs(conn: psycopg.Connection) -> None:
    """A bare orchestrator (no _cfg/_scanner) must not 500 the endpoint."""
    class _Bare:
        def request_shutdown(self) -> None: ...

    app = create_app(conn=conn, orchestrator=_Bare())
    with TestClient(app) as client:
        r = client.get("/api/scanner/status")
    assert r.status_code == 200
    assert r.json()["enabled"] is False


# ---- /scanner (events) ----------------------------------------------------

def test_events_include_scanner_name_and_all_hypotheses(conn: psycopg.Connection) -> None:
    _seed_event(conn, scanner="failed_breakout", ticker="RIVN")
    _seed_event(conn, scanner="gap_fade", ticker="SOFI")

    app = create_app(conn=conn, orchestrator=_FakeOrch())
    with TestClient(app) as client:
        rows = client.get("/api/scanner?since_days=7").json()
    assert {r["scanner_name"] for r in rows} == {"failed_breakout", "gap_fade"}


def test_events_scanner_filter(conn: psycopg.Connection) -> None:
    _seed_event(conn, scanner="failed_breakout", ticker="RIVN")
    _seed_event(conn, scanner="gap_fade", ticker="SOFI")

    app = create_app(conn=conn, orchestrator=_FakeOrch())
    with TestClient(app) as client:
        rows = client.get("/api/scanner?since_days=7&scanner=gap_fade").json()
    assert len(rows) == 1
    assert rows[0]["scanner_name"] == "gap_fade"
    assert rows[0]["ticker"] == "SOFI"


def test_events_unknown_scanner_filter_ignored(conn: psycopg.Connection) -> None:
    """Names outside the whitelist are ignored (all events returned), not
    interpolated into SQL."""
    _seed_event(conn, scanner="failed_breakout", ticker="RIVN")
    app = create_app(conn=conn, orchestrator=_FakeOrch())
    with TestClient(app) as client:
        rows = client.get(
            "/api/scanner?since_days=7&scanner=nope%27%3BDROP"
        ).json()
    assert len(rows) == 1


# ---- /scanner/trades ------------------------------------------------------

def test_trades_endpoint_open_first_with_stats(conn: psycopg.Connection) -> None:
    opened = datetime(2026, 8, 4, 14, 0, tzinfo=timezone.utc)

    closed_id = journal.insert_scanner_trade(
        conn, scanner_name="failed_breakout", ticker="SOFI",
        event_day=date(2026, 8, 3), qty=59, entry_price=Decimal("16.80"),
        opened_at=opened.replace(day=3), entry_order_id=None, stop_order_id=None,
    )
    journal.close_scanner_trade(
        conn, trade_id=closed_id, closed_at=opened.replace(day=3, hour=15),
        exit_price=Decimal("16.70"), exit_reason="time_exit",
        gross_pnl=Decimal("5.90"), pnl_pct=Decimal("0.0060"), exit_order_id=None,
    )
    journal.insert_scanner_trade(
        conn, scanner_name="failed_breakout", ticker="RIVN",
        event_day=date(2026, 8, 4), qty=59, entry_price=Decimal("16.69"),
        opened_at=opened, entry_order_id=None, stop_order_id=None,
    )

    app = create_app(conn=conn, orchestrator=_FakeOrch())
    with TestClient(app) as client:
        body = client.get("/api/scanner/trades").json()

    assert [t["ticker"] for t in body["trades"]] == ["RIVN", "SOFI"]  # open first
    assert body["trades"][0]["is_open"] is True
    assert body["trades"][1]["exit_reason"] == "time_exit"
    assert body["stats"]["n_closed"] == 1
    assert body["stats"]["winners"] == 1
    assert Decimal(body["stats"]["total_pnl"]) == Decimal("5.90")


def test_trades_endpoint_empty_book(conn: psycopg.Connection) -> None:
    app = create_app(conn=conn, orchestrator=_FakeOrch())
    with TestClient(app) as client:
        body = client.get("/api/scanner/trades").json()
    assert body["trades"] == []
    assert body["stats"]["n_closed"] == 0


# ---- /timeline (scanner integration) --------------------------------------

def test_timeline_merges_scanner_items(conn: psycopg.Connection) -> None:
    """The feed interleaves scanner events and S2 trades with the X
    archive, newest first, each with a unique key."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM signal_price_tracks")
        cur.execute("DELETE FROM indicator_snapshots")
        cur.execute("DELETE FROM trades")
        cur.execute("DELETE FROM fills")
        cur.execute("DELETE FROM orders")
        cur.execute("DELETE FROM signals")
        cur.execute("DELETE FROM x_posts")
    conn.commit()

    _seed_event(conn, scanner="vwap_reject", ticker="RIVN")

    opened = datetime(2026, 8, 4, 14, 30, tzinfo=timezone.utc)
    open_id = journal.insert_scanner_trade(
        conn, scanner_name="failed_breakout", ticker="RIVN",
        event_day=date(2026, 8, 4), qty=59, entry_price=Decimal("16.69"),
        opened_at=opened, entry_order_id=None, stop_order_id=None,
    )
    closed_id = journal.insert_scanner_trade(
        conn, scanner_name="failed_breakout", ticker="SOFI",
        event_day=date(2026, 8, 4), qty=50, entry_price=Decimal("16.80"),
        opened_at=opened, entry_order_id=None, stop_order_id=None,
    )
    journal.close_scanner_trade(
        conn, trade_id=closed_id, closed_at=opened.replace(hour=15),
        exit_price=Decimal("16.70"), exit_reason="time_exit",
        gross_pnl=Decimal("5.00"), pnl_pct=Decimal("0.0060"), exit_order_id=None,
    )

    app = create_app(conn=conn, orchestrator=_FakeOrch())
    with TestClient(app) as client:
        items = client.get("/api/timeline").json()

    kinds = [i["kind"] for i in items]
    assert "scanner_event" in kinds
    assert "scanner_trade_open" in kinds
    assert "scanner_trade_closed" in kinds

    keys = [i["key"] for i in items]
    assert len(keys) == len(set(keys))          # globally unique
    assert f"st-{open_id}" in keys

    ts_list = [i["ts"] for i in items if i["ts"]]
    assert ts_list == sorted(ts_list, reverse=True)  # newest first

    closed = next(i for i in items if i["kind"] == "scanner_trade_closed")
    assert closed["ticker"] == "SOFI"
    assert Decimal(closed["gross_pnl"]) == Decimal("5.00")
    assert closed["exit_reason"] == "time_exit"


def test_timeline_scanner_only_respects_limit(conn: psycopg.Connection) -> None:
    for i in range(5):
        _seed_event(conn, scanner="failed_breakout", ticker=f"T{i}")
    app = create_app(conn=conn, orchestrator=_FakeOrch())
    with TestClient(app) as client:
        items = client.get("/api/timeline?limit=3").json()
    assert len(items) == 3
