"""Pure entry/exit logic for scanner-driven equity shorts (Phase S2).

SCANNER_PROGRAM.md is the spec. The validated failed-breakout slice
(volume_ratio >= 1.0, failure before 10:30 ET) is shorted in the
underlying equity — NOT options — because the measured edge (-0.60%
over 60 minutes) is real but too thin to survive options friction.

Same discipline as strategy.py:
  - No I/O, no network, no DB — the orchestrator owns all side effects.
  - No datetime.now(): time is always a parameter (house rule #3).
  - All money is Decimal (house rule #2).

Exit model (owner-confirmed 2026-08-04):
  - Hard time exit 60 minutes after entry — the scored edge decays to
    nothing by the close, so overstaying gives the move back.
  - Protective buy-stop resting at +1.0% above entry (shorts lose when
    price RISES). evaluate_exit also price-checks as a local backup in
    case the resting stop is missing.
  - 15:55 ET failsafe flatten — positions should never live that long;
    if one does, something upstream broke and we get flat.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# Entry gate — the validated slice.
ENTRY_FAILURE_CUTOFF_ET = time(10, 30)
# An event older than this at decision time has already spent part of
# its 60-minute drift window; entering late eats the edge.
MAX_ENTRY_DELAY = timedelta(minutes=10)

# Exit model.
EXIT_HOLD = timedelta(minutes=60)
STOP_GAIN_PCT = Decimal("0.01")      # buy-stop 1% above entry
EOD_FLATTEN_ET = time(15, 55)


@dataclass(frozen=True)
class EquityShortPosition:
    """One open scanner short. Immutable — the orchestrator replaces the
    whole record on state changes, same pattern as strategy.Position."""
    ticker: str
    qty: int                 # shares held short (positive number)
    entry_price: Decimal
    entry_time: datetime     # UTC, tz-aware
    stop_price: Decimal


@dataclass(frozen=True)
class EntryDecision:
    accepted: bool
    reason: str              # 'ok' when accepted; rejection cause otherwise


def _et_wall(ts: datetime) -> time:
    return ts.astimezone(ET).timetz().replace(tzinfo=None)


def evaluate_entry(
    *,
    scanner_name: str,
    volume_ratio: Decimal | None,
    failure_ts: datetime,
    now: datetime,
    armed: bool,
    open_position_count: int,
    ticker_already_open: bool,
    max_concurrent: int,
    min_volume_ratio: Decimal,
) -> EntryDecision:
    """Gate one scanner event against the S2 entry rules.

    Ordered so the cheapest/most-common rejections come first and each
    event gets exactly one reason. Every input is explicit — nothing is
    read from config or clocks here.
    """
    if now.tzinfo is None or failure_ts.tzinfo is None:
        raise ValueError("now and failure_ts must be timezone-aware")

    if not armed:
        return EntryDecision(False, "disarmed")
    if scanner_name != "failed_breakout":
        return EntryDecision(False, "unvalidated_hypothesis")
    if volume_ratio is None:
        return EntryDecision(False, "no_volume_ratio")
    if volume_ratio < min_volume_ratio:
        return EntryDecision(False, "volume_ratio_below_min")
    if _et_wall(failure_ts) >= ENTRY_FAILURE_CUTOFF_ET:
        return EntryDecision(False, "failure_after_cutoff")
    if now - failure_ts > MAX_ENTRY_DELAY:
        return EntryDecision(False, "stale_event")
    if ticker_already_open:
        return EntryDecision(False, "ticker_already_open")
    if open_position_count >= max_concurrent:
        return EntryDecision(False, "max_concurrent_reached")
    return EntryDecision(True, "ok")


def shares_for_notional(notional: Decimal, price: Decimal) -> int:
    """Whole shares for a fixed dollar notional. 0 when price is invalid
    or a single share exceeds the notional — the caller skips the trade."""
    if price <= 0 or notional <= 0:
        return 0
    return int((notional / price).to_integral_value(rounding=ROUND_DOWN))


def stop_price_for(entry_price: Decimal) -> Decimal:
    """Protective buy-stop level: +1% above the short entry, cents."""
    return (entry_price * (1 + STOP_GAIN_PCT)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


def evaluate_exit(
    position: EquityShortPosition,
    now: datetime,
    current_price: Decimal | None = None,
) -> str | None:
    """Return an exit reason or None to keep holding.

    Precedence: eod_failsafe > time_exit > stop_backup. The stop check
    is a BACKUP — the protective buy-stop rests on Alpaca's book and the
    orchestrator polls its fill status; this catches the pathological
    case where the resting order is missing and price has run through
    the stop level.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    if _et_wall(now) >= EOD_FLATTEN_ET:
        return "eod_failsafe"
    if now >= position.entry_time + EXIT_HOLD:
        return "time_exit"
    if current_price is not None and current_price >= position.stop_price:
        return "stop_backup"
    return None


def short_pnl(
    entry_price: Decimal, exit_price: Decimal, qty: int
) -> tuple[Decimal, Decimal]:
    """(gross_pnl, pnl_pct) for a short: profit when exit < entry."""
    gross = ((entry_price - exit_price) * qty).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    pct = (
        ((entry_price - exit_price) / entry_price).quantize(
            Decimal("0.0001"), rounding=ROUND_HALF_UP
        )
        if entry_price > 0 else Decimal("0")
    )
    return gross, pct
