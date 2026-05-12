"""Integration tests for capture_snapshot + close_trade — Phase 7 gates.

Uses a hand-rolled FakeMarketDataProvider so the test is deterministic (no
live API surprises) while still exercising the real journal writes against
the local Postgres. Each test cleans the DB to start fresh.

Skips cleanly when DATABASE_URL is unset.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
from dotenv import load_dotenv

from x_alpaca_trading_bot import db, journal
from x_alpaca_trading_bot.data_service import (
    Greeks,
    Indicators,
    IVData,
    MarketContext,
    Quote,
)
from x_alpaca_trading_bot.snapshot import (
    SnapshotContext,
    SnapshotScheduler,
    capture_snapshot,
    close_trade,
)


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
        cur.execute("DELETE FROM events")
        cur.execute("DELETE FROM signals")
        cur.execute("DELETE FROM x_posts")
    c.commit()
    try:
        yield c
    finally:
        c.close()


# ---- Fake provider --------------------------------------------------------

class HappyProvider:
    """Returns realistic populated data for every call."""

    def is_market_open(self) -> bool:
        return True

    def get_option_quote(self, ticker, expiration, option_type, strike) -> Quote:
        bid = Decimal("2.45")
        ask = Decimal("2.55")
        mid = (bid + ask) / Decimal(2)
        return Quote(bid=bid, ask=ask, mid=mid, spread_pct=(ask - bid) / mid, ts=datetime(2026, 5, 12, 14, 30, tzinfo=timezone.utc))

    def get_greeks(self, contract_symbol: str) -> Greeks:
        return Greeks(
            delta=Decimal("0.55"),
            gamma=Decimal("0.03"),
            theta=Decimal("-0.07"),
            vega=Decimal("0.18"),
        )

    def get_iv_data(self, contract_symbol: str) -> IVData:
        return IVData(iv=Decimal("0.32"), iv_rank=None, iv_percentile=None)

    def get_indicators(self, ticker: str, now: datetime) -> Indicators:
        return Indicators(
            rsi_14=Decimal("58.4"),
            macd=Decimal("0.15"),
            macd_signal=Decimal("0.10"),
            vwap=Decimal("185.20"),
            ema_9=Decimal("184.90"),
            ema_21=Decimal("184.10"),
            atr_14=Decimal("2.10"),
            bb_position=Decimal("0.62"),
        )

    def get_market_context(self, now: datetime) -> MarketContext:
        return MarketContext(
            vix=Decimal("17.5"),
            spy_vs_ema21="above",
            qqq_vs_ema21="above",
            sector_etf_trend={
                "XLK": Decimal("0.0123"),
                "XLE": Decimal("-0.0045"),
                "XLF": Decimal("0.0067"),
            },
        )

    def get_underlying_price(self, ticker: str) -> Decimal:
        return Decimal("185.10")


class FailingProvider:
    """Every call raises — used to verify resilience."""

    def is_market_open(self) -> bool:
        raise RuntimeError("provider down")

    def get_option_quote(self, ticker, expiration, option_type, strike):
        raise RuntimeError("quote unavailable")

    def get_greeks(self, contract_symbol: str):
        raise RuntimeError("greeks unavailable")

    def get_iv_data(self, contract_symbol: str):
        raise RuntimeError("iv unavailable")

    def get_indicators(self, ticker: str, now: datetime):
        raise RuntimeError("indicators unavailable")

    def get_market_context(self, now: datetime):
        raise RuntimeError("market context unavailable")

    def get_underlying_price(self, ticker: str):
        raise RuntimeError("price unavailable")


# ---- Helpers ---------------------------------------------------------------

def _seed_signal(conn: psycopg.Connection) -> int:
    """Insert one x_posts + signals row so snapshots can FK to it.

    Returns the signal id.
    """
    posted = datetime(2026, 5, 12, 14, 25, tzinfo=timezone.utc)
    x_id = journal.insert_raw_post(
        conn,
        post_id=f"seed-{datetime.now(timezone.utc).timestamp()}",
        post_text="$AAPL 6/20 $185c @ 2.50",
        posted_at=posted,
        received_at=posted + timedelta(seconds=1),
        parse_result={"signal": {"ticker": "AAPL"}, "parse_version": "v1"},
        actionable=True,
    )
    signal_id = journal.insert_signal(
        conn,
        x_post_id=x_id,
        parsed_at=posted + timedelta(seconds=2),
        ticker="AAPL",
        option_type="call",
        strike=Decimal("185.00"),
        expiration=date(2026, 6, 20),
        posted_price=Decimal("2.50"),
        live_ask=Decimal("2.55"),
        taken=True,
        rejection_reason=None,
        gate_results={"accepted": True, "gates": []},
    )
    return signal_id


def _ctx(signal_id: int) -> SnapshotContext:
    return SnapshotContext(
        signal_id=signal_id,
        contract_symbol="AAPL260620C00185000",
        underlying_ticker="AAPL",
    )


def _read_snapshot(conn: psycopg.Connection, snapshot_id: int) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM indicator_snapshots WHERE id = %s", (snapshot_id,))
        colnames = [d.name for d in cur.description]
        row = cur.fetchone()
    return dict(zip(colnames, row))


# ---- capture_snapshot: happy path ----------------------------------------

def test_capture_snapshot_happy_path_populates_all_fields(conn: psycopg.Connection) -> None:
    signal_id = _seed_signal(conn)
    now = datetime(2026, 5, 12, 14, 30, tzinfo=timezone.utc)

    sid = capture_snapshot(
        conn,
        HappyProvider(),
        _ctx(signal_id),
        snapshot_type="entry",
        now=now,
        option_expiration=date(2026, 6, 20),
        option_type="call",
        strike=Decimal("185.00"),
    )
    row = _read_snapshot(conn, sid)
    assert row["snapshot_type"] == "entry"
    assert row["delta"] == Decimal("0.5500")
    assert row["theta"] == Decimal("-0.0700")
    assert row["iv"] == Decimal("0.3200")
    assert row["rsi_14"] == Decimal("58.4000")
    assert row["vwap"] == Decimal("185.2000")
    assert row["atr_14"] == Decimal("2.1000")
    assert row["vix"] == Decimal("17.5000")
    assert row["spy_vs_ema21"] == "above"
    assert row["option_bid"] == Decimal("2.4500")
    assert row["option_ask"] == Decimal("2.5500")
    assert row["option_mid"] == Decimal("2.5000")
    assert row["underlying_price"] == Decimal("185.1000")
    # Sector field is a packed string of top-3 absolute movers
    assert "XLK" in row["sector_etf_trend"]


# ---- capture_snapshot: every source failing → null + events --------------

def test_capture_snapshot_survives_total_provider_failure(conn: psycopg.Connection) -> None:
    signal_id = _seed_signal(conn)
    now = datetime(2026, 5, 12, 14, 30, tzinfo=timezone.utc)

    sid = capture_snapshot(
        conn,
        FailingProvider(),
        _ctx(signal_id),
        snapshot_type="monitor",
        now=now,
        option_expiration=date(2026, 6, 20),
        option_type="call",
        strike=Decimal("185.00"),
    )
    row = _read_snapshot(conn, sid)
    assert row["snapshot_type"] == "monitor"
    # Every indicator field should be NULL
    for col in ("delta", "gamma", "theta", "vega", "iv", "rsi_14", "macd",
                "vwap", "ema_9", "ema_21", "atr_14", "bb_position",
                "vix", "underlying_price", "option_bid", "option_ask", "option_mid"):
        assert row[col] is None, f"{col} should be NULL when source failed"

    # And we should have logged one event per failing source (6 sources).
    with conn.cursor() as cur:
        cur.execute(
            "SELECT category, message FROM events WHERE category = 'snapshot'"
            " ORDER BY id"
        )
        events = cur.fetchall()
    # 6 sources fail: greeks, iv, indicators, market_context, underlying_price, option_quote.
    assert len(events) == 6
    messages = {m for _, m in events}
    assert "data_source_unavailable:greeks" in messages
    assert "data_source_unavailable:iv" in messages
    assert "data_source_unavailable:indicators" in messages
    assert "data_source_unavailable:market_context" in messages
    assert "data_source_unavailable:underlying_price" in messages
    assert "data_source_unavailable:option_quote" in messages


# ---- capture_snapshot: option_quote optional ------------------------------

def test_capture_snapshot_without_option_params_skips_quote_fetch(conn: psycopg.Connection) -> None:
    """If caller doesn't pass expiration/type/strike, option_bid/ask are NULL
    and no option_quote event is logged (we never tried).
    """
    signal_id = _seed_signal(conn)
    now = datetime(2026, 5, 12, 14, 30, tzinfo=timezone.utc)

    sid = capture_snapshot(
        conn, HappyProvider(), _ctx(signal_id),
        snapshot_type="monitor", now=now,
    )
    row = _read_snapshot(conn, sid)
    assert row["option_bid"] is None
    assert row["option_ask"] is None
    # Other fields still populated
    assert row["delta"] == Decimal("0.5500")


# ---- close_trade ----------------------------------------------------------

def test_close_trade_writes_exit_snapshot_and_trade(conn: psycopg.Connection) -> None:
    signal_id = _seed_signal(conn)
    scheduler = SnapshotScheduler()
    opened_at = datetime(2026, 5, 12, 13, 30, tzinfo=timezone.utc)
    closed_at = datetime(2026, 5, 12, 15, 0, tzinfo=timezone.utc)
    scheduler.register(_ctx(signal_id), opened_at=opened_at)
    scheduler.update_extremes(signal_id, Decimal("0.40"))
    scheduler.update_extremes(signal_id, Decimal("-0.05"))

    snapshot_id, trade_id = close_trade(
        conn, HappyProvider(),
        ctx=_ctx(signal_id), scheduler=scheduler,
        opened_at=opened_at, closed_at=closed_at,
        option_type="call",
        strike=Decimal("185.00"),
        expiration=date(2026, 6, 20),
        entry_price=Decimal("2.50"),
        exit_price=Decimal("3.10"),
        qty=1,
        exit_reason="stop_loss",
    )

    # Exit snapshot landed
    snap = _read_snapshot(conn, snapshot_id)
    assert snap["snapshot_type"] == "exit"
    assert snap["delta"] == Decimal("0.5500")

    # Trade row carries the right summary
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ticker, entry_price, exit_price, gross_pnl, pnl_pct,"
            "       exit_reason, hold_minutes, max_gain_pct, max_loss_pct"
            " FROM trades WHERE id = %s",
            (trade_id,),
        )
        row = cur.fetchone()
    ticker, entry, exit_, gross_pnl, pnl_pct, reason, hold_minutes, max_g, max_l = row
    assert ticker == "AAPL"
    assert entry == Decimal("2.5000")
    assert exit_ == Decimal("3.1000")
    assert gross_pnl == Decimal("0.6000")
    assert pnl_pct == Decimal("0.2400")  # (3.10 - 2.50) / 2.50
    assert reason == "stop_loss"
    assert hold_minutes == 90
    assert max_g == Decimal("0.4000")
    assert max_l == Decimal("-0.0500")

    # Scheduler unregistered the position
    assert scheduler.get(signal_id) is None


def test_close_trade_works_without_scheduler_state(conn: psycopg.Connection) -> None:
    """If the position wasn't tracked (e.g., adopted via reconciliation),
    max_gain_pct / max_loss_pct land as NULL — not an exception."""
    signal_id = _seed_signal(conn)
    scheduler = SnapshotScheduler()
    opened_at = datetime(2026, 5, 12, 13, 30, tzinfo=timezone.utc)
    closed_at = datetime(2026, 5, 12, 14, 0, tzinfo=timezone.utc)

    _, trade_id = close_trade(
        conn, HappyProvider(),
        ctx=_ctx(signal_id), scheduler=scheduler,
        opened_at=opened_at, closed_at=closed_at,
        option_type="call",
        strike=Decimal("185.00"),
        expiration=date(2026, 6, 20),
        entry_price=Decimal("2.00"),
        exit_price=Decimal("1.80"),
        qty=2,
        exit_reason="time_stop_1555",
    )
    with conn.cursor() as cur:
        cur.execute("SELECT gross_pnl, qty, max_gain_pct, max_loss_pct FROM trades WHERE id = %s", (trade_id,))
        gross_pnl, qty, max_g, max_l = cur.fetchone()
    assert gross_pnl == Decimal("-0.4000")  # (1.80 - 2.00) * 2
    assert qty == 2
    assert max_g is None
    assert max_l is None


# ---- Cadence × DB lifecycle integration ------------------------------------

def test_entry_then_two_monitors_then_exit_lifecycle(conn: psycopg.Connection) -> None:
    """End-to-end: 4 snapshots written for one signal over the position's life."""
    signal_id = _seed_signal(conn)
    scheduler = SnapshotScheduler()
    opened_at = datetime(2026, 5, 12, 13, 30, tzinfo=timezone.utc)
    scheduler.register(_ctx(signal_id), opened_at=opened_at)

    # Entry snapshot
    capture_snapshot(
        conn, HappyProvider(), _ctx(signal_id),
        snapshot_type="entry", now=opened_at,
        option_expiration=date(2026, 6, 20),
        option_type="call",
        strike=Decimal("185.00"),
    )

    # Two monitor snapshots
    for offset in (timedelta(minutes=15), timedelta(minutes=30)):
        ts = opened_at + offset
        capture_snapshot(
            conn, HappyProvider(), _ctx(signal_id),
            snapshot_type="monitor", now=ts,
        )
        scheduler.mark_snapshotted(signal_id, ts)

    # Close with exit
    close_trade(
        conn, HappyProvider(),
        ctx=_ctx(signal_id), scheduler=scheduler,
        opened_at=opened_at,
        closed_at=opened_at + timedelta(minutes=45),
        option_type="call",
        strike=Decimal("185.00"),
        expiration=date(2026, 6, 20),
        entry_price=Decimal("2.50"),
        exit_price=Decimal("3.00"),
        qty=1,
        exit_reason="stop_loss",
    )

    with conn.cursor() as cur:
        cur.execute(
            "SELECT snapshot_type, ts FROM indicator_snapshots"
            " WHERE signal_id = %s ORDER BY ts",
            (signal_id,),
        )
        rows = cur.fetchall()
    types = [r[0] for r in rows]
    assert types == ["entry", "monitor", "monitor", "exit"]
