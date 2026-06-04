"""strategy — Phase 4.

Pure functional position management. Given a `Position` snapshot, the current
option price, and the current wall-clock time, returns an `EvaluationResult`
that contains the (possibly updated) position state and an optional exit
decision.

Design constraints from X_ALPACA_OPTIONS_HANDOFF.md §2.3 and §6:
  - No I/O. No imports from alpaca, tweepy, anthropic, psycopg, httpx, etc.
  - No `datetime.now()`. The caller passes `now` explicitly.
  - All money math uses `Decimal`. Never float.
  - Timezone-aware datetimes everywhere.

Trailing stop (continuous peak-trailing, activated at +5% gain):

    Peak Gain  | Stop Action
    -----------+--------------------------------------------
       < +5%   | initial stop (initial_stop_pct below entry)
      >= +5%   | trail 5% behind running peak, never below breakeven
      >= +40%  | trail tightens to 3% behind running peak

The stop is a continuous function of the position's PEAK price, not a
discrete ratchet at fixed gain thresholds. Once peak gain crosses the
activation threshold (+5%), the stop locks in as `peak * (1 - width)`
and ratchets up tick-by-tick as the peak rises. It never moves down.

Why continuous instead of a discrete table:
The previous implementation used (+20%/+30%/+40%/+60%) discrete steps
with the stop sitting at the last level between thresholds. INOD
(2026-06-04) peaked at +8.89% mid, never crossed the +20% activation,
never ratcheted, and the 15:55 ET flatten took it at -11% on the bid.
With continuous trail at 5%, that peak would have moved the stop to
~+3.9%, exiting on pullback instead of holding through the decay.

ratchet_level is preserved as an integer state for journal/dashboard
backward compat:
    0 = trail not active (initial stop in effect)
    1 = trail active (5% behind peak)
    2 = aggressive trail (3% behind peak)

Hard exits (spec §1.4), checked in priority order on each tick:
  1. stop_loss          — current_price <= stop_price (capital protection first)
  2. time_stop_1555     — at or past 15:55 ET (mandatory daily flatten)
  3. dte_close          — DTE <= dte_threshold_days (default 1)
  4. stale_no_movement  — open >= 4h and price near entry
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

ExitReason = Literal[
    "stop_loss",
    "time_stop_1555",
    "dte_close",
    "stale_no_movement",
]

ET = ZoneInfo("America/New_York")

# Continuous trail configuration. The stop locks in at
# `peak * (1 - width)` once peak_gain crosses the activation threshold.
#
# Two regimes: a standard 5% trail above the +5% activation, and a
# tighter 3% trail once peak_gain crosses +40% to protect big winners
# more aggressively.
#
# All Decimals — no float math anywhere in the strategy.
TRAIL_ACTIVATION_GAIN: Decimal = Decimal("0.05")    # activate at peak gain >= +5%
TRAIL_WIDTH: Decimal = Decimal("0.05")               # standard: 5% behind peak
TIGHT_TRAIL_GAIN: Decimal = Decimal("0.40")          # aggressive regime at peak gain >= +40%
TIGHT_TRAIL_WIDTH: Decimal = Decimal("0.03")         # aggressive: 3% behind peak

# Default tunables. Caller can override.
DEFAULT_MARKET_CLOSE = time(15, 55)              # ET wall-clock
DEFAULT_DTE_THRESHOLD_DAYS = 1
DEFAULT_STALE_WINDOW = timedelta(hours=4)
DEFAULT_STALE_MOVEMENT_PCT = Decimal("0.02")     # |Δprice|/entry below which we call it "no movement"


@dataclass(frozen=True)
class Position:
    """Snapshot of an open position. All money in Decimal."""

    entry_price: Decimal
    qty: int                       # positive (long-only)
    opened_at: datetime            # tz-aware (UTC or any tz)
    expiration: date
    initial_stop_pct: Decimal      # e.g. Decimal("0.20") for -20% initial stop
    stop_price: Decimal            # current trailing stop level (price)
    # Running maximum of `current_price` seen across all evaluate() calls.
    # The continuous trail anchors off this — stop = peak_price * (1 - width)
    # once activated. Initialized to entry_price by open_position(); never
    # drops, only rises tick-by-tick.
    peak_price: Decimal = Decimal(0)
    ratchet_level: int = 0
    # ratchet_level meaning under continuous trail:
    #   0 = trail not active (initial stop in effect)
    #   1 = standard trail active (5% behind peak)
    #   2 = aggressive trail active (3% behind peak, peak gain >= +40%)


@dataclass(frozen=True)
class ExitDecision:
    reason: ExitReason
    exit_price: Decimal
    triggered_at: datetime


@dataclass(frozen=True)
class EvaluationResult:
    position: Position                  # may be the same object if nothing changed
    exit: ExitDecision | None           # None means continue holding


def open_position(
    *,
    entry_price: Decimal,
    qty: int,
    opened_at: datetime,
    expiration: date,
    initial_stop_pct: Decimal,
) -> Position:
    """Construct a Position from fill data with the initial stop computed.

    initial_stop_pct is the FRACTION below entry, e.g. Decimal("0.20") for
    a 20% stop loss.
    """
    if qty <= 0:
        raise ValueError(f"qty must be positive (long-only); got {qty}")
    if entry_price <= 0:
        raise ValueError(f"entry_price must be positive; got {entry_price}")
    if not (Decimal(0) < initial_stop_pct < Decimal(1)):
        raise ValueError(f"initial_stop_pct must be in (0, 1); got {initial_stop_pct}")
    if opened_at.tzinfo is None:
        raise ValueError("opened_at must be timezone-aware")

    initial_stop = entry_price * (Decimal(1) - initial_stop_pct)
    return Position(
        entry_price=entry_price,
        qty=qty,
        opened_at=opened_at,
        expiration=expiration,
        initial_stop_pct=initial_stop_pct,
        stop_price=initial_stop,
        peak_price=entry_price,    # start tracking from entry
        ratchet_level=0,
    )


def evaluate(
    position: Position,
    current_price: Decimal,
    now: datetime,
    *,
    market_close_time: time = DEFAULT_MARKET_CLOSE,
    dte_threshold_days: int = DEFAULT_DTE_THRESHOLD_DAYS,
    stale_window: timedelta = DEFAULT_STALE_WINDOW,
    stale_movement_pct: Decimal = DEFAULT_STALE_MOVEMENT_PCT,
) -> EvaluationResult:
    """Advance position state and emit an exit if any hard rule fires.

    `now` and `market_close_time` are passed explicitly — strategy never calls
    `datetime.now()` (spec §2.3 rule 3).
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if current_price <= 0:
        raise ValueError(f"current_price must be positive; got {current_price}")

    # ---- Step 1: peak update + continuous trail ----------------------------
    # The stop is a continuous function of the running peak. We update
    # peak first (only ever upward), then derive the trail stop from it.
    peak_price = (
        current_price
        if current_price > position.peak_price
        else position.peak_price
    )
    peak_gain = (peak_price - position.entry_price) / position.entry_price

    # Pick the trail width by regime. Below activation, no trail —
    # the initial stop stays in force.
    if peak_gain >= TIGHT_TRAIL_GAIN:
        new_level = 2
        trail_stop = peak_price * (Decimal(1) - TIGHT_TRAIL_WIDTH)
    elif peak_gain >= TRAIL_ACTIVATION_GAIN:
        new_level = 1
        trail_stop = peak_price * (Decimal(1) - TRAIL_WIDTH)
    else:
        new_level = position.ratchet_level   # don't downgrade if already activated
        trail_stop = None

    # Apply the trail. Never below breakeven (once activated, the stop
    # locks in at least at entry). Never below the previous stop (stops
    # only move up). Never downgrade the ratchet_level either.
    if trail_stop is not None:
        candidate = trail_stop if trail_stop >= position.entry_price else position.entry_price
        new_stop = candidate if candidate > position.stop_price else position.stop_price
    else:
        new_stop = position.stop_price
    new_level = max(new_level, position.ratchet_level)

    if (
        new_stop == position.stop_price
        and new_level == position.ratchet_level
        and peak_price == position.peak_price
    ):
        new_position = position
    else:
        new_position = replace(
            position,
            stop_price=new_stop,
            peak_price=peak_price,
            ratchet_level=new_level,
        )

    # ---- Step 2: exit checks in priority order -----------------------------
    # 2a. Stop loss — capital protection first.
    if current_price <= new_stop:
        return EvaluationResult(
            position=new_position,
            exit=ExitDecision(reason="stop_loss", exit_price=current_price, triggered_at=now),
        )

    # 2b. 15:55 ET mandatory close.
    if _is_at_or_past_close(now, market_close_time):
        return EvaluationResult(
            position=new_position,
            exit=ExitDecision(reason="time_stop_1555", exit_price=current_price, triggered_at=now),
        )

    # 2c. DTE close.
    et_today = now.astimezone(ET).date()
    days_to_exp = (position.expiration - et_today).days
    if days_to_exp <= dte_threshold_days:
        return EvaluationResult(
            position=new_position,
            exit=ExitDecision(reason="dte_close", exit_price=current_price, triggered_at=now),
        )

    # 2d. Stale — open longer than window with no meaningful price movement.
    age = now - position.opened_at
    if age >= stale_window:
        change_pct = abs(current_price - position.entry_price) / position.entry_price
        if change_pct < stale_movement_pct:
            return EvaluationResult(
                position=new_position,
                exit=ExitDecision(reason="stale_no_movement", exit_price=current_price, triggered_at=now),
            )

    return EvaluationResult(position=new_position, exit=None)


# ---- Helpers ---------------------------------------------------------------

def _is_at_or_past_close(now: datetime, close_time: time) -> bool:
    """True iff `now` (any tz) is at or past `close_time` in ET on its ET date."""
    et = now.astimezone(ET)
    close_dt = datetime.combine(et.date(), close_time, tzinfo=ET)
    return et >= close_dt
