"""Integration tests for risk_manager DB helpers and event journaling.

Phase 5 acceptance gates 2 and 3:
  - Manual injection of synthetic fills shows correct kill switch behavior.
  - Every risk decision (taken or rejected) written to events table.

The "fills" the spec mentions are summarized in the `trades` table by the
orchestrator at position close (Phase 7 work). Here we exercise the helpers
that the risk_manager will use against pre-populated trades rows.

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

from x_alpaca_trading_bot import db, journal, risk_manager
from x_alpaca_trading_bot.risk_manager import (
    SessionState,
    consecutive_loss_count,
    evaluate_and_log,
    realized_pnl_today,
)


def _database_url() -> str | None:
    load_dotenv(override=True)
    return os.environ.get("DATABASE_URL") or None


@pytest.fixture
def conn() -> Iterator[psycopg.Connection]:
    url = _database_url()
    if not url:
        pytest.skip("DATABASE_URL not set; skipping integration tests")
    try:
        c = db.connect(url)
    except psycopg.OperationalError as exc:
        pytest.skip(f"Cannot connect to {url}: {exc}")

    deploy_dir = Path(__file__).resolve().parent.parent / "deploy"
    db.run_migrations(c, deploy_dir)

    # Clear dependent tables in FK order.
    with c.cursor() as cur:
        cur.execute("DELETE FROM signal_price_tracks")
        cur.execute("DELETE FROM events")
        cur.execute("DELETE FROM indicator_snapshots")
        cur.execute("DELETE FROM trades")
        cur.execute("DELETE FROM fills")
        cur.execute("DELETE FROM orders")
        cur.execute("DELETE FROM signals")
        cur.execute("DELETE FROM x_posts")
    c.commit()
    try:
        yield c
    finally:
        c.close()


def _insert_trade(
    conn: psycopg.Connection,
    *,
    closed_at: datetime,
    gross_pnl: Decimal,
    pnl_pct: Decimal,
    exit_reason: str = "stop_loss",
    opened_offset: timedelta = timedelta(minutes=30),
) -> None:
    """Insert a synthetic closed trade with the given P&L."""
    opened = closed_at - opened_offset
    entry = Decimal("2.00")
    qty = 1
    exit_price = entry + gross_pnl
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO trades
                (signal_id, opened_at, closed_at, ticker, option_type, strike,
                 expiration, entry_price, exit_price, qty, gross_pnl, pnl_pct,
                 exit_reason, hold_minutes)
            VALUES (NULL, %s, %s, 'AAPL', 'call', 185, '2026-06-20', %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                opened, closed_at, entry, exit_price, qty,
                gross_pnl, pnl_pct, exit_reason,
                int(opened_offset.total_seconds() / 60),
            ),
        )
    conn.commit()


# ---- realized_pnl_today ----------------------------------------------------

def test_realized_pnl_today_sums_only_session_date(conn: psycopg.Connection) -> None:
    today = date(2026, 5, 12)
    # 3 trades today
    for pnl in (Decimal("0.50"), Decimal("-0.30"), Decimal("0.10")):
        _insert_trade(
            conn,
            closed_at=datetime(2026, 5, 12, 14, 0, tzinfo=timezone.utc),
            gross_pnl=pnl,
            pnl_pct=pnl / Decimal("2.00"),
        )
    # 1 trade yesterday — must not count
    _insert_trade(
        conn,
        closed_at=datetime(2026, 5, 11, 14, 0, tzinfo=timezone.utc),
        gross_pnl=Decimal("99.99"),
        pnl_pct=Decimal("0.50"),
    )
    assert realized_pnl_today(conn, today) == Decimal("0.30")


def test_realized_pnl_today_no_trades_returns_zero(conn: psycopg.Connection) -> None:
    assert realized_pnl_today(conn, date(2026, 5, 12)) == Decimal("0")


# ---- consecutive_loss_count ------------------------------------------------

def test_consecutive_loss_count_basic(conn: psycopg.Connection) -> None:
    """Most recent 4 are losses → count is 4."""
    base = datetime(2026, 5, 12, 14, 0, tzinfo=timezone.utc)
    # Insert oldest first so closed_at order matches loss/win sequence
    sequence: list[Decimal] = [
        Decimal("0.50"),     # oldest: winner
        Decimal("-0.20"),
        Decimal("-0.15"),
        Decimal("-0.10"),
        Decimal("-0.30"),    # newest: 4th loss
    ]
    for i, pnl in enumerate(sequence):
        _insert_trade(conn, closed_at=base + timedelta(minutes=i * 10), gross_pnl=pnl, pnl_pct=pnl / Decimal("2.00"))
    after_last = base + timedelta(hours=1)
    assert consecutive_loss_count(conn, before=after_last) == 4


def test_consecutive_loss_count_breaks_on_winner(conn: psycopg.Connection) -> None:
    """L L W L L L: from newest backward, count 3, then stop."""
    base = datetime(2026, 5, 12, 14, 0, tzinfo=timezone.utc)
    sequence = [
        Decimal("-0.10"),  # oldest
        Decimal("-0.20"),
        Decimal("0.30"),   # winner — breaks the streak
        Decimal("-0.10"),
        Decimal("-0.20"),
        Decimal("-0.05"),  # newest
    ]
    for i, pnl in enumerate(sequence):
        _insert_trade(conn, closed_at=base + timedelta(minutes=i * 10), gross_pnl=pnl, pnl_pct=pnl / Decimal("2.00"))
    assert consecutive_loss_count(conn, before=base + timedelta(hours=1)) == 3


def test_consecutive_loss_count_zero_when_last_was_winner(conn: psycopg.Connection) -> None:
    base = datetime(2026, 5, 12, 14, 0, tzinfo=timezone.utc)
    _insert_trade(conn, closed_at=base, gross_pnl=Decimal("-0.10"), pnl_pct=Decimal("-0.05"))
    _insert_trade(conn, closed_at=base + timedelta(minutes=5), gross_pnl=Decimal("0.20"), pnl_pct=Decimal("0.10"))
    assert consecutive_loss_count(conn, before=base + timedelta(hours=1)) == 0


def test_consecutive_loss_count_empty_returns_zero(conn: psycopg.Connection) -> None:
    assert consecutive_loss_count(conn, before=datetime(2026, 5, 12, 14, 0, tzinfo=timezone.utc)) == 0


def test_consecutive_loss_count_lookback_bounds(conn: psycopg.Connection) -> None:
    """Trades older than the lookback window must be ignored."""
    base = datetime(2026, 5, 12, 14, 0, tzinfo=timezone.utc)
    # Old loss outside lookback
    _insert_trade(conn, closed_at=base - timedelta(days=45), gross_pnl=Decimal("-0.10"), pnl_pct=Decimal("-0.05"))
    # Recent loss inside lookback
    _insert_trade(conn, closed_at=base - timedelta(days=1), gross_pnl=Decimal("-0.20"), pnl_pct=Decimal("-0.10"))
    assert consecutive_loss_count(conn, before=base, lookback_days=30) == 1


# ---- evaluate_and_log writes events ---------------------------------------

def test_evaluate_and_log_writes_pass_event(conn: psycopg.Connection) -> None:
    state = SessionState(
        starting_equity=Decimal("100000"),
        current_equity=Decimal("100000"),
        consecutive_losses=0,
        last_x_received_at=datetime(2026, 5, 12, 14, 30, tzinfo=timezone.utc) - timedelta(seconds=5),
        last_alpaca_ok_at=datetime(2026, 5, 12, 14, 30, tzinfo=timezone.utc) - timedelta(seconds=5),
        market_open=True,
    )
    decision = evaluate_and_log(
        conn, state, datetime(2026, 5, 12, 14, 30, tzinfo=timezone.utc),
        daily_loss_kill_pct=Decimal("0.03"),
        max_consecutive_losses=4,
    )
    assert decision.accepted is True

    with conn.cursor() as cur:
        cur.execute("SELECT severity, category, message, context FROM events ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
    assert row is not None
    severity, category, message, context = row
    assert severity == "info"
    assert category == "risk"
    assert message == "risk_check_passed"
    assert context["accepted"] is True
    assert context["tripped_switches"] == []


def test_evaluate_and_log_writes_critical_on_new_trip(conn: psycopg.Connection) -> None:
    state = SessionState(
        starting_equity=Decimal("100000"),
        current_equity=Decimal("90000"),  # -10% → daily_loss
        consecutive_losses=0,
        last_x_received_at=datetime(2026, 5, 12, 14, 30, tzinfo=timezone.utc) - timedelta(seconds=5),
        last_alpaca_ok_at=datetime(2026, 5, 12, 14, 30, tzinfo=timezone.utc) - timedelta(seconds=5),
        market_open=True,
    )
    decision = evaluate_and_log(
        conn, state, datetime(2026, 5, 12, 14, 30, tzinfo=timezone.utc),
        daily_loss_kill_pct=Decimal("0.03"),
        max_consecutive_losses=4,
        context={"trigger": "synthetic_test"},
    )
    assert decision.accepted is False
    assert "daily_loss" in decision.newly_tripped

    with conn.cursor() as cur:
        cur.execute("SELECT severity, message, context FROM events ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
    severity, message, context = row
    assert severity == "critical"
    assert message == "daily_loss"
    assert context["tripped_switches"] == ["daily_loss"]
    assert context["newly_tripped"] == ["daily_loss"]
    assert context["context"]["trigger"] == "synthetic_test"


def test_evaluate_and_log_writes_warning_when_switch_persists(conn: psycopg.Connection) -> None:
    """If a switch was already active and doesn't NEWLY trip, severity stays at warning."""
    state = SessionState(
        starting_equity=Decimal("100000"),
        current_equity=Decimal("100000"),
        consecutive_losses=0,
        last_x_received_at=datetime(2026, 5, 12, 14, 30, tzinfo=timezone.utc) - timedelta(seconds=5),
        last_alpaca_ok_at=datetime(2026, 5, 12, 14, 30, tzinfo=timezone.utc) - timedelta(seconds=5),
        market_open=True,
        active_switches=frozenset({"consecutive_losses"}),
    )
    decision = evaluate_and_log(
        conn, state, datetime(2026, 5, 12, 14, 30, tzinfo=timezone.utc),
        daily_loss_kill_pct=Decimal("0.03"),
        max_consecutive_losses=4,
    )
    assert decision.accepted is False
    assert decision.newly_tripped == ()  # nothing newly tripped

    with conn.cursor() as cur:
        cur.execute("SELECT severity, message FROM events ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
    assert row[0] == "warning"
    assert row[1] == "consecutive_losses"


# ---- Phase 5 acceptance gate 2 — synthetic-fills behavior end-to-end -------

def test_synthetic_fills_drive_consecutive_losses_kill_switch(conn: psycopg.Connection) -> None:
    """Insert 4 losing trades; consecutive_loss_count returns 4; risk_manager trips."""
    base = datetime(2026, 5, 12, 14, 0, tzinfo=timezone.utc)
    for i in range(4):
        _insert_trade(conn, closed_at=base + timedelta(minutes=i * 10), gross_pnl=Decimal("-0.10"), pnl_pct=Decimal("-0.05"))

    now = base + timedelta(hours=1)
    losses = consecutive_loss_count(conn, before=now)
    assert losses == 4

    state = SessionState(
        starting_equity=Decimal("100000"),
        current_equity=Decimal("99500"),
        consecutive_losses=losses,
        last_x_received_at=now - timedelta(seconds=5),
        last_alpaca_ok_at=now - timedelta(seconds=5),
        market_open=True,
    )
    decision = evaluate_and_log(
        conn, state, now,
        daily_loss_kill_pct=Decimal("0.03"),
        max_consecutive_losses=4,
    )
    assert decision.accepted is False
    assert "consecutive_losses" in decision.tripped_switches


def test_synthetic_fills_drive_daily_loss_kill_switch(conn: psycopg.Connection) -> None:
    """Insert a heavy losing day; realized_pnl_today shows it; risk_manager trips."""
    base = datetime(2026, 5, 12, 14, 0, tzinfo=timezone.utc)
    _insert_trade(conn, closed_at=base, gross_pnl=Decimal("-4000"), pnl_pct=Decimal("-0.40"))

    today = base.date()
    pnl = realized_pnl_today(conn, today)
    assert pnl == Decimal("-4000")

    starting = Decimal("100000")
    state = SessionState(
        starting_equity=starting,
        current_equity=starting + pnl,   # 96000 → -4% loss
        consecutive_losses=0,
        last_x_received_at=base - timedelta(seconds=5),
        last_alpaca_ok_at=base - timedelta(seconds=5),
        market_open=True,
    )
    decision = evaluate_and_log(
        conn, state, base + timedelta(seconds=1),
        daily_loss_kill_pct=Decimal("0.03"),
        max_consecutive_losses=4,
    )
    assert decision.accepted is False
    assert "daily_loss" in decision.tripped_switches
