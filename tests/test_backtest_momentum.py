"""Tests for the momentum-breakout backtester's pure analysis functions.

No network — every test feeds synthetic Bar data through the detection,
forward-return, and summary logic.
"""

from __future__ import annotations

import sys
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

# Make scripts/ importable in tests.
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import backtest_momentum as bm  # noqa: E402

ET = ZoneInfo("America/New_York")


# ---- helpers ---------------------------------------------------------------

def et_dt(y: int, m: int, d: int, hh: int, mm: int) -> datetime:
    """An ET-local timezone-aware datetime."""
    return datetime(y, m, d, hh, mm, tzinfo=ET)


def bar(ts: datetime, close: float, *, volume: float = 1000.0) -> bm.Bar:
    """A bar whose OHLC all sit at `close` (enough for breakout/return math)."""
    return bm.Bar(ts=ts, open=close, high=close, low=close, close=close, volume=volume)


def intraday_day(y: int, m: int, d: int, closes: list[float], *, volume: float = 1000.0):
    """5-minute bars from 09:30 ET, one per close given."""
    out = []
    minute = 9 * 60 + 30
    for c in closes:
        out.append(bar(et_dt(y, m, d, minute // 60, minute % 60), c, volume=volume))
        minute += 5
    return out


# ---- et_date / et_time -----------------------------------------------------

def test_et_date_and_time_convert_from_utc():
    # 2026-05-19 13:30 UTC == 09:30 ET (EDT, -4)
    utc = datetime(2026, 5, 19, 13, 30, tzinfo=ZoneInfo("UTC"))
    assert bm.et_date(utc).isoformat() == "2026-05-19"
    assert bm.et_time(utc) == time(9, 30)


# ---- prior_day_highs -------------------------------------------------------

def test_prior_day_highs_maps_to_previous_day():
    daily = [
        bm.Bar(et_dt(2026, 5, 18, 12, 0), 95, 100, 94, 99, 0),
        bm.Bar(et_dt(2026, 5, 19, 12, 0), 99, 105, 98, 104, 0),
        bm.Bar(et_dt(2026, 5, 20, 12, 0), 104, 108, 103, 107, 0),
    ]
    highs = bm.prior_day_highs(daily)
    # First day omitted — no predecessor.
    assert highs == {
        et_dt(2026, 5, 19, 12, 0).date(): 100,
        et_dt(2026, 5, 20, 12, 0).date(): 105,
    }


def test_prior_day_highs_sorts_unordered_input():
    daily = [
        bm.Bar(et_dt(2026, 5, 20, 12, 0), 104, 108, 103, 107, 0),
        bm.Bar(et_dt(2026, 5, 18, 12, 0), 95, 100, 94, 99, 0),
        bm.Bar(et_dt(2026, 5, 19, 12, 0), 99, 105, 98, 104, 0),
    ]
    highs = bm.prior_day_highs(daily)
    assert highs[et_dt(2026, 5, 20, 12, 0).date()] == 105


# ---- regular_session -------------------------------------------------------

def test_regular_session_drops_pre_and_post_market():
    bars = [
        bar(et_dt(2026, 5, 19, 8, 0), 1),    # pre-market
        bar(et_dt(2026, 5, 19, 9, 25), 2),   # pre-market
        bar(et_dt(2026, 5, 19, 9, 30), 3),   # open — kept
        bar(et_dt(2026, 5, 19, 12, 0), 4),   # midday — kept
        bar(et_dt(2026, 5, 19, 15, 55), 5),  # late — kept
        bar(et_dt(2026, 5, 19, 16, 0), 6),   # close bar — dropped (>= 16:00)
        bar(et_dt(2026, 5, 19, 18, 0), 7),   # post-market
    ]
    kept = [b.close for b in bm.regular_session(bars)]
    assert kept == [3, 4, 5]


# ---- group_by_day ----------------------------------------------------------

def test_group_by_day_buckets_and_sorts():
    bars = [
        bar(et_dt(2026, 5, 19, 9, 35), 10),
        bar(et_dt(2026, 5, 19, 9, 30), 9),
        bar(et_dt(2026, 5, 20, 9, 30), 11),
    ]
    grouped = bm.group_by_day(bars)
    assert set(grouped) == {
        et_dt(2026, 5, 19, 0, 0).date(),
        et_dt(2026, 5, 20, 0, 0).date(),
    }
    day19 = grouped[et_dt(2026, 5, 19, 0, 0).date()]
    assert [b.close for b in day19] == [9, 10]  # time-sorted


# ---- find_breakouts --------------------------------------------------------

def _daily_with_prior_high(day_y, day_m, day_d, prior_high):
    """Daily series so that (day) has `prior_high` as its prior-day high."""
    return [
        bm.Bar(et_dt(day_y, day_m, day_d - 1, 12, 0), 0, prior_high, 0, prior_high, 0),
        bm.Bar(et_dt(day_y, day_m, day_d, 12, 0), 0, prior_high + 10, 0, 0, 0),
    ]


def test_find_breakouts_first_bar_above_prior_high():
    daily = _daily_with_prior_high(2026, 5, 19, prior_high=100.0)
    # 09:30=98, 09:35=99.5, 09:40=100.5 (breakout), 09:45=101
    intraday = intraday_day(2026, 5, 19, [98.0, 99.5, 100.5, 101.0])
    breakouts = bm.find_breakouts(daily, intraday, baseline_volume=1000.0)
    assert len(breakouts) == 1
    bo = breakouts[0]
    assert bm.et_time(bo.breakout_bar.ts) == time(9, 40)
    assert bo.breakout_bar.close == 100.5
    assert bo.prior_high == 100.0
    assert bo.volume_ratio == 1.0


def test_find_breakouts_one_per_day():
    daily = _daily_with_prior_high(2026, 5, 19, prior_high=100.0)
    intraday = intraday_day(2026, 5, 19, [101.0, 102.0, 103.0])  # all above
    breakouts = bm.find_breakouts(daily, intraday, baseline_volume=1000.0)
    assert len(breakouts) == 1
    assert bm.et_time(breakouts[0].breakout_bar.ts) == time(9, 30)


def test_find_breakouts_respects_cutoff():
    daily = _daily_with_prior_high(2026, 5, 19, prior_high=100.0)
    # Breakout only at 15:05 ET — past a 15:00 cutoff.
    closes = [99.0] * 67 + [101.0]  # 09:30 + 67*5min = 15:05
    intraday = intraday_day(2026, 5, 19, closes)
    assert bm.et_time(intraday[-1].ts) == time(15, 5)
    breakouts = bm.find_breakouts(
        daily, intraday, cutoff=time(15, 0), baseline_volume=1000.0
    )
    assert breakouts == []


def test_find_breakouts_skips_day_without_prior_high():
    # Only one daily bar -> no prior-day high for any intraday day.
    daily = [bm.Bar(et_dt(2026, 5, 19, 12, 0), 0, 100, 0, 0, 0)]
    intraday = intraday_day(2026, 5, 19, [101.0, 102.0])
    assert bm.find_breakouts(daily, intraday, baseline_volume=1000.0) == []


def test_find_breakouts_default_baseline_is_sample_mean():
    daily = _daily_with_prior_high(2026, 5, 19, prior_high=100.0)
    # Two bars: one below, one breakout. Volumes 500 and 1500 -> mean 1000.
    intraday = [
        bar(et_dt(2026, 5, 19, 9, 30), 99.0, volume=500.0),
        bar(et_dt(2026, 5, 19, 9, 35), 101.0, volume=1500.0),
    ]
    breakouts = bm.find_breakouts(daily, intraday)  # baseline_volume=None
    assert len(breakouts) == 1
    assert breakouts[0].volume_ratio == 1.5  # 1500 / mean(1000)


# ---- forward_returns -------------------------------------------------------

def test_forward_returns_30_60_close():
    # 13 bars, 09:30..10:30. Entry at index 2 (09:40), close 100.
    day = intraday_day(
        2026, 5, 19,
        [98, 99, 100, 100.5, 101, 102, 103, 102, 104, 105, 106, 107, 108],
    )
    bo = bm.Breakout(day[2].ts.date(), day[2], prior_high=99.0, volume_ratio=1.0)
    fr = bm.forward_returns(bo, day)
    # +30m = 09:40 -> 10:10 = index 8, close 104 -> +4%
    assert round(fr.ret_30m, 4) == 0.04
    # +60m = 10:40 has no bar (series ends 10:30) -> None
    assert fr.ret_60m is None
    # to-close = last bar 10:30, close 108 -> +8%
    assert round(fr.ret_close, 4) == 0.08


def test_forward_returns_missing_horizon_is_none():
    # Entry on the second-to-last bar — no +30m / +60m bar exists.
    day = intraday_day(2026, 5, 19, [100, 101, 102])
    bo = bm.Breakout(day[1].ts.date(), day[1], prior_high=99.0, volume_ratio=1.0)
    fr = bm.forward_returns(bo, day)
    assert fr.ret_30m is None
    assert fr.ret_60m is None
    # to-close still resolves: entry 101 -> last close 102
    assert round(fr.ret_close, 6) == round(102 / 101 - 1, 6)


def test_forward_returns_entry_is_last_bar_close_is_zero():
    day = intraday_day(2026, 5, 19, [100, 101])
    bo = bm.Breakout(day[1].ts.date(), day[1], prior_high=99.0, volume_ratio=1.0)
    fr = bm.forward_returns(bo, day)
    assert fr.ret_close == 0.0


# ---- unconditional_to_close ------------------------------------------------

def test_unconditional_to_close_one_return_per_precutoff_bar():
    day = intraday_day(2026, 5, 19, [100, 110, 120])  # day close 120
    rets = bm.unconditional_to_close(day, cutoff=time(15, 0))
    assert len(rets) == 3
    assert round(rets[0], 6) == round(120 / 100 - 1, 6)
    assert rets[2] == 0.0  # last bar to itself


def test_unconditional_to_close_excludes_postcutoff_bars():
    closes = [100.0] * 67 + [200.0]  # last bar 15:05
    day = intraday_day(2026, 5, 19, closes)
    rets = bm.unconditional_to_close(day, cutoff=time(15, 0))
    # 66 pre-cutoff bars: 09:30..14:55 (15:00 itself hits the cutoff)
    assert len(rets) == 66


# ---- summarize -------------------------------------------------------------

def _event(symbol, vol_ratio, ret_close):
    bo = bm.Breakout(
        et_dt(2026, 5, 19, 9, 30).date(),
        bar(et_dt(2026, 5, 19, 9, 30), 100.0),
        prior_high=99.0,
        volume_ratio=vol_ratio,
    )
    return bm.Event(symbol, bo, bm.ForwardReturns(None, None, ret_close))


def test_summarize_splits_by_volume_bucket():
    events = [
        _event("A", 2.0, 0.05),   # high
        _event("B", 2.5, 0.03),   # high
        _event("C", 1.0, -0.02),  # low
    ]
    s = bm.summarize(events, vol_threshold=1.5)
    assert s["all"]["n"] == 3
    assert s["high_volume"]["n"] == 2
    assert s["low_volume"]["n"] == 1
    assert round(s["high_volume"]["mean_close"], 4) == 0.04
    assert round(s["low_volume"]["mean_close"], 4) == -0.02
    assert s["high_volume"]["hit_rate_close"] == 1.0
    assert s["low_volume"]["hit_rate_close"] == 0.0


def test_summarize_empty():
    s = bm.summarize([], vol_threshold=1.5)
    assert s["all"]["n"] == 0
    assert s["all"]["mean_close"] is None


# ---- analyze_symbol (end to end, pure) -------------------------------------

def test_analyze_symbol_end_to_end():
    daily = _daily_with_prior_high(2026, 5, 19, prior_high=100.0)
    # Breakout at 09:40 (100.5), then drifts up to 106 by close.
    intraday = intraday_day(
        2026, 5, 19,
        [98, 99, 100.5, 101, 102, 103, 104, 105, 105.5, 106],
    )
    events = bm.analyze_symbol("TEST", daily, intraday, baseline_volume=1000.0)
    assert len(events) == 1
    e = events[0]
    assert e.symbol == "TEST"
    assert e.breakout.breakout_bar.close == 100.5
    assert e.returns.ret_close is not None
    assert e.returns.ret_close > 0
