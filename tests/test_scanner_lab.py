"""Tests for the scanner lab (SCANNER_PROGRAM.md Phase S1).

Detectors are exercised against hand-crafted bar sequences (like the
failed-breakout tests); the ScannerLab runtime against an in-memory bar
source. No network, no DB.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

import pytest

from x_alpaca_trading_bot.scanners.failed_breakout import Bar, FailedBreakout
from x_alpaca_trading_bot.scanners.lab import (
    HYPOTHESES,
    LabDataBarSource,
    PriorDay,
    ScannerLab,
    _running_vwap,
    detect_failed_breakout,
    detect_gap_fade,
    detect_prior_low_break,
    detect_vwap_reject,
    resolve_hypotheses,
)

ET = ZoneInfo("America/New_York")


def _bar(hh: int, mm: int, close: float, *, volume: float = 1000.0,
         low: float | None = None, open_: float | None = None,
         day: int = 6) -> Bar:
    """5-min bar at the given ET wall clock on 2026-06-day. open/high/low
    default to close so VWAP typical price == close unless overridden."""
    ts_et = datetime(2026, 6, day, hh, mm, tzinfo=ET)
    return Bar(
        ts=ts_et.astimezone(timezone.utc),
        open=open_ if open_ is not None else close,
        high=close,
        low=low if low is not None else close,
        close=close,
        volume=volume,
    )


_PRIOR = PriorDay(high=100.0, low=95.0, close=98.0)


# ---- VWAP helper -----------------------------------------------------------

def test_running_vwap_equal_volume_is_mean_of_typical_prices() -> None:
    bars = [_bar(9, 35, 100.0), _bar(9, 40, 102.0), _bar(9, 45, 98.0)]
    vwaps = _running_vwap(bars)
    assert vwaps == [pytest.approx(100.0), pytest.approx(101.0), pytest.approx(100.0)]


def test_running_vwap_none_while_no_volume() -> None:
    bars = [_bar(9, 35, 100.0, volume=0.0), _bar(9, 40, 102.0)]
    vwaps = _running_vwap(bars)
    assert vwaps[0] is None
    assert vwaps[1] == pytest.approx(102.0)


# ---- vwap_reject -----------------------------------------------------------

def test_vwap_reject_detected() -> None:
    """Push >=0.3% above running VWAP, then close back below it."""
    bars = [
        _bar(9, 35, 100.0),   # vwap 100.00 — not above
        _bar(9, 40, 102.0),   # vwap 101.00; 102 >= 101*1.003 → trigger
        _bar(9, 45, 99.0),    # vwap 100.33; 99 < vwap → confirm
    ]
    event = detect_vwap_reject("AAPL", bars, _PRIOR, 1000.0)
    assert event is not None
    assert event.breakout_price == 102.0
    assert event.failure_price == 99.0
    assert event.prior_high == pytest.approx(100.333333, rel=1e-4)  # ref = vwap
    assert event.volume_ratio == 1.0


def test_vwap_reject_none_when_strength_holds() -> None:
    bars = [
        _bar(9, 35, 100.0),
        _bar(9, 40, 102.0),   # trigger
        _bar(9, 45, 103.0),   # stays above vwap
        _bar(9, 50, 104.0),
    ]
    assert detect_vwap_reject("AAPL", bars, _PRIOR, None) is None


def test_vwap_reject_ignores_trigger_after_cutoff() -> None:
    bars = [
        _bar(9, 35, 100.0),
        _bar(10, 35, 102.0),  # would trigger, but after 10:30
        _bar(10, 40, 99.0),
    ]
    assert detect_vwap_reject("AAPL", bars, _PRIOR, None) is None


# ---- gap_fade --------------------------------------------------------------

def _gap_bars(open_px: float) -> list[Bar]:
    return [
        _bar(9, 30, open_px + 0.5, open_=open_px, low=open_px),        # OR
        _bar(9, 35, open_px + 0.3, low=open_px - 0.2),                 # OR
        _bar(9, 40, open_px + 0.4, low=open_px - 0.1),                 # OR
        _bar(9, 50, open_px - 0.5),                                    # below OR low
    ]


def test_gap_fade_detected() -> None:
    """Open 3% above prior close (98 → 101), then lose the opening-range low."""
    bars = _gap_bars(101.0)
    event = detect_gap_fade("AAPL", bars, _PRIOR, 500.0)
    assert event is not None
    assert event.breakout_price == 101.0            # the gap open
    assert event.prior_high == pytest.approx(100.8)  # OR low = 101 - 0.2
    assert event.failure_price == pytest.approx(100.5)
    assert event.volume_ratio == 2.0


def test_gap_fade_none_without_gap() -> None:
    """Open below the 2% gap threshold (98 * 1.02 = 99.96) — no event even
    though the opening range later breaks."""
    bars = _gap_bars(99.5)
    assert detect_gap_fade("AAPL", bars, _PRIOR, None) is None


def test_gap_fade_none_when_range_holds() -> None:
    bars = [
        _bar(9, 30, 101.5, open_=101.0, low=101.0),
        _bar(9, 35, 101.3, low=100.8),
        _bar(9, 40, 101.4, low=100.9),
        _bar(9, 50, 101.2),   # never closes below OR low (100.8)
        _bar(10, 0, 101.6),
    ]
    assert detect_gap_fade("AAPL", bars, _PRIOR, None) is None


def test_gap_fade_confirm_window_closes_at_1130() -> None:
    bars = [
        _bar(9, 30, 101.5, open_=101.0, low=101.0),
        _bar(9, 35, 101.3, low=100.8),
        _bar(9, 40, 101.4, low=100.9),
        _bar(11, 30, 100.0),  # breaks OR low but at/after 11:30 — too late
    ]
    assert detect_gap_fade("AAPL", bars, _PRIOR, None) is None


# ---- prior_low_break -------------------------------------------------------

def test_prior_low_break_detected() -> None:
    bars = [
        _bar(9, 35, 96.0),    # above prior low (95)
        _bar(9, 40, 94.5),    # trigger: close below prior low
        _bar(9, 45, 94.0),    # confirm: next bar also below
    ]
    event = detect_prior_low_break("AAPL", bars, _PRIOR, 2000.0)
    assert event is not None
    assert event.breakout_price == 94.5
    assert event.failure_price == 94.0
    assert event.prior_high == 95.0   # ref = prior-day low
    assert event.volume_ratio == 0.5


def test_prior_low_break_single_bar_sweep_rejected() -> None:
    """One close below, immediate reclaim — a stop sweep, not a breakdown."""
    bars = [
        _bar(9, 35, 96.0),
        _bar(9, 40, 94.5),    # trigger
        _bar(9, 45, 95.5),    # reclaims — no confirm
        _bar(9, 50, 94.0),    # later weakness is NOT re-evaluated (first only)
    ]
    assert detect_prior_low_break("AAPL", bars, _PRIOR, None) is None


def test_prior_low_break_trigger_after_cutoff_ignored() -> None:
    bars = [
        _bar(9, 35, 96.0),
        _bar(10, 30, 94.5),
        _bar(10, 35, 94.0),
    ]
    assert detect_prior_low_break("AAPL", bars, _PRIOR, None) is None


# ---- failed_breakout adapter ----------------------------------------------

def test_failed_breakout_adapter_matches_original_semantics() -> None:
    bars = [
        _bar(9, 35, 99.5),
        _bar(9, 40, 100.5),   # breakout above prior high (100)
        _bar(9, 50, 99.0),    # failure
    ]
    event = detect_failed_breakout("AAPL", bars, _PRIOR, 1000.0)
    assert event is not None
    assert event.prior_high == 100.0
    assert event.failure_price == 99.0


# ---- resolve_hypotheses ----------------------------------------------------

def test_resolve_none_returns_all() -> None:
    assert set(resolve_hypotheses(None)) == set(HYPOTHESES)


def test_resolve_subset_and_normalization() -> None:
    out = resolve_hypotheses([" Gap_Fade ", "vwap_reject"])
    assert set(out) == {"gap_fade", "vwap_reject"}


def test_resolve_unknown_name_raises() -> None:
    with pytest.raises(RuntimeError, match="Unknown scanner hypothesis"):
        resolve_hypotheses(["failed_breakout", "moon_phase"])


def test_resolve_empty_raises() -> None:
    with pytest.raises(RuntimeError, match="empty"):
        resolve_hypotheses(["", "  "])


def test_resolve_binds_failed_breakout_cutoff() -> None:
    """With a 12:00 override an 11:00 breakout counts; the stock default
    (10:30) rejects it."""
    bars = [
        _bar(9, 35, 99.0),
        _bar(11, 0, 100.5),
        _bar(11, 10, 99.0),
    ]
    stock = resolve_hypotheses(["failed_breakout"])["failed_breakout"]
    assert stock("AAPL", bars, _PRIOR, None) is None

    widened = resolve_hypotheses(
        ["failed_breakout"], failed_breakout_cutoff=time(12, 0)
    )["failed_breakout"]
    assert widened("AAPL", bars, _PRIOR, None) is not None


# ---- ScannerLab runtime ----------------------------------------------------

@dataclass
class _FakeLabSource:
    bars_by_key: dict[tuple[str, date], list[Bar]] = field(default_factory=dict)
    prior_by_key: dict[tuple[str, date], PriorDay | None] = field(default_factory=dict)
    baseline_by_key: dict[tuple[str, date], float | None] = field(default_factory=dict)
    raises_for: set[str] = field(default_factory=set)

    def get_intraday_bars(self, ticker: str, day: date) -> list[Bar]:
        if ticker in self.raises_for:
            raise RuntimeError(f"network glitch for {ticker}")
        return self.bars_by_key.get((ticker, day), [])

    def get_prior_day_bar(self, ticker: str, day: date) -> PriorDay | None:
        return self.prior_by_key.get((ticker, day))

    def get_baseline_bar_volume(self, ticker: str, day: date) -> float | None:
        return self.baseline_by_key.get((ticker, day))


def _now_et(hh: int = 11, mm: int = 0, day: int = 6) -> datetime:
    return datetime(2026, 6, day, hh, mm, tzinfo=ET).astimezone(timezone.utc)


def test_lab_multiple_hypotheses_fire_on_one_ticker() -> None:
    """One bar sequence that is BOTH a failed breakout (vs prior high 100)
    and a VWAP reject — the lab records each under its own name."""
    day = date(2026, 6, 6)
    bars = [
        _bar(9, 35, 99.0),
        _bar(9, 40, 100.5),   # breakout + vwap strength (vwap 99.75)
        _bar(9, 50, 99.0),    # fails prior high AND closes below vwap
    ]
    src = _FakeLabSource(
        bars_by_key={("AAPL", day): bars},
        prior_by_key={("AAPL", day): _PRIOR},
    )
    recorded: list[tuple[str, FailedBreakout]] = []

    def _rec(name, event, now) -> bool:
        recorded.append((name, event))
        return True

    lab = ScannerLab(universe=["AAPL"], bar_source=src, record_event=_rec)
    new_events = lab.scan(_now_et())

    names = {n for n, _ in new_events}
    assert names == {"failed_breakout", "vwap_reject"}
    assert {n for n, _ in recorded} == names


def test_lab_detector_exception_does_not_stop_others() -> None:
    day = date(2026, 6, 6)

    def _boom(ticker, bars, prior, baseline):
        raise RuntimeError("detector bug")

    def _always(ticker, bars, prior, baseline):
        return FailedBreakout(
            ticker=ticker, day=day,
            breakout_ts=bars[0].ts, breakout_price=bars[0].close,
            failure_ts=bars[-1].ts, failure_price=bars[-1].close,
            prior_high=prior.high, volume_ratio=None,
        )

    src = _FakeLabSource(
        bars_by_key={("AAPL", day): [_bar(9, 35, 99.0), _bar(9, 40, 98.0)]},
        prior_by_key={("AAPL", day): _PRIOR},
    )
    lab = ScannerLab(
        universe=["AAPL"], bar_source=src,
        record_event=lambda n, e, t: True,
        hypotheses={"boom": _boom, "ok": _always},
    )
    new_events = lab.scan(_now_et())
    assert [n for n, _ in new_events] == ["ok"]


def test_lab_skips_ticker_when_fetch_raises() -> None:
    day = date(2026, 6, 6)
    bars = [_bar(9, 35, 99.0), _bar(9, 40, 100.5), _bar(9, 50, 99.0)]
    src = _FakeLabSource(
        bars_by_key={("AAPL", day): bars, ("BAD", day): bars},
        prior_by_key={("AAPL", day): _PRIOR, ("BAD", day): _PRIOR},
        raises_for={"BAD"},
    )
    lab = ScannerLab(
        universe=["BAD", "AAPL"], bar_source=src,
        record_event=lambda n, e, t: True,
    )
    tickers = {e.ticker for _, e in lab.scan(_now_et())}
    assert tickers == {"AAPL"}


def test_lab_dedupe_via_recorder_false() -> None:
    day = date(2026, 6, 6)
    bars = [_bar(9, 35, 99.0), _bar(9, 40, 100.5), _bar(9, 50, 99.0)]
    src = _FakeLabSource(
        bars_by_key={("AAPL", day): bars},
        prior_by_key={("AAPL", day): _PRIOR},
    )
    lab = ScannerLab(
        universe=["AAPL"], bar_source=src,
        record_event=lambda n, e, t: False,  # everything already recorded
    )
    assert lab.scan(_now_et()) == []


def test_lab_rejects_naive_now() -> None:
    lab = ScannerLab(
        universe=["AAPL"], bar_source=_FakeLabSource(),
        record_event=lambda n, e, t: True,
    )
    with pytest.raises(ValueError):
        lab.scan(datetime(2026, 6, 6, 11, 0))


# ---- LabDataBarSource ------------------------------------------------------

@dataclass(frozen=True)
class _FakeDaily:
    ts: datetime
    high: float
    low: float
    close: float
    volume: int


class _FakeDS:
    def __init__(self, daily: list[_FakeDaily]) -> None:
        self._daily = daily

    def get_recent_daily_bars(self, ticker: str, now: datetime, *, limit: int):
        return self._daily[-limit:]


def test_lab_source_prior_day_bar() -> None:
    def _d(day: int, high: float, low: float, close: float) -> _FakeDaily:
        ts = datetime(2026, 6, day, 16, 0, tzinfo=ET).astimezone(timezone.utc)
        return _FakeDaily(ts=ts, high=high, low=low, close=close, volume=1)

    src = LabDataBarSource(_FakeDS([
        _d(4, 101.0, 96.0, 99.0),
        _d(5, 100.0, 95.0, 98.0),   # the prior trading day
        _d(6, 999.0, 1.0, 500.0),   # target day — must be excluded
    ]))
    prior = src.get_prior_day_bar("AAPL", date(2026, 6, 6))
    assert prior == PriorDay(high=100.0, low=95.0, close=98.0)


def test_lab_source_prior_day_none_without_history() -> None:
    src = LabDataBarSource(_FakeDS([]))
    assert src.get_prior_day_bar("AAPL", date(2026, 6, 6)) is None
