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
    TIGHT_TRAIL_GAIN,
    TIGHT_TRAIL_WIDTH,
    TRAIL_ACTIVATION_GAIN,
    TRAIL_WIDTH,
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
    assert p.peak_price == Decimal("2.50")   # peak starts at entry
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


# ---- Continuous trail ------------------------------------------------------

def test_trail_inactive_below_5_pct_keeps_initial_stop() -> None:
    """Below the +5% activation threshold the initial -20% stop holds."""
    p = _new_position(entry=Decimal("2.50"))
    result = evaluate(p, Decimal("2.60"), NOW)   # +4%
    assert result.exit is None
    assert result.position.ratchet_level == 0
    assert result.position.stop_price == Decimal("2.00")  # initial -20% unchanged
    assert result.position.peak_price == Decimal("2.60")  # peak tracked even pre-activation


def test_trail_activates_at_5_pct_clamps_stop_to_breakeven() -> None:
    """At exactly +5% peak gain the trail activates. Raw trail_stop is
    peak * 0.95 = entry * 1.05 * 0.95 = entry * 0.9975 (below entry),
    so it clamps to breakeven."""
    p = _new_position(entry=Decimal("2.50"))
    result = evaluate(p, Decimal("2.625"), NOW)  # +5% exactly
    assert result.exit is None
    assert result.position.ratchet_level == 1
    assert result.position.stop_price == Decimal("2.50")   # clamped to breakeven
    assert result.position.peak_price == Decimal("2.625")


def test_trail_follows_peak_continuously() -> None:
    """Above +5%, stop = peak * 0.95 and ratchets up tick by tick."""
    p = _new_position(entry=Decimal("2.50"))

    # Tick 1: peak $2.875 (+15%). stop = 2.875 * 0.95 = 2.73125 (+9.25%)
    after_1 = evaluate(p, Decimal("2.875"), NOW).position
    assert after_1.ratchet_level == 1
    assert after_1.peak_price == Decimal("2.875")
    assert after_1.stop_price == Decimal("2.73125")

    # Tick 2: peak rises to $3.10 (+24%). stop = 3.10 * 0.95 = 2.945
    after_2 = evaluate(after_1, Decimal("3.10"), NOW + timedelta(minutes=1)).position
    assert after_2.peak_price == Decimal("3.10")
    assert after_2.stop_price == Decimal("2.945")


def test_trail_peak_persists_when_price_drops() -> None:
    """If the peak was higher than the current tick's price, peak stays
    and the stop stays. Only upward moves change anything."""
    p = _new_position(entry=Decimal("2.50"))
    high = evaluate(p, Decimal("3.00"), NOW).position             # peak +20%
    assert high.peak_price == Decimal("3.00")
    after_dip = evaluate(high, Decimal("2.85"), NOW + timedelta(minutes=1)).position
    assert after_dip.peak_price == Decimal("3.00")                 # peak unchanged
    assert after_dip.stop_price == high.stop_price                 # stop unchanged
    assert after_dip.ratchet_level == 1


def test_trail_stop_fires_when_pullback_crosses_it() -> None:
    """After a peak, a pullback below the trail stop triggers an exit."""
    p = _new_position(entry=Decimal("2.50"))
    # Peak at +20% -> stop at 3.00 * 0.95 = 2.85
    high = evaluate(p, Decimal("3.00"), NOW).position
    assert high.stop_price == Decimal("2.85")
    # Pullback to $2.80 — below the trail
    result = evaluate(high, Decimal("2.80"), NOW + timedelta(minutes=1))
    assert result.exit is not None
    assert result.exit.reason == "stop_loss"
    assert result.position.peak_price == Decimal("3.00")    # peak preserved


def test_aggressive_trail_activates_at_40_pct_peak() -> None:
    """At peak gain >= +40% the trail tightens from 5% to 3%."""
    p = _new_position(entry=Decimal("2.50"))
    # Peak at +40% exactly. trail_stop = 3.50 * 0.97 = 3.395 (+35.8%)
    result = evaluate(p, Decimal("3.50"), NOW)
    assert result.exit is None
    assert result.position.ratchet_level == 2
    assert result.position.peak_price == Decimal("3.50")
    assert result.position.stop_price == Decimal("3.395")


def test_aggressive_trail_ratchets_continuously() -> None:
    """Above the aggressive threshold, stop = peak * 0.97 and trails up."""
    p = _new_position(entry=Decimal("2.50"))
    # Tick 1: +50% peak -> stop = 3.75 * 0.97 = 3.6375
    after = evaluate(p, Decimal("3.75"), NOW).position
    assert after.ratchet_level == 2
    assert after.stop_price == Decimal("3.6375")
    # Tick 2: +60% peak -> stop = 4.00 * 0.97 = 3.88
    later = evaluate(after, Decimal("4.00"), NOW + timedelta(minutes=1)).position
    assert later.stop_price == Decimal("3.88")


def test_trail_never_moves_down_on_pullback() -> None:
    """Once raised, the stop holds even on big pullbacks (until it triggers)."""
    p = _new_position(entry=Decimal("2.50"))
    # Peak +40% -> stop at 3.395
    high = evaluate(p, Decimal("3.50"), NOW).position
    assert high.stop_price == Decimal("3.395")
    # Pullback to +20% (below stop) — stop fires, but stop_price stays
    result = evaluate(high, Decimal("3.00"), NOW + timedelta(minutes=1))
    assert result.position.stop_price == Decimal("3.395")
    assert result.exit is not None
    assert result.exit.reason == "stop_loss"


def test_ratchet_level_does_not_downgrade() -> None:
    """Once the aggressive regime is reached, dipping back below +40% on
    the current price (peak stays put) keeps ratchet_level at 2."""
    p = _new_position(entry=Decimal("2.50"))
    after_high = evaluate(p, Decimal("3.75"), NOW).position    # +50% -> level 2
    assert after_high.ratchet_level == 2
    # Price drops to +30%; peak stays at +50%, so level stays 2.
    result = evaluate(after_high, Decimal("3.25"), NOW + timedelta(minutes=1))
    assert result.position.ratchet_level == 2


def test_big_jump_straight_to_aggressive_regime() -> None:
    """One tick from entry directly to +50% lands in level 2 with 3% trail."""
    p = _new_position(entry=Decimal("2.50"))
    result = evaluate(p, Decimal("3.75"), NOW)
    assert result.exit is None
    assert result.position.ratchet_level == 2
    assert result.position.peak_price == Decimal("3.75")
    assert result.position.stop_price == Decimal("3.6375")    # 3.75 * 0.97


def test_inod_scenario_continuous_trail_protects_the_trade() -> None:
    """Regression for the INOD 2026-06-04 trade. Entry $1.80, peak $1.96
    (+8.89%), drift back to $1.72 by close. Discrete +20% table never
    fired and the trade closed at -11.1% via 15:55 flatten. Continuous
    trail should stop out near +3.4% on the pullback instead."""
    p = open_position(
        entry_price=Decimal("1.80"),
        qty=5,
        opened_at=NOW,
        expiration=date(2026, 6, 18),
        initial_stop_pct=Decimal("0.20"),
    )
    # Reach the historical peak. Trail activates at +5%; +8.89% > +5%.
    # stop = 1.96 * 0.95 = 1.862  (= entry * 1.0344 = +3.44%)
    after_peak = evaluate(p, Decimal("1.96"), NOW).position
    assert after_peak.ratchet_level == 1
    assert after_peak.peak_price == Decimal("1.96")
    assert after_peak.stop_price == Decimal("1.862")

    # Mid drifts back down. When it crosses below $1.862, the stop fires.
    result = evaluate(after_peak, Decimal("1.86"), NOW + timedelta(minutes=30))
    assert result.exit is not None
    assert result.exit.reason == "stop_loss"
    # Exit price reflects the current tick — the actual fill on Alpaca
    # will be at the live bid which can be lower, but the strategy's
    # decision is anchored to the mid here.
    assert result.exit.exit_price == Decimal("1.86")


# ---- Time-based: 15:55 ET (DTE-gated) -------------------------------------

# Default DEFAULT_EOD_DTE_THRESHOLD_DAYS = 3 — at 15:55, only flatten
# contracts with <= 3 days to expiration. Longer-DTE positions hold
# overnight, protected by the trailing stop.

def test_at_1555_et_with_near_expiry_flattens() -> None:
    """DTE = 3 contract at 15:55 → flatten (time_stop_1555)."""
    at_1555 = datetime(2026, 5, 12, 15, 55, tzinfo=ET)
    exp_3_days = at_1555.date() + timedelta(days=3)
    p = _new_position(expiration=exp_3_days)
    result = evaluate(p, Decimal("2.70"), at_1555.astimezone(timezone.utc))
    assert result.exit is not None
    assert result.exit.reason == "time_stop_1555"


def test_after_1555_et_with_near_expiry_flattens() -> None:
    at_1601 = datetime(2026, 5, 12, 16, 1, tzinfo=ET)
    exp_2_days = at_1601.date() + timedelta(days=2)
    p = _new_position(expiration=exp_2_days)
    result = evaluate(p, Decimal("2.70"), at_1601.astimezone(timezone.utc))
    assert result.exit is not None
    assert result.exit.reason == "time_stop_1555"


def test_at_1555_et_with_far_dte_holds_overnight() -> None:
    """DTE = 10 contract at 15:55 → NO exit. Position holds overnight,
    managed by the trailing stop. This is the user-approved 2026-06
    change to let real winners run instead of capping at close."""
    at_1555 = datetime(2026, 5, 12, 15, 55, tzinfo=ET)
    exp_10_days = at_1555.date() + timedelta(days=10)
    p = _new_position(expiration=exp_10_days)
    result = evaluate(p, Decimal("2.70"), at_1555.astimezone(timezone.utc))
    assert result.exit is None


def test_at_1555_et_with_dte_4_holds() -> None:
    """Boundary check: DTE=4 (one beyond the threshold) holds overnight."""
    at_1555 = datetime(2026, 5, 12, 15, 55, tzinfo=ET)
    exp_4_days = at_1555.date() + timedelta(days=4)
    p = _new_position(expiration=exp_4_days)
    result = evaluate(p, Decimal("2.70"), at_1555.astimezone(timezone.utc))
    assert result.exit is None


def test_eod_dte_threshold_configurable() -> None:
    """Caller can override the EOD DTE threshold per-tick."""
    at_1555 = datetime(2026, 5, 12, 15, 55, tzinfo=ET)
    exp_5_days = at_1555.date() + timedelta(days=5)
    p = _new_position(expiration=exp_5_days)
    # Default threshold = 3 → no exit
    assert evaluate(p, Decimal("2.70"), at_1555.astimezone(timezone.utc)).exit is None
    # Bump threshold to 7 → DTE=5 now flattens at 15:55
    result = evaluate(
        p, Decimal("2.70"), at_1555.astimezone(timezone.utc),
        eod_dte_threshold_days=7,
    )
    assert result.exit is not None
    assert result.exit.reason == "time_stop_1555"


def test_before_1555_et_no_time_stop_regardless_of_dte() -> None:
    """The 15:55 rule only fires AT or past 15:55. At 15:54 it's silent
    even on a same-day expiry."""
    at_1554 = datetime(2026, 5, 12, 15, 54, tzinfo=ET)
    same_day = at_1554.date()
    p = _new_position(expiration=same_day + timedelta(days=2))
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
    """Open 5h but price has moved beyond the stale threshold — not stale.
    With continuous trail active at +5%, the trail also kicks in here,
    but the test is specifically that stale_no_movement does NOT fire."""
    opened = NOW - timedelta(hours=5)
    p = _new_position(opened_at=opened, entry=Decimal("2.50"))
    result = evaluate(p, Decimal("2.625"), NOW)  # +5% — beyond 2% movement gate
    assert result.exit is None
    # Trail does activate at +5%; that's expected and not what this test guards.
    assert result.position.ratchet_level == 1


# ---- Priority ordering ----------------------------------------------------

def test_stop_loss_wins_over_time_stop_at_1555() -> None:
    """Stop loss takes priority even if 15:55 ET also fires.
    Uses a near-expiry contract so the time_stop rule is actually live;
    otherwise (DTE > 3) the time stop wouldn't fire under the new logic
    and the test wouldn't be testing priority at all."""
    at_1555 = datetime(2026, 5, 12, 15, 55, tzinfo=ET)
    exp_2_days = at_1555.date() + timedelta(days=2)
    p = _new_position(expiration=exp_2_days)
    # current price below stop AND at close time AND near expiry
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
    """Tick at a price below the current peak: no exit, no state change.
    Using a price <= entry (= initial peak) so peak_price doesn't update."""
    p = _new_position()
    result = evaluate(p, Decimal("2.45"), NOW)
    assert result.exit is None
    assert result.position == p  # unchanged


# ---- Trail invariants -----------------------------------------------------

def test_trail_constants_are_sensible() -> None:
    """Sanity bounds on the trail config so a future edit can't silently
    invert the relationship between standard and aggressive regimes."""
    # Activation must happen before the aggressive regime kicks in.
    assert TRAIL_ACTIVATION_GAIN < TIGHT_TRAIL_GAIN
    # Aggressive width must be tighter (smaller) than standard.
    assert TIGHT_TRAIL_WIDTH < TRAIL_WIDTH
    # Widths must be in (0, 1) — fractions of peak, not multiples.
    assert Decimal(0) < TRAIL_WIDTH < Decimal(1)
    assert Decimal(0) < TIGHT_TRAIL_WIDTH < Decimal(1)


def test_trail_stop_never_drops_across_ticks() -> None:
    """Property: across a sequence of ticks the stop is non-decreasing."""
    p = _new_position(entry=Decimal("2.50"))
    last_stop = p.stop_price
    # Walk through a noisy path: up, down, up further, down, up.
    for price in [Decimal(s) for s in ("2.60", "2.80", "2.65", "3.10", "2.95", "3.40", "3.20")]:
        p = evaluate(p, price, NOW).position
        assert p.stop_price >= last_stop
        last_stop = p.stop_price


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
