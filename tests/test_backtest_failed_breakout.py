"""Tests for the failed-breakout backtester's pure analysis functions."""

from __future__ import annotations

import sys
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

# Make research/ importable in tests.
RESEARCH_DIR = Path(__file__).resolve().parent.parent / "research"
sys.path.insert(0, str(RESEARCH_DIR))

import backtest_momentum as bm  # noqa: E402
import backtest_failed_breakout as fb  # noqa: E402

ET = ZoneInfo("America/New_York")


# ---- helpers ---------------------------------------------------------------

def et_dt(y: int, m: int, d: int, hh: int, mm: int) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=ET)


def bar(ts: datetime, close: float, *, volume: float = 1000.0) -> bm.Bar:
    return bm.Bar(ts=ts, open=close, high=close, low=close, close=close, volume=volume)


def intraday_day(y: int, m: int, d: int, closes: list[float], *, volume: float = 1000.0):
    """5-min bars from 09:30 ET, one per close."""
    out = []
    minute = 9 * 60 + 30
    for c in closes:
        out.append(bar(et_dt(y, m, d, minute // 60, minute % 60), c, volume=volume))
        minute += 5
    return out


def _daily_with_prior_high(y: int, m: int, d: int, prior_high: float):
    """Daily series so that (y,m,d) has `prior_high` as its prior-day high."""
    return [
        bm.Bar(et_dt(y, m, d - 1, 12, 0), 0, prior_high, 0, prior_high, 0),
        bm.Bar(et_dt(y, m, d, 12, 0), 0, prior_high + 10, 0, 0, 0),
    ]


# ---- find_failed_breakouts -------------------------------------------------

def test_failed_breakout_detected_when_close_drops_back_below():
    daily = _daily_with_prior_high(2026, 5, 19, prior_high=100.0)
    # 09:35 99 (skip first bar; we set earliest=09:35)
    # 09:40 100.5 (breakout)
    # 09:45 99.5 (failure — closed back below 100)
    closes = [99.0, 100.5, 99.5, 99.0, 98.5]
    intraday = intraday_day(2026, 5, 19, closes)
    events = fb.find_failed_breakouts(daily, intraday, baseline_volume=1000.0)
    assert len(events) == 1
    e = events[0]
    assert bm.et_time(e.breakout_bar.ts) == time(9, 35)
    assert e.breakout_bar.close == 100.5
    assert bm.et_time(e.failure_bar.ts) == time(9, 40)
    assert e.failure_bar.close == 99.5
    assert e.prior_high == 100.0
    assert e.volume_ratio == 1.0


def test_breakout_that_holds_is_not_a_failed_event():
    daily = _daily_with_prior_high(2026, 5, 19, prior_high=100.0)
    # Breakout at 09:35 and price keeps going up — no failure.
    closes = [99.0, 100.5, 101.0, 102.0, 103.0, 104.0]
    intraday = intraday_day(2026, 5, 19, closes)
    events = fb.find_failed_breakouts(daily, intraday, baseline_volume=1000.0)
    assert events == []


def test_only_first_breakout_each_day_is_evaluated():
    daily = _daily_with_prior_high(2026, 5, 19, prior_high=100.0)
    # Two distinct breakouts in the day: 09:35 (fails) and 11:30 (holds).
    # The first one fires the event; the second doesn't matter.
    closes = (
        [99.0, 100.5, 99.5]                 # 09:30-09:40 — failed breakout
        + [99.0] * 21                       # 09:45-11:25 below high
        + [101.0, 102.0, 103.0]             # 11:30-11:40 — second breakout
    )
    intraday = intraday_day(2026, 5, 19, closes)
    events = fb.find_failed_breakouts(daily, intraday, baseline_volume=1000.0)
    assert len(events) == 1
    assert bm.et_time(events[0].breakout_bar.ts) == time(9, 35)


def test_breakout_after_cutoff_is_skipped():
    daily = _daily_with_prior_high(2026, 5, 19, prior_high=100.0)
    # Breakout at 12:05 — past the 12:00 cutoff.
    closes = [99.0] * 31 + [101.0, 99.5]  # 09:30 + 31*5 = 12:05
    intraday = intraday_day(2026, 5, 19, closes)
    assert bm.et_time(intraday[31].ts) == time(12, 5)
    events = fb.find_failed_breakouts(
        daily, intraday,
        breakout_cutoff=time(12, 0),
        baseline_volume=1000.0,
    )
    assert events == []


def test_failure_outside_window_does_not_count():
    daily = _daily_with_prior_high(2026, 5, 19, prior_high=100.0)
    # Breakout at 09:35, failure 65 minutes later — past a 60-min window.
    closes = [99.0, 100.5] + [101.0] * 12 + [99.5]  # 09:35 -> failure at 10:40
    intraday = intraday_day(2026, 5, 19, closes)
    assert bm.et_time(intraday[-1].ts) == time(10, 40)
    events = fb.find_failed_breakouts(
        daily, intraday,
        failure_window_minutes=60,
        baseline_volume=1000.0,
    )
    assert events == []


def test_earliest_breakout_filter_skips_first_bars():
    daily = _daily_with_prior_high(2026, 5, 19, prior_high=100.0)
    # 09:30 breakout — but earliest_breakout=09:35 skips it.
    closes = [101.0, 99.5, 99.0]
    intraday = intraday_day(2026, 5, 19, closes)
    events = fb.find_failed_breakouts(
        daily, intraday,
        earliest_breakout=time(9, 35),
        baseline_volume=1000.0,
    )
    assert events == []


def test_volume_ratio_uses_failure_bar_volume():
    daily = _daily_with_prior_high(2026, 5, 19, prior_high=100.0)
    intraday = [
        bar(et_dt(2026, 5, 19, 9, 35), 100.5, volume=500.0),    # breakout
        bar(et_dt(2026, 5, 19, 9, 40), 99.5, volume=2500.0),   # failure on big volume
    ]
    events = fb.find_failed_breakouts(daily, intraday, baseline_volume=1000.0)
    assert len(events) == 1
    assert events[0].volume_ratio == 2.5  # 2500/1000


# ---- forward_returns_from_failure -----------------------------------------

def test_forward_returns_measured_from_failure_close():
    day = intraday_day(
        2026, 5, 19,
        # 09:30..; breakout at idx 1 (09:35, 100.5); failure at idx 2 (09:40, 99.5)
        [99, 100.5, 99.5, 99.0, 98.5, 97.5, 97.0, 96.0, 95.5, 95.0, 94.5, 94.0, 93.5],
    )
    event = fb.FailedBreakout(
        day=day[2].ts.date(),
        breakout_bar=day[1],
        failure_bar=day[2],
        prior_high=100.0,
        volume_ratio=1.0,
    )
    r = fb.forward_returns_from_failure(event, day)
    # Failure entry at 99.5. +30m = 10:10 = idx 8 -> 95.5 -> -4.02%
    assert round(r.ret_30m, 4) == round(95.5 / 99.5 - 1, 4)
    # +60m = 10:40 -> no bar (series ends 10:30) -> None
    assert r.ret_60m is None
    # to-close = last bar 93.5 -> -6.03%
    assert round(r.ret_close, 4) == round(93.5 / 99.5 - 1, 4)


# ---- summarize ------------------------------------------------------------

def _event(symbol, vol_ratio, ret_close):
    failure_bar = bar(et_dt(2026, 5, 19, 9, 40), 100.0)
    breakout_bar = bar(et_dt(2026, 5, 19, 9, 35), 100.5)
    e = fb.FailedBreakout(
        day=et_dt(2026, 5, 19, 9, 35).date(),
        breakout_bar=breakout_bar,
        failure_bar=failure_bar,
        prior_high=100.0,
        volume_ratio=vol_ratio,
    )
    return fb.Event(symbol, e, bm.ForwardReturns(None, None, ret_close))


def test_summarize_put_hit_rate_counts_negative_returns():
    events = [
        _event("A", 2.0, -0.03),   # high, put won
        _event("B", 2.5, -0.01),   # high, put won
        _event("C", 2.5,  0.02),   # high, put lost
        _event("D", 1.0, -0.02),   # low, put won
    ]
    s = fb.summarize(events, vol_threshold=1.5)
    assert s["all"]["n"] == 4
    assert s["high_volume"]["n"] == 3
    assert s["low_volume"]["n"] == 1
    # 2 of 3 high-volume events had negative returns -> put-hit 0.667
    assert round(s["high_volume"]["put_hit_rate_close"], 4) == round(2 / 3, 4)
    assert s["low_volume"]["put_hit_rate_close"] == 1.0


# ---- analyze_symbol end-to-end --------------------------------------------

def test_analyze_symbol_attaches_returns():
    daily = _daily_with_prior_high(2026, 5, 19, prior_high=100.0)
    intraday = intraday_day(
        2026, 5, 19,
        [99.0, 100.5, 99.5, 98.0, 97.0, 96.0, 95.0],
    )
    events = fb.analyze_symbol("TEST", daily, intraday, baseline_volume=1000.0)
    assert len(events) == 1
    e = events[0]
    assert e.symbol == "TEST"
    assert e.breakout.failure_bar.close == 99.5
    assert e.returns.ret_close is not None
    assert e.returns.ret_close < 0  # underlying kept falling — good for puts
