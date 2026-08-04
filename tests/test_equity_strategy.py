"""Unit tests for equity_strategy (Phase S2 pure logic).

House rule #6: full unit tests before this module is wired into main.py.
Everything here is pure — no network, no DB, no clocks.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from x_alpaca_trading_bot.equity_strategy import (
    EquityShortPosition,
    evaluate_entry,
    evaluate_exit,
    shares_for_notional,
    short_pnl,
    stop_price_for,
)

ET = ZoneInfo("America/New_York")


def _utc(hh: int, mm: int, *, day: int = 6) -> datetime:
    """UTC datetime for an ET wall-clock time on 2026-07-day."""
    return datetime(2026, 7, day, hh, mm, tzinfo=ET).astimezone(timezone.utc)


def _entry_kwargs(**overrides):
    """A baseline ACCEPTED entry; tests override one field at a time."""
    kw = dict(
        scanner_name="failed_breakout",
        volume_ratio=Decimal("1.5"),
        failure_ts=_utc(10, 0),
        now=_utc(10, 5),
        armed=True,
        open_position_count=0,
        ticker_already_open=False,
        max_concurrent=3,
        min_volume_ratio=Decimal("1.0"),
    )
    kw.update(overrides)
    return kw


# ---- evaluate_entry --------------------------------------------------------

def test_entry_accepted_on_validated_slice() -> None:
    d = evaluate_entry(**_entry_kwargs())
    assert d.accepted
    assert d.reason == "ok"


def test_entry_rejected_when_disarmed() -> None:
    d = evaluate_entry(**_entry_kwargs(armed=False))
    assert not d.accepted
    assert d.reason == "disarmed"


def test_entry_rejected_for_other_hypotheses() -> None:
    """Only failed_breakout is validated — lab hypotheses under
    observation must never trade."""
    d = evaluate_entry(**_entry_kwargs(scanner_name="vwap_reject"))
    assert d.reason == "unvalidated_hypothesis"


def test_entry_rejected_without_volume_ratio() -> None:
    d = evaluate_entry(**_entry_kwargs(volume_ratio=None))
    assert d.reason == "no_volume_ratio"


def test_entry_rejected_below_min_volume_ratio() -> None:
    d = evaluate_entry(**_entry_kwargs(volume_ratio=Decimal("0.99")))
    assert d.reason == "volume_ratio_below_min"


def test_entry_accepts_volume_ratio_exactly_at_min() -> None:
    d = evaluate_entry(**_entry_kwargs(volume_ratio=Decimal("1.0")))
    assert d.accepted


def test_entry_rejected_when_failure_at_or_after_1030() -> None:
    d = evaluate_entry(**_entry_kwargs(
        failure_ts=_utc(10, 30), now=_utc(10, 32),
    ))
    assert d.reason == "failure_after_cutoff"


def test_entry_accepts_failure_just_before_1030() -> None:
    d = evaluate_entry(**_entry_kwargs(
        failure_ts=_utc(10, 25), now=_utc(10, 28),
    ))
    assert d.accepted


def test_entry_rejected_when_stale() -> None:
    """Detected >10 minutes after the failure bar — the drift window is
    already partly spent."""
    d = evaluate_entry(**_entry_kwargs(
        failure_ts=_utc(10, 0), now=_utc(10, 11),
    ))
    assert d.reason == "stale_event"


def test_entry_rejected_when_ticker_already_open() -> None:
    d = evaluate_entry(**_entry_kwargs(ticker_already_open=True))
    assert d.reason == "ticker_already_open"


def test_entry_rejected_at_max_concurrent() -> None:
    d = evaluate_entry(**_entry_kwargs(open_position_count=3))
    assert d.reason == "max_concurrent_reached"


def test_entry_rejects_naive_datetimes() -> None:
    with pytest.raises(ValueError):
        evaluate_entry(**_entry_kwargs(now=datetime(2026, 7, 6, 10, 5)))


# ---- sizing / stop ---------------------------------------------------------

def test_shares_for_notional_floors() -> None:
    assert shares_for_notional(Decimal("1000"), Decimal("16.69")) == 59


def test_shares_for_notional_zero_when_price_exceeds_notional() -> None:
    """One share of a $1,500 stock doesn't fit a $1,000 notional — skip."""
    assert shares_for_notional(Decimal("1000"), Decimal("1500")) == 0


def test_shares_for_notional_zero_on_bad_price() -> None:
    assert shares_for_notional(Decimal("1000"), Decimal("0")) == 0
    assert shares_for_notional(Decimal("1000"), Decimal("-5")) == 0


def test_stop_price_is_one_pct_above_entry() -> None:
    assert stop_price_for(Decimal("100.00")) == Decimal("101.00")
    assert stop_price_for(Decimal("16.69")) == Decimal("16.86")  # 16.8569 → 16.86


# ---- evaluate_exit ---------------------------------------------------------

def _pos(*, entry_hh: int = 10, entry_mm: int = 0) -> EquityShortPosition:
    entry = Decimal("100.00")
    return EquityShortPosition(
        ticker="RIVN",
        qty=59,
        entry_price=entry,
        entry_time=_utc(entry_hh, entry_mm),
        stop_price=stop_price_for(entry),
    )


def test_exit_none_inside_hold_window() -> None:
    assert evaluate_exit(_pos(), _utc(10, 30)) is None


def test_exit_time_after_60_minutes() -> None:
    assert evaluate_exit(_pos(), _utc(11, 0)) == "time_exit"


def test_exit_eod_failsafe_beats_everything() -> None:
    pos = _pos(entry_hh=15, entry_mm=30)
    assert evaluate_exit(pos, _utc(15, 55), current_price=Decimal("150")) == "eod_failsafe"


def test_exit_stop_backup_when_price_through_stop() -> None:
    assert evaluate_exit(_pos(), _utc(10, 30), current_price=Decimal("101.00")) == "stop_backup"


def test_exit_no_stop_backup_below_stop() -> None:
    assert evaluate_exit(_pos(), _utc(10, 30), current_price=Decimal("100.99")) is None


def test_exit_rejects_naive_now() -> None:
    with pytest.raises(ValueError):
        evaluate_exit(_pos(), datetime(2026, 7, 6, 15, 0))


# ---- pnl -------------------------------------------------------------------

def test_short_pnl_gains_when_price_falls() -> None:
    gross, pct = short_pnl(Decimal("100.00"), Decimal("99.40"), 59)
    assert gross == Decimal("35.40")
    assert pct == Decimal("0.0060")


def test_short_pnl_loses_when_price_rises() -> None:
    gross, pct = short_pnl(Decimal("100.00"), Decimal("101.00"), 59)
    assert gross == Decimal("-59.00")
    assert pct == Decimal("-0.0100")
