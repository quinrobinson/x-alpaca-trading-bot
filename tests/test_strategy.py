"""Tests for strategy — Phase 4 acceptance gate.

Strategy is a pure function; every test runs synchronously with synthetic
inputs. No mocks needed because there's no I/O to mock.
"""

from __future__ import annotations

import inspect
import sys
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from x_alpaca_trading_bot import strategy
from x_alpaca_trading_bot.strategy import (
    DEFAULT_MARKET_CLOSE,
    ET,
    RATCHET_TABLE,
    EvaluationResult,
    ExitDecision,
    Position,
    evaluate,
    open_position,
)


# Fixed reference: noon ET on a non-expiry weekday, well before close.
NOW_ET_NOON = datetime(2026, 5, 12, 12, 0, tzinfo=ET)
NOW = NOW_ET_NOON.astimezone(timezone.utc)
EXPIRATION = date(2026, 6, 20)


def _new_position(
    *,
    entry: Decimal = Decimal("2.50"),
    stop_pct: Decimal = Decimal("0.20"),
    opened_at: datetime = NOW - timedelta(minutes=5),
    expiration: date = EXPIRATION,
) -> Position:
    return open_position(
        entry_price=entry,
        qty=1,
        opened_at=opened_at,
        expiration=expiration,
        initial_stop_pct=stop_pct,
    )


# ---- Position construction ------------------------------------------------

def test_open_position_sets_initial_stop_at_minus_20() -> None:
    p = _new_position(entry=Decimal("2.50"), stop_pct=Decimal("0.20"))
    assert p.entry_price == Decimal("2.50")
    assert p.stop_price == Decimal("2.00")
    assert p.ratchet_level == 0


def test_open_position_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        open_position(
            entry_price=Decimal("2.50"),
            qty=1,
            opened_at=datetime(2026, 5, 12, 12, 0),  # naive
            expiration=EXPIRATION,
            initial_stop_pct=Decimal("0.20"),
        )


def test_open_position_rejects_nonpositive_qty() -> None:
    with pytest.raises(ValueError, match="qty"):
        open_position(
            entry_price=Decimal("2.50"),
            qty=0,
            opened_at=NOW,
            expiration=EXPIRATION,
            initial_stop_pct=Decimal("0.20"),
        )


def test_open_position_rejects_invalid_stop_pct() -> None:
    for bad in (Decimal("0"), Decimal("1"), Decimal("1.5"), Decimal("-0.1")):
        with pytest.raises(ValueError):
            open_position(
                entry_price=Decimal("2.50"),
                qty=1,
                opened_at=NOW,
                expiration=EXPIRATION,
                initial_stop_pct=bad,
            )


# ---- Initial stop loss -----------------------------------------------------

def test_stop_loss_triggers_at_minus_20() -> None:
    p = _new_position()
    result = evaluate(p, Decimal("2.00"), NOW)  # at stop
    assert result.exit is not None
    assert result.exit.reason == "stop_loss"
    assert result.exit.exit_price == Decimal("2.00")


def test_stop_loss_triggers_below_stop() -> None:
    p = _new_position()
    result = evaluate(p, Decimal("1.95"), NOW)  # below stop
    assert result.exit is not None
    assert result.exit.reason == "stop_loss"


def test_no_exit_above_initial_stop() -> None:
    p = _new_position()
    result = evaluate(p, Decimal("2.45"), NOW)  # just above stop
    assert result.exit is None
    assert result.position.stop_price == Decimal("2.00")  # unchanged
    assert result.position.ratchet_level == 0


# ---- Ratchet ratchet ratchet ---------------------------------------------

def test_ratchet_level_1_at_plus_20_moves_stop_to_breakeven() -> None:
    p = _new_position(entry=Decimal("2.50"))
    result = evaluate(p, Decimal("3.00"), NOW)  # +20%
    assert result.exit is None
    assert result.position.ratchet_level == 1
    assert result.position.stop_price == Decimal("2.50")  # breakeven


def test_ratchet_level_2_at_plus_30_moves_stop_to_plus_10() -> None:
    p = _new_position(entry=Decimal("2.50"))
    result = evaluate(p, Decimal("3.25"), NOW)  # +30%
    assert result.exit is None
    assert result.position.ratchet_level == 2
    assert result.position.stop_price == Decimal("2.75")  # entry * 1.10


def test_ratchet_level_3_at_plus_40_moves_stop_to_plus_20() -> None:
    p = _new_position(entry=Decimal("2.50"))
    result = evaluate(p, Decimal("3.50"), NOW)  # +40%
    assert result.exit is None
    assert result.position.ratchet_level == 3
    assert result.position.stop_price == Decimal("3.00")  # entry * 1.20


def test_ratchet_level_4_at_plus_60_moves_stop_to_plus_30() -> None:
    p = _new_position(entry=Decimal("2.50"))
    result = evaluate(p, Decimal("4.00"), NOW)  # +60%
    assert result.exit is None
    assert result.position.ratchet_level == 4
    assert result.position.stop_price == Decimal("3.25")  # entry * 1.30


def test_ratchet_below_first_trigger_does_not_move_stop() -> None:
    """The first trigger is +20% now (was +10%). A +15% move shouldn't ratchet."""
    p = _new_position(entry=Decimal("2.50"))
    result = evaluate(p, Decimal("2.875"), NOW)  # +15%
    assert result.exit is None
    assert result.position.ratchet_level == 0  # no ratchet yet
    assert result.position.stop_price == Decimal("2.00")  # initial stop unchanged


def test_ratchet_skips_intermediate_levels_on_big_jump() -> None:
    """One tick from entry directly to +70% — must land at level 4."""
    p = _new_position(entry=Decimal("2.50"))
    result = evaluate(p, Decimal("4.25"), NOW)  # +70%, past +60%
    assert result.exit is None
    assert result.position.ratchet_level == 4
    assert result.position.stop_price == Decimal("3.25")


def test_stop_never_moves_down_after_pullback() -> None:
    """Once ratcheted up, the stop stays high even if price pulls back."""
    p = _new_position(entry=Decimal("2.50"))
    # First tick: +40%, stop ratchets to +20% (=$3.00)
    after_high = evaluate(p, Decimal("3.50"), NOW).position
    assert after_high.stop_price == Decimal("3.00")
    assert after_high.ratchet_level == 3

    # Second tick: price pulls back to +5% — stop must stay at $3.00,
    # which means the position should now exit on stop_loss because
    # current_price ($2.625) < stop ($3.00).
    result = evaluate(after_high, Decimal("2.625"), NOW + timedelta(minutes=1))
    assert result.exit is not None
    assert result.exit.reason == "stop_loss"
    assert result.position.stop_price == Decimal("3.00")  # didn't drop


def test_ratchet_does_not_downgrade_existing_state() -> None:
    """Re-evaluating at a level we've already crossed shouldn't change state."""
    p = _new_position(entry=Decimal("2.50"))
    after_4 = evaluate(p, Decimal("4.00"), NOW).position  # +60% lands at level 4
    assert after_4.ratchet_level == 4

    # Price drops to +30% — ratchet shouldn't go DOWN to level 2.
    result = evaluate(after_4, Decimal("3.25"), NOW + timedelta(minutes=1))
    # Position would stop out (current $3.25 ≈ stop $3.25), but the state
    # update happens BEFORE the stop check, so ratchet_level remains 4.
    assert result.position.ratchet_level == 4
    assert result.position.stop_price == Decimal("3.25")
    assert result.exit is not None
    assert result.exit.reason == "stop_loss"


# ---- Time-based: 15:55 ET --------------------------------------------------

def test_at_1555_et_exits_with_time_stop() -> None:
    p = _new_position()
    at_1555 = datetime(2026, 5, 12, 15, 55, tzinfo=ET)
    result = evaluate(p, Decimal("2.70"), at_1555.astimezone(timezone.utc))
    assert result.exit is not None
    assert result.exit.reason == "time_stop_1555"


def test_after_1555_et_exits_with_time_stop() -> None:
    p = _new_position()
    at_1601 = datetime(2026, 5, 12, 16, 1, tzinfo=ET)
    result = evaluate(p, Decimal("2.70"), at_1601.astimezone(timezone.utc))
    assert result.exit is not None
    assert result.exit.reason == "time_stop_1555"


def test_before_1555_et_no_time_stop() -> None:
    p = _new_position()
    at_1554 = datetime(2026, 5, 12, 15, 54, tzinfo=ET)
    result = evaluate(p, Decimal("2.70"), at_1554.astimezone(timezone.utc))
    assert result.exit is None


# ---- DTE-based exit --------------------------------------------------------

def test_dte_1_exits() -> None:
    """Expires tomorrow → exit."""
    tomorrow = NOW_ET_NOON.date() + timedelta(days=1)
    p = _new_position(expiration=tomorrow)
    result = evaluate(p, Decimal("2.70"), NOW)
    assert result.exit is not None
    assert result.exit.reason == "dte_close"


def test_dte_0_exits() -> None:
    """Expires today → exit."""
    today = NOW_ET_NOON.date()
    p = _new_position(expiration=today)
    result = evaluate(p, Decimal("2.70"), NOW)
    assert result.exit is not None
    assert result.exit.reason == "dte_close"


def test_dte_2_does_not_exit() -> None:
    in_two_days = NOW_ET_NOON.date() + timedelta(days=2)
    p = _new_position(expiration=in_two_days)
    result = evaluate(p, Decimal("2.70"), NOW)
    assert result.exit is None


def test_dte_threshold_configurable() -> None:
    """If caller bumps the threshold to 3 days, expirations within 3 close."""
    in_three_days = NOW_ET_NOON.date() + timedelta(days=3)
    p = _new_position(expiration=in_three_days)
    result = evaluate(p, Decimal("2.70"), NOW, dte_threshold_days=3)
    assert result.exit is not None
    assert result.exit.reason == "dte_close"


# ---- Stale ----------------------------------------------------------------

def test_stale_4h_flat_exits() -> None:
    """Open 4h+, price flat (within 2% of entry) → exit."""
    opened = NOW - timedelta(hours=5)
    p = _new_position(opened_at=opened, entry=Decimal("2.50"))
    result = evaluate(p, Decimal("2.51"), NOW)  # +0.4% — definitely flat
    assert result.exit is not None
    assert result.exit.reason == "stale_no_movement"


def test_stale_below_4h_does_not_exit() -> None:
    opened = NOW - timedelta(hours=3)
    p = _new_position(opened_at=opened, entry=Decimal("2.50"))
    result = evaluate(p, Decimal("2.51"), NOW)
    assert result.exit is None


def test_stale_with_movement_does_not_exit() -> None:
    """Open 5h but price has moved +5% — not stale."""
    opened = NOW - timedelta(hours=5)
    p = _new_position(opened_at=opened, entry=Decimal("2.50"))
    result = evaluate(p, Decimal("2.625"), NOW)  # +5%
    # The ratchet won't fire (need +10%), but stale shouldn't either.
    assert result.exit is None
    assert result.position.ratchet_level == 0


# ---- Priority ordering ----------------------------------------------------

def test_stop_loss_wins_over_time_stop_at_1555() -> None:
    """Stop loss takes priority even if 15:55 ET also fires."""
    p = _new_position()
    at_1555 = datetime(2026, 5, 12, 15, 55, tzinfo=ET)
    # current price below stop AND at close time
    result = evaluate(p, Decimal("1.95"), at_1555.astimezone(timezone.utc))
    assert result.exit is not None
    assert result.exit.reason == "stop_loss"


def test_time_stop_beats_dte() -> None:
    """If 15:55 fires AND DTE=0, the time stop wins (it's checked first)."""
    today = NOW_ET_NOON.date()
    p = _new_position(expiration=today)
    at_1555 = datetime(2026, 5, 12, 15, 55, tzinfo=ET)
    result = evaluate(p, Decimal("2.70"), at_1555.astimezone(timezone.utc))
    assert result.exit is not None
    assert result.exit.reason == "time_stop_1555"


# ---- Clean hold -----------------------------------------------------------

def test_clean_hold_returns_no_exit() -> None:
    p = _new_position()
    result = evaluate(p, Decimal("2.55"), NOW)
    assert result.exit is None
    assert result.position == p  # unchanged


# ---- Ratchet table sanity -------------------------------------------------

def test_ratchet_table_is_monotonic() -> None:
    """Triggers and new stops must both increase monotonically."""
    for i in range(1, len(RATCHET_TABLE)):
        trig_prev, stop_prev, level_prev = RATCHET_TABLE[i - 1]
        trig_curr, stop_curr, level_curr = RATCHET_TABLE[i]
        assert trig_curr > trig_prev
        assert stop_curr > stop_prev
        assert level_curr > level_prev


def test_default_market_close_is_1555() -> None:
    assert DEFAULT_MARKET_CLOSE == time(15, 55)


# ---- Isolation meta-test: zero I/O imports ---------------------------------

FORBIDDEN_IMPORT_ROOTS = {
    "alpaca", "tweepy", "anthropic", "psycopg", "httpx", "fastapi",
    "uvicorn", "websockets", "pandas", "pandas_ta", "pandas_ta_classic",
}


def test_strategy_module_has_zero_io_imports() -> None:
    """Phase 4 acceptance: strategy must depend on nothing but stdlib.

    Parses the module's AST to find every `import X` and `from X import ...`
    statement; fails if any root package is in FORBIDDEN_IMPORT_ROOTS.
    """
    import ast

    source = inspect.getsource(strategy)
    tree = ast.parse(source)
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported_roots.add(node.module.split(".")[0])

    forbidden_hit = imported_roots & FORBIDDEN_IMPORT_ROOTS
    assert not forbidden_hit, (
        f"strategy.py imports forbidden modules: {sorted(forbidden_hit)}"
    )

    # Also confirm at runtime: walk the module's namespace.
    for name in dir(strategy):
        obj = getattr(strategy, name)
        module_name = getattr(obj, "__module__", None) or ""
        for forbidden in FORBIDDEN_IMPORT_ROOTS:
            assert not module_name.startswith(forbidden), (
                f"strategy exposes {name} from forbidden module {module_name}"
            )
