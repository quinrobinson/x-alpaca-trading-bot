"""Unit tests for the snapshot scheduler — Phase 7 acceptance gate.

Tests the pure in-memory scheduling logic. The DB-touching helpers
(capture_snapshot, close_trade) are exercised in
test_snapshot_integration.py against the local Postgres.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from x_alpaca_trading_bot.snapshot import (
    DEFAULT_INTERVAL,
    SnapshotContext,
    SnapshotScheduler,
    TrackedPosition,
)


NOW = datetime(2026, 5, 12, 14, 30, tzinfo=timezone.utc)


def _ctx(signal_id: int = 1, symbol: str = "AAPL260620C00185000", ticker: str = "AAPL") -> SnapshotContext:
    return SnapshotContext(signal_id=signal_id, contract_symbol=symbol, underlying_ticker=ticker)


# ---- Construction ----------------------------------------------------------

def test_scheduler_default_interval_is_15_minutes() -> None:
    s = SnapshotScheduler()
    assert s.interval == timedelta(minutes=15) == DEFAULT_INTERVAL


def test_scheduler_rejects_non_positive_interval() -> None:
    with pytest.raises(ValueError, match="positive"):
        SnapshotScheduler(interval=timedelta(0))


def test_scheduler_starts_empty() -> None:
    s = SnapshotScheduler()
    assert len(s) == 0
    assert s.positions_due(NOW) == []


# ---- Register / unregister -------------------------------------------------

def test_register_adds_to_tracked() -> None:
    s = SnapshotScheduler()
    s.register(_ctx(), opened_at=NOW)
    assert len(s) == 1
    p = s.get(1)
    assert isinstance(p, TrackedPosition)
    assert p.ctx.signal_id == 1
    assert p.opened_at == NOW
    assert p.last_snapshot_at == NOW
    assert p.max_gain_pct == Decimal(0)
    assert p.max_loss_pct == Decimal(0)


def test_register_rejects_naive_opened_at() -> None:
    s = SnapshotScheduler()
    with pytest.raises(ValueError, match="timezone-aware"):
        s.register(_ctx(), opened_at=datetime(2026, 5, 12, 14, 30))


def test_unregister_removes() -> None:
    s = SnapshotScheduler()
    s.register(_ctx(), opened_at=NOW)
    s.unregister(1)
    assert len(s) == 0
    assert s.get(1) is None


def test_unregister_missing_is_safe() -> None:
    s = SnapshotScheduler()
    s.unregister(999)  # no exception


# ---- positions_due ---------------------------------------------------------

def test_positions_due_before_interval_empty() -> None:
    s = SnapshotScheduler()
    s.register(_ctx(), opened_at=NOW)
    assert s.positions_due(NOW + timedelta(minutes=14)) == []


def test_positions_due_at_interval_returns_position() -> None:
    s = SnapshotScheduler()
    s.register(_ctx(), opened_at=NOW)
    due = s.positions_due(NOW + timedelta(minutes=15))
    assert len(due) == 1
    assert due[0].ctx.signal_id == 1


def test_positions_due_past_interval_returns_position() -> None:
    s = SnapshotScheduler()
    s.register(_ctx(), opened_at=NOW)
    due = s.positions_due(NOW + timedelta(minutes=30))
    assert len(due) == 1


def test_positions_due_handles_multiple_positions() -> None:
    s = SnapshotScheduler()
    s.register(_ctx(signal_id=1), opened_at=NOW)
    s.register(_ctx(signal_id=2, ticker="TSLA"), opened_at=NOW + timedelta(minutes=10))
    s.register(_ctx(signal_id=3, ticker="NVDA"), opened_at=NOW + timedelta(minutes=20))

    # At T+15m: only signal 1 is due (others opened too recently).
    due = s.positions_due(NOW + timedelta(minutes=15))
    assert {p.ctx.signal_id for p in due} == {1}

    # At T+30m: signals 1 and 2 due.
    due = s.positions_due(NOW + timedelta(minutes=30))
    assert {p.ctx.signal_id for p in due} == {1, 2}


def test_positions_due_rejects_naive_now() -> None:
    s = SnapshotScheduler()
    s.register(_ctx(), opened_at=NOW)
    with pytest.raises(ValueError, match="timezone-aware"):
        s.positions_due(datetime(2026, 5, 12, 15, 0))


# ---- mark_snapshotted -----------------------------------------------------

def test_mark_snapshotted_resets_cadence() -> None:
    s = SnapshotScheduler()
    s.register(_ctx(), opened_at=NOW)
    later = NOW + timedelta(minutes=15)
    s.mark_snapshotted(1, later)
    # Just-snapshotted — not due until another interval passes.
    assert s.positions_due(later + timedelta(minutes=14)) == []
    assert len(s.positions_due(later + timedelta(minutes=15))) == 1


def test_mark_snapshotted_missing_is_noop() -> None:
    s = SnapshotScheduler()
    s.mark_snapshotted(999, NOW)  # no exception
    assert len(s) == 0


# ---- update_extremes ------------------------------------------------------

def test_update_extremes_tracks_max_gain() -> None:
    s = SnapshotScheduler()
    s.register(_ctx(), opened_at=NOW)
    s.update_extremes(1, Decimal("0.15"))
    s.update_extremes(1, Decimal("0.25"))
    s.update_extremes(1, Decimal("0.10"))  # lower — shouldn't change max
    p = s.get(1)
    assert p is not None
    assert p.max_gain_pct == Decimal("0.25")


def test_update_extremes_tracks_max_loss() -> None:
    s = SnapshotScheduler()
    s.register(_ctx(), opened_at=NOW)
    s.update_extremes(1, Decimal("-0.05"))
    s.update_extremes(1, Decimal("-0.18"))
    s.update_extremes(1, Decimal("-0.10"))  # less negative
    p = s.get(1)
    assert p is not None
    assert p.max_loss_pct == Decimal("-0.18")


def test_update_extremes_both_directions() -> None:
    s = SnapshotScheduler()
    s.register(_ctx(), opened_at=NOW)
    # Swing up, then down
    s.update_extremes(1, Decimal("0.30"))
    s.update_extremes(1, Decimal("-0.12"))
    s.update_extremes(1, Decimal("0.05"))
    p = s.get(1)
    assert p is not None
    assert p.max_gain_pct == Decimal("0.30")
    assert p.max_loss_pct == Decimal("-0.12")


def test_update_extremes_missing_returns_none() -> None:
    s = SnapshotScheduler()
    result = s.update_extremes(999, Decimal("0.10"))
    assert result is None


# ---- Lifecycle integration: entry / monitor / monitor / exit cadence ----

def test_full_cadence_lifecycle() -> None:
    """Simulate: open at T0, monitor at T+15, T+30, T+45, then close."""
    s = SnapshotScheduler()
    s.register(_ctx(), opened_at=NOW)

    # Just-opened, no monitor due yet
    assert s.positions_due(NOW + timedelta(minutes=10)) == []

    # T+15 due
    t1 = NOW + timedelta(minutes=15)
    assert len(s.positions_due(t1)) == 1
    s.mark_snapshotted(1, t1)

    # T+30 due
    t2 = NOW + timedelta(minutes=30)
    assert len(s.positions_due(t2)) == 1
    s.mark_snapshotted(1, t2)

    # T+45 due
    t3 = NOW + timedelta(minutes=45)
    assert len(s.positions_due(t3)) == 1
    s.mark_snapshotted(1, t3)

    # T+50: nothing due (just marked)
    assert s.positions_due(t3 + timedelta(minutes=5)) == []

    # Position closed
    s.unregister(1)
    assert s.positions_due(NOW + timedelta(hours=2)) == []
