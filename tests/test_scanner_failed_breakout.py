"""Tests for the failed-breakout scanner.

Detection logic is tested against hand-crafted bar sequences so the
expected event (or non-event) is unambiguous. The scanner runtime is
tested with an in-memory bar source + recorder — no network, no DB.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from x_alpaca_trading_bot.scanners.failed_breakout import (
    BARS_PER_RTH_SESSION,
    Bar,
    DataServiceBarSource,
    FailedBreakout,
    FailedBreakoutScanner,
    find_failed_breakout,
)

ET = ZoneInfo("America/New_York")


def _bar(hh: int, mm: int, close: float, *, volume: float = 1000.0, day: int = 6) -> Bar:
    """Helper: build a 5-min bar at the given ET wall clock for 2026-06-day."""
    ts_et = datetime(2026, 6, day, hh, mm, tzinfo=ET)
    return Bar(
        ts=ts_et.astimezone(timezone.utc),
        open=close,
        high=close,
        low=close,
        close=close,
        volume=volume,
    )


# ---- Pure detection -------------------------------------------------------

def test_no_bars_returns_none() -> None:
    assert find_failed_breakout("AAPL", [], prior_high=100.0) is None


def test_no_breakout_returns_none() -> None:
    """All bars close below the prior-day high — no breakout occurred."""
    bars = [
        _bar(9, 35, 99.0),
        _bar(9, 40, 99.5),
        _bar(9, 45, 99.8),
    ]
    assert find_failed_breakout("AAPL", bars, prior_high=100.0) is None


def test_breakout_that_holds_returns_none() -> None:
    """Breakout happened but never closed back below prior_high within the
    failure window — that's a SUCCESSFUL breakout, not our event."""
    bars = [
        _bar(9, 35, 99.5),    # below
        _bar(9, 40, 100.5),   # breakout
        _bar(9, 45, 101.0),   # holds above
        _bar(9, 50, 101.2),   # still above
        _bar(10, 0, 101.5),   # still above
        _bar(10, 40, 102.0),  # well past failure window
    ]
    assert find_failed_breakout("AAPL", bars, prior_high=100.0) is None


def test_classic_failed_breakout_detected() -> None:
    """Breakout above 100.0 followed by close back below — the canonical event."""
    bars = [
        _bar(9, 35, 99.5),    # below
        _bar(9, 40, 100.5),   # breakout (close > prior_high)
        _bar(9, 45, 100.2),   # still above
        _bar(9, 50, 99.0),    # FAILURE: close back below
        _bar(9, 55, 98.5),
    ]
    event = find_failed_breakout("AAPL", bars, prior_high=100.0)
    assert event is not None
    assert event.ticker == "AAPL"
    assert event.breakout_price == 100.5
    assert event.failure_price == 99.0
    assert event.prior_high == 100.0
    assert event.day == date(2026, 6, 6)


def test_breakout_before_earliest_window_ignored() -> None:
    """A breakout at 09:30 (before 09:35) does NOT count — match backtest."""
    bars = [
        _bar(9, 30, 100.5),   # breakout but too early
        _bar(9, 45, 99.0),    # would be failure if breakout counted
        _bar(9, 50, 98.0),
    ]
    assert find_failed_breakout("AAPL", bars, prior_high=100.0) is None


def test_breakout_at_or_after_cutoff_ignored() -> None:
    """A breakout at the default cutoff (10:30) or later does NOT count."""
    bars = [
        _bar(9, 35, 99.0),
        _bar(10, 30, 100.5),  # at cutoff — excluded
        _bar(10, 35, 99.0),
    ]
    assert find_failed_breakout("AAPL", bars, prior_high=100.0) is None


def test_breakout_just_before_cutoff_counts() -> None:
    """10:25 is inside the default 09:35-10:30 window."""
    bars = [
        _bar(9, 35, 99.0),
        _bar(10, 25, 100.5),  # breakout, 5 min before cutoff
        _bar(10, 35, 99.0),   # failure
    ]
    event = find_failed_breakout("AAPL", bars, prior_high=100.0)
    assert event is not None
    assert event.breakout_price == 100.5


def test_custom_cutoff_widens_window() -> None:
    """An explicit cutoff (e.g. the backtest's original 12:00) re-admits
    late-morning breakouts the tightened default rejects."""
    bars = [
        _bar(9, 35, 99.0),
        _bar(11, 0, 100.5),   # after default cutoff, before custom
        _bar(11, 10, 99.0),
    ]
    assert find_failed_breakout("AAPL", bars, prior_high=100.0) is None
    event = find_failed_breakout(
        "AAPL", bars, prior_high=100.0, breakout_cutoff=time(12, 0)
    )
    assert event is not None


def test_failure_outside_window_returns_none() -> None:
    """Breakout at 09:40, prior_high crossed back below at 11:00 — that's 80
    min later, outside the 60-min failure window. Should be treated as a
    holding breakout, not an event."""
    bars = [
        _bar(9, 35, 99.5),
        _bar(9, 40, 100.5),   # breakout
        _bar(10, 0, 101.0),   # holds
        _bar(10, 30, 101.5),  # still holds
        _bar(11, 0, 99.0),    # failure — but 80 min after breakout
    ]
    assert find_failed_breakout("AAPL", bars, prior_high=100.0) is None


def test_only_first_breakout_considered() -> None:
    """If price breaks out, fails, then breaks out again — we report the FIRST
    breakout's failure, not the second."""
    bars = [
        _bar(9, 35, 99.5),
        _bar(9, 40, 100.5),   # FIRST breakout
        _bar(9, 50, 99.0),    # FIRST failure — this is what we report
        _bar(10, 0, 100.7),   # second breakout — ignored
        _bar(10, 10, 98.5),   # second failure — ignored
    ]
    event = find_failed_breakout("AAPL", bars, prior_high=100.0)
    assert event is not None
    assert event.breakout_price == 100.5
    assert event.failure_price == 99.0


def test_volume_ratio_computed_when_baseline_provided() -> None:
    """volume_ratio = failure-bar volume / baseline."""
    bars = [
        _bar(9, 35, 99.5, volume=1000),
        _bar(9, 40, 100.5, volume=1000),
        _bar(9, 50, 99.0, volume=5000),  # failure with 5x volume
    ]
    event = find_failed_breakout("AAPL", bars, prior_high=100.0, baseline_volume=1000.0)
    assert event is not None
    assert event.volume_ratio == 5.0


def test_volume_ratio_none_when_baseline_zero_or_unset() -> None:
    bars = [
        _bar(9, 35, 99.5),
        _bar(9, 40, 100.5),
        _bar(9, 50, 99.0),
    ]
    event = find_failed_breakout("AAPL", bars, prior_high=100.0, baseline_volume=0.0)
    assert event is not None
    assert event.volume_ratio is None


def test_invalid_prior_high_returns_none() -> None:
    """Negative / zero prior_high means we don't have valid data to compare against."""
    bars = [_bar(9, 35, 100.5)]
    assert find_failed_breakout("AAPL", bars, prior_high=0.0) is None
    assert find_failed_breakout("AAPL", bars, prior_high=-1.0) is None


# ---- Scanner runtime ------------------------------------------------------

@dataclass
class _FakeBarSource:
    """In-memory bar source. Maps (ticker, day) -> bars + prior-day high."""

    bars_by_key: dict[tuple[str, date], list[Bar]] = field(default_factory=dict)
    high_by_key: dict[tuple[str, date], float | None] = field(default_factory=dict)
    baseline_by_key: dict[tuple[str, date], float | None] = field(default_factory=dict)
    raises_for: set[str] = field(default_factory=set)
    baseline_raises_for: set[str] = field(default_factory=set)

    def get_intraday_bars(self, ticker: str, day: date) -> list[Bar]:
        if ticker in self.raises_for:
            raise RuntimeError(f"network glitch for {ticker}")
        return self.bars_by_key.get((ticker, day), [])

    def get_prior_day_high(self, ticker: str, day: date) -> float | None:
        return self.high_by_key.get((ticker, day))

    def get_baseline_bar_volume(self, ticker: str, day: date) -> float | None:
        if ticker in self.baseline_raises_for:
            raise RuntimeError(f"baseline fetch failed for {ticker}")
        return self.baseline_by_key.get((ticker, day))


def _now_et(hh: int = 11, mm: int = 0, day: int = 6) -> datetime:
    """A UTC datetime corresponding to a wall-clock ET time on 2026-06-day."""
    return datetime(2026, 6, day, hh, mm, tzinfo=ET).astimezone(timezone.utc)


def _failed_bars() -> list[Bar]:
    return [
        _bar(9, 35, 99.5),
        _bar(9, 40, 100.5),
        _bar(9, 50, 99.0),
    ]


def test_scan_returns_new_event_when_recorder_accepts() -> None:
    src = _FakeBarSource(
        bars_by_key={("AAPL", date(2026, 6, 6)): _failed_bars()},
        high_by_key={("AAPL", date(2026, 6, 6)): 100.0},
    )

    recorded: list[FailedBreakout] = []

    def _recorder(name, event, now) -> bool:
        recorded.append(event)
        return True

    scanner = FailedBreakoutScanner(
        universe=["AAPL"], bar_source=src, record_event=_recorder
    )
    new_events = scanner.scan(_now_et())

    assert len(new_events) == 1
    assert new_events[0].ticker == "AAPL"
    assert len(recorded) == 1


def test_scan_drops_event_when_recorder_says_already_recorded() -> None:
    """When the DB UNIQUE conflicts (recorder returns False), the scanner
    treats the event as a duplicate and excludes it from the return value
    — even though detection still found it."""
    src = _FakeBarSource(
        bars_by_key={("AAPL", date(2026, 6, 6)): _failed_bars()},
        high_by_key={("AAPL", date(2026, 6, 6)): 100.0},
    )

    scanner = FailedBreakoutScanner(
        universe=["AAPL"],
        bar_source=src,
        record_event=lambda name, event, now: False,  # always "already recorded"
    )
    new_events = scanner.scan(_now_et())
    assert new_events == []


def test_scan_skips_tickers_with_no_bars_or_no_prior_high() -> None:
    src = _FakeBarSource(
        bars_by_key={("AAPL", date(2026, 6, 6)): _failed_bars()},
        # NOTE: TSLA missing entirely; MSFT has no prior_high.
        high_by_key={
            ("AAPL", date(2026, 6, 6)): 100.0,
            ("MSFT", date(2026, 6, 6)): None,
        },
    )

    recorded: list[FailedBreakout] = []
    scanner = FailedBreakoutScanner(
        universe=["AAPL", "MSFT", "TSLA"],
        bar_source=src,
        record_event=lambda n, e, t: (recorded.append(e), True)[1],
    )
    new_events = scanner.scan(_now_et())

    assert [e.ticker for e in new_events] == ["AAPL"]
    assert [e.ticker for e in recorded] == ["AAPL"]


def test_scan_continues_when_one_ticker_raises() -> None:
    """One ticker's bar fetch raising must not stop the scan of the rest."""
    src = _FakeBarSource(
        bars_by_key={
            ("AAPL", date(2026, 6, 6)): _failed_bars(),
            ("BAD", date(2026, 6, 6)): [],
        },
        high_by_key={
            ("AAPL", date(2026, 6, 6)): 100.0,
            ("BAD", date(2026, 6, 6)): 50.0,
        },
        raises_for={"BAD"},
    )

    scanner = FailedBreakoutScanner(
        universe=["BAD", "AAPL"],
        bar_source=src,
        record_event=lambda n, e, t: True,
    )
    new_events = scanner.scan(_now_et())
    assert [e.ticker for e in new_events] == ["AAPL"]


def test_scan_continues_when_recorder_raises() -> None:
    src = _FakeBarSource(
        bars_by_key={
            ("AAPL", date(2026, 6, 6)): _failed_bars(),
            ("MSFT", date(2026, 6, 6)): _failed_bars(),
        },
        high_by_key={
            ("AAPL", date(2026, 6, 6)): 100.0,
            ("MSFT", date(2026, 6, 6)): 100.0,
        },
    )

    fail_for = {"AAPL"}

    def _recorder(name, event, now) -> bool:
        if event.ticker in fail_for:
            raise RuntimeError("db error")
        return True

    scanner = FailedBreakoutScanner(
        universe=["AAPL", "MSFT"], bar_source=src, record_event=_recorder
    )
    new_events = scanner.scan(_now_et())
    # AAPL failed in the recorder; MSFT still made it through.
    assert [e.ticker for e in new_events] == ["MSFT"]


def test_scan_rejects_naive_now() -> None:
    src = _FakeBarSource()
    scanner = FailedBreakoutScanner(
        universe=["AAPL"],
        bar_source=src,
        record_event=lambda n, e, t: True,
    )
    with pytest.raises(ValueError):
        scanner.scan(datetime(2026, 6, 6, 11, 0))  # naive


def test_scanner_normalizes_universe_to_uppercase_and_strips() -> None:
    """Operators paste tickers from spreadsheets — be forgiving."""
    src = _FakeBarSource()
    scanner = FailedBreakoutScanner(
        universe=[" aapl ", "MSFT", "", "  tsla"],
        bar_source=src,
        record_event=lambda n, e, t: True,
    )
    assert scanner._universe == ("AAPL", "MSFT", "TSLA")


# ---- Per-ticker baseline volume -------------------------------------------

def _failed_bars_with_volume_spike() -> list[Bar]:
    return [
        _bar(9, 35, 99.5, volume=1000),
        _bar(9, 40, 100.5, volume=1000),
        _bar(9, 50, 99.0, volume=5000),  # failure bar, 5x volume
    ]


def test_scan_uses_per_ticker_baseline_from_bar_source() -> None:
    """Production path: no constructor baseline, so the scanner asks the
    bar source per ticker and the logged event carries volume_ratio."""
    day = date(2026, 6, 6)
    src = _FakeBarSource(
        bars_by_key={("AAPL", day): _failed_bars_with_volume_spike()},
        high_by_key={("AAPL", day): 100.0},
        baseline_by_key={("AAPL", day): 1000.0},
    )
    scanner = FailedBreakoutScanner(
        universe=["AAPL"], bar_source=src, record_event=lambda n, e, t: True
    )
    new_events = scanner.scan(_now_et())
    assert len(new_events) == 1
    assert new_events[0].volume_ratio == 5.0


def test_constructor_baseline_overrides_bar_source() -> None:
    day = date(2026, 6, 6)
    src = _FakeBarSource(
        bars_by_key={("AAPL", day): _failed_bars_with_volume_spike()},
        high_by_key={("AAPL", day): 100.0},
        baseline_by_key={("AAPL", day): 1000.0},  # would give 5.0
    )
    scanner = FailedBreakoutScanner(
        universe=["AAPL"],
        bar_source=src,
        record_event=lambda n, e, t: True,
        baseline_volume=2500.0,  # explicit override → 2.0
    )
    new_events = scanner.scan(_now_et())
    assert new_events[0].volume_ratio == 2.0


def test_baseline_fetch_failure_does_not_suppress_detection() -> None:
    """A broken baseline fetch degrades to volume_ratio=None — the event
    itself must still be detected and recorded."""
    day = date(2026, 6, 6)
    src = _FakeBarSource(
        bars_by_key={("AAPL", day): _failed_bars_with_volume_spike()},
        high_by_key={("AAPL", day): 100.0},
        baseline_raises_for={"AAPL"},
    )
    scanner = FailedBreakoutScanner(
        universe=["AAPL"], bar_source=src, record_event=lambda n, e, t: True
    )
    new_events = scanner.scan(_now_et())
    assert len(new_events) == 1
    assert new_events[0].volume_ratio is None


def test_scanner_respects_custom_breakout_cutoff() -> None:
    """An 11:00 breakout is rejected by the default 10:30 cutoff but
    admitted when the scanner is built with the old 12:00 cutoff."""
    day = date(2026, 6, 6)
    late_bars = [
        _bar(9, 35, 99.0),
        _bar(11, 0, 100.5),
        _bar(11, 10, 99.0),
    ]
    src = _FakeBarSource(
        bars_by_key={("AAPL", day): late_bars},
        high_by_key={("AAPL", day): 100.0},
    )

    default_scanner = FailedBreakoutScanner(
        universe=["AAPL"], bar_source=src, record_event=lambda n, e, t: True
    )
    assert default_scanner.scan(_now_et(hh=13)) == []

    wide_scanner = FailedBreakoutScanner(
        universe=["AAPL"],
        bar_source=src,
        record_event=lambda n, e, t: True,
        breakout_cutoff=time(12, 0),
    )
    assert len(wide_scanner.scan(_now_et(hh=13))) == 1


# ---- DataServiceBarSource baseline ----------------------------------------

@dataclass(frozen=True)
class _FakeDailyBar:
    """Duck-typed stand-in for data_service.OHLCBar."""
    ts: datetime
    high: float
    volume: int


class _FakeDataService:
    """Returns canned daily bars and counts fetches (for the cache test)."""

    def __init__(self, daily_bars: list[_FakeDailyBar]) -> None:
        self._daily = daily_bars
        self.daily_calls = 0

    def get_recent_daily_bars(self, ticker: str, now: datetime, *, limit: int):
        self.daily_calls += 1
        return self._daily[-limit:]


def _daily_bar(day: int, volume: int) -> _FakeDailyBar:
    ts_et = datetime(2026, 6, day, 16, 0, tzinfo=ET)
    return _FakeDailyBar(ts=ts_et.astimezone(timezone.utc), high=100.0, volume=volume)


def test_baseline_is_mean_daily_volume_over_78() -> None:
    """Baseline = mean daily volume of sessions BEFORE the target day,
    divided by the 78 5-min bars in an RTH session."""
    ds = _FakeDataService([
        _daily_bar(2, 780_000),
        _daily_bar(3, 1_560_000),
        _daily_bar(6, 9_999_999),  # target day itself — must be excluded
    ])
    src = DataServiceBarSource(ds)
    baseline = src.get_baseline_bar_volume("AAPL", date(2026, 6, 6))
    # mean(780k, 1.56M) / 78 = 1.17M / 78 = 15_000
    assert baseline == pytest.approx(1_170_000 / BARS_PER_RTH_SESSION)


def test_baseline_none_when_no_prior_days() -> None:
    ds = _FakeDataService([_daily_bar(6, 780_000)])  # only the target day
    src = DataServiceBarSource(ds)
    assert src.get_baseline_bar_volume("AAPL", date(2026, 6, 6)) is None


def test_baseline_cached_per_ticker_day() -> None:
    """Second lookup for the same (ticker, day) must not refetch — the
    scan loop calls this every 5 minutes."""
    ds = _FakeDataService([_daily_bar(2, 780_000), _daily_bar(3, 780_000)])
    src = DataServiceBarSource(ds)
    first = src.get_baseline_bar_volume("AAPL", date(2026, 6, 6))
    second = src.get_baseline_bar_volume("AAPL", date(2026, 6, 6))
    assert first == second
    assert ds.daily_calls == 1
