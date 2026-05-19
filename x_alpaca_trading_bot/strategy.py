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

Trailing stop ratchet (revised for options microstructure):

    Position Gain | Stop Loss Action
    --------------+------------------
        +20%      | Move stop to breakeven
        +30%      | Move stop to +10%
        +40%      | Move stop to +20%
        +60%+     | Tighten to +30%, reassess

The stop only moves up. Once a ratchet level is reached, the position's
`ratchet_level` is recorded and the stop is raised to the new floor.

Why the higher triggers vs the original spec (+10/+20/+25/+40):
Options have wide bid/ask spreads. The original first ratchet (+10% →
breakeven) was triggered by normal intraday noise, then when the price
mean-reverted, the subsequent _close_position market sell took the bid,
turning a "protected breakeven" into a real -10% to -12% exit. Doubling
the first trigger to +20% gives the trade room to confirm a real trend
before we lock anything in.

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

# (trigger gain multiplier, new stop multiplier, ratchet level)
# E.g. (1.20, 1.00, 1) means: at +20% gain, raise stop to entry (breakeven) and
# move ratchet_level to 1. Subsequent ticks above +20% don't change anything
# until the next threshold (+30%) is crossed.
#
# Each level keeps a ~20pp buffer between the trigger and the new stop —
# giving options room to breathe through normal intraday noise before
# we lock in a level. The previous table used +10%/+20%/+25%/+40% which
# tripped on routine wiggles and bid/ask slippage turned breakeven exits
# into real losses; see docstring above.
RATCHET_TABLE: tuple[tuple[Decimal, Decimal, int], ...] = (
    (Decimal("1.20"), Decimal("1.00"), 1),  # +20% → stop to breakeven
    (Decimal("1.30"), Decimal("1.10"), 2),  # +30% → stop to +10%
    (Decimal("1.40"), Decimal("1.20"), 3),  # +40% → stop to +20%
    (Decimal("1.60"), Decimal("1.30"), 4),  # +60% → stop to +30%
)

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
    ratchet_level: int = 0         # 0 = initial, 1..4 = ratchet thresholds crossed


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

    # ---- Step 1: ratchet update --------------------------------------------
    new_stop = position.stop_price
    new_level = position.ratchet_level
    for trigger_mul, stop_mul, level in RATCHET_TABLE:
        threshold_price = position.entry_price * trigger_mul
        if current_price >= threshold_price and level > new_level:
            new_level = level
            candidate = position.entry_price * stop_mul
            if candidate > new_stop:
                new_stop = candidate

    new_position = (
        position
        if new_stop == position.stop_price and new_level == position.ratchet_level
        else replace(position, stop_price=new_stop, ratchet_level=new_level)
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
