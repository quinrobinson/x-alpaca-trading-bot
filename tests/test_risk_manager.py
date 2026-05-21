"""Tests for risk_manager — Phase 5 acceptance gate.

evaluate() is a pure function — every test runs synchronously without mocks.
The DB-touching helpers (realized_pnl_today, consecutive_loss_count,
evaluate_and_log) are exercised in the integration suite that runs alongside
the local Postgres (tests/test_risk_manager_integration.py).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from x_alpaca_trading_bot.risk_manager import (
    DEFAULT_CONNECTION_STALL_SECONDS,
    KILL_SWITCH_NAMES,
    RiskDecision,
    SessionState,
    evaluate,
)


NOW = datetime(2026, 5, 12, 14, 30, tzinfo=timezone.utc)

DAILY_LOSS_KILL_PCT = Decimal("0.03")
MAX_CONSECUTIVE_LOSSES = 4


def _state(
    *,
    starting_equity: Decimal = Decimal("100000"),
    current_equity: Decimal = Decimal("100000"),
    consecutive_losses: int = 0,
    last_x_received_at: datetime | None = None,
    last_alpaca_ok_at: datetime | None = None,
    market_open: bool = True,
    active_switches: frozenset[str] = frozenset(),
    last_trade_closed_at: datetime | None = None,
) -> SessionState:
    # Default heartbeats to "just received" so the connection switches don't
    # trip incidentally in tests that aren't about connections.
    if last_x_received_at is None:
        last_x_received_at = NOW - timedelta(seconds=5)
    if last_alpaca_ok_at is None:
        last_alpaca_ok_at = NOW - timedelta(seconds=5)
    return SessionState(
        starting_equity=starting_equity,
        current_equity=current_equity,
        consecutive_losses=consecutive_losses,
        last_x_received_at=last_x_received_at,
        last_alpaca_ok_at=last_alpaca_ok_at,
        market_open=market_open,
        active_switches=active_switches,
        last_trade_closed_at=last_trade_closed_at,
    )


def _evaluate(state: SessionState) -> RiskDecision:
    return evaluate(
        state,
        NOW,
        daily_loss_kill_pct=DAILY_LOSS_KILL_PCT,
        max_consecutive_losses=MAX_CONSECUTIVE_LOSSES,
    )


# ---- Happy path ----

def test_clean_state_accepts() -> None:
    d = _evaluate(_state())
    assert d.accepted is True
    assert d.tripped_switches == ()
    assert d.newly_tripped == ()
    assert d.reason is None


# ---- Daily loss kill switch ----

def test_daily_loss_trips_at_threshold() -> None:
    # exactly -3% of $100k = $97k
    d = _evaluate(_state(current_equity=Decimal("97000")))
    assert d.accepted is False
    assert "daily_loss" in d.tripped_switches
    assert "daily_loss" in d.newly_tripped


def test_daily_loss_no_trip_below_threshold() -> None:
    # -2.99% — still under
    d = _evaluate(_state(current_equity=Decimal("97010")))
    assert d.accepted is True


def test_daily_loss_no_trip_when_in_profit() -> None:
    d = _evaluate(_state(current_equity=Decimal("105000")))
    assert d.accepted is True


def test_daily_loss_threshold_configurable() -> None:
    # With a 5% threshold, -3% should NOT trip.
    state = _state(current_equity=Decimal("97000"))
    d = evaluate(
        state, NOW,
        daily_loss_kill_pct=Decimal("0.05"),
        max_consecutive_losses=MAX_CONSECUTIVE_LOSSES,
    )
    assert d.accepted is True


# ---- Consecutive losses ----

def test_four_consecutive_losses_trips() -> None:
    d = _evaluate(_state(consecutive_losses=4))
    assert d.accepted is False
    assert "consecutive_losses" in d.tripped_switches
    assert "consecutive_losses" in d.newly_tripped


def test_three_consecutive_losses_no_trip() -> None:
    d = _evaluate(_state(consecutive_losses=3))
    assert d.accepted is True


def test_consecutive_threshold_configurable() -> None:
    state = _state(consecutive_losses=3)
    d = evaluate(
        state, NOW,
        daily_loss_kill_pct=DAILY_LOSS_KILL_PCT,
        max_consecutive_losses=3,
    )
    assert d.accepted is False
    assert "consecutive_losses" in d.tripped_switches


def test_consecutive_losses_trips_when_last_trade_is_recent() -> None:
    """A streak with the last trade inside the cooldown window stays tripped."""
    state = _state(
        consecutive_losses=4,
        last_trade_closed_at=NOW - timedelta(minutes=5),
    )
    d = _evaluate(state)
    assert d.accepted is False
    assert "consecutive_losses" in d.tripped_switches


def test_consecutive_losses_auto_clears_after_cooldown() -> None:
    """Once the cooldown elapses since the last trade, the switch clears
    on its own even though the loss count is still over the threshold."""
    state = _state(
        consecutive_losses=4,
        last_trade_closed_at=NOW - timedelta(minutes=45),  # past the 30-min cooldown
    )
    d = _evaluate(state)
    assert d.accepted is True
    assert "consecutive_losses" not in d.tripped_switches


def test_consecutive_loss_cooldown_is_configurable() -> None:
    """A caller can pass a custom cooldown window."""
    state = _state(
        consecutive_losses=4,
        last_trade_closed_at=NOW - timedelta(minutes=45),
    )
    # With a 90-minute cooldown, 45 minutes elapsed is NOT enough — still tripped.
    d = evaluate(
        state, NOW,
        daily_loss_kill_pct=DAILY_LOSS_KILL_PCT,
        max_consecutive_losses=MAX_CONSECUTIVE_LOSSES,
        consecutive_loss_cooldown=timedelta(minutes=90),
    )
    assert d.accepted is False
    assert "consecutive_losses" in d.tripped_switches


# ---- Connection switches ----

def test_x_stream_stalled_during_market_hours_trips() -> None:
    d = _evaluate(_state(last_x_received_at=NOW - timedelta(seconds=90)))
    assert d.accepted is False
    assert "x_stream_disconnected" in d.tripped_switches


def test_x_stream_never_connected_during_market_hours_trips() -> None:
    state = SessionState(
        starting_equity=Decimal("100000"),
        current_equity=Decimal("100000"),
        consecutive_losses=0,
        last_x_received_at=None,
        last_alpaca_ok_at=NOW - timedelta(seconds=5),
        market_open=True,
    )
    d = _evaluate(state)
    assert "x_stream_disconnected" in d.tripped_switches


def test_x_stream_stall_ignored_when_market_closed() -> None:
    d = _evaluate(_state(
        last_x_received_at=NOW - timedelta(hours=12),
        last_alpaca_ok_at=NOW - timedelta(hours=12),
        market_open=False,
    ))
    assert d.accepted is True


def test_alpaca_stalled_during_market_hours_trips() -> None:
    d = _evaluate(_state(last_alpaca_ok_at=NOW - timedelta(seconds=90)))
    assert "alpaca_disconnected" in d.tripped_switches


def test_connection_stall_threshold_configurable() -> None:
    # 30-second stall, switches set with 120s threshold — no trip.
    state = _state(last_x_received_at=NOW - timedelta(seconds=30))
    d = evaluate(
        state, NOW,
        daily_loss_kill_pct=DAILY_LOSS_KILL_PCT,
        max_consecutive_losses=MAX_CONSECUTIVE_LOSSES,
        connection_stall_seconds=120,
    )
    assert d.accepted is True


# ---- Active switches persist ----

def test_existing_active_switches_keep_decision_rejected() -> None:
    # Consecutive_losses already active — even with clean current state, stays tripped.
    d = _evaluate(_state(active_switches=frozenset({"consecutive_losses"})))
    assert d.accepted is False
    assert "consecutive_losses" in d.tripped_switches
    # Not 'newly tripped' though — it was already on.
    assert "consecutive_losses" not in d.newly_tripped


def test_active_plus_new_trip_both_surface() -> None:
    state = _state(
        active_switches=frozenset({"consecutive_losses"}),
        current_equity=Decimal("97000"),  # -3%
    )
    d = _evaluate(state)
    assert d.accepted is False
    assert set(d.tripped_switches) == {"consecutive_losses", "daily_loss"}
    assert d.newly_tripped == ("daily_loss",)


# ---- Multiple simultaneous trips ----

def test_multiple_simultaneous_trips_all_listed() -> None:
    state = _state(
        current_equity=Decimal("90000"),                            # -10% → daily_loss
        consecutive_losses=5,                                       # → consecutive_losses
        last_x_received_at=NOW - timedelta(seconds=90),             # → x_stream
    )
    d = _evaluate(state)
    assert d.accepted is False
    assert {"daily_loss", "consecutive_losses", "x_stream_disconnected"}.issubset(set(d.tripped_switches))


def test_reason_prefers_capital_protection() -> None:
    """When daily_loss and connection switches both fire, daily_loss wins as reason."""
    state = _state(
        current_equity=Decimal("90000"),
        last_x_received_at=NOW - timedelta(seconds=90),
    )
    d = _evaluate(state)
    assert d.reason == "daily_loss"


def test_reason_is_consecutive_losses_when_only_that() -> None:
    d = _evaluate(_state(consecutive_losses=4))
    assert d.reason == "consecutive_losses"


# ---- Input validation ----

def test_evaluate_rejects_naive_now() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        evaluate(
            _state(), datetime(2026, 5, 12, 14, 30),  # naive
            daily_loss_kill_pct=DAILY_LOSS_KILL_PCT,
            max_consecutive_losses=MAX_CONSECUTIVE_LOSSES,
        )


def test_evaluate_rejects_non_positive_starting_equity() -> None:
    state = _state(starting_equity=Decimal("0"))
    with pytest.raises(ValueError, match="starting_equity"):
        _evaluate(state)


# ---- Sanity on the kill-switch name list ----

def test_kill_switch_names_complete() -> None:
    assert set(KILL_SWITCH_NAMES) == {
        "daily_loss", "consecutive_losses",
        "x_stream_disconnected", "alpaca_disconnected",
    }
    assert DEFAULT_CONNECTION_STALL_SECONDS == 60
