"""Failed-breakout scanner — live counterpart to research/backtest_failed_breakout.py.

Hypothesis (validated in the offline backtest at +0.62% to-close edge on
top-quintile-volume small/mid-cap events): when a stock breaks above the
prior trading day's high in the morning, then closes back below it within
60 minutes, the underlying tends to drift down over the next 30-60 minutes
as trapped longs cover.

Live scanner contract — Phase A
-------------------------------
1. On each scan tick (default every 5 min during RTH), iterate the
   configured universe of optionable tickers.
2. For each ticker, fetch the current day's 5-min bars + the prior day's
   daily bar. Detect whether a "failed breakout" event has occurred
   today using the same detection logic as the backtest.
3. If detected and not already recorded, write a row to scanner_events.
4. Phase A is log-only — no orchestrator wiring, no orders submitted.

Detection params match the backtest:
    breakout window: 09:35 - 12:00 ET (first bar closing above prior-day high)
    failure  window: 60 min after breakout (first subsequent bar closing
                     back below the prior-day high)
    one event max per ticker per day (DB UNIQUE enforces this)

State and idempotency
---------------------
Each scan is stateless beyond what the DB stores. A per-ticker per-day
UNIQUE constraint in scanner_events means a second scan of the same day
that re-detects the same event is a no-op insert (handled with
ON CONFLICT DO NOTHING). The in-memory cache below is purely a fast-path
optimization — correctness lives in the DB.

Detection primitives are intentionally vendored from the backtest rather
than imported. CLAUDE.md treats research/ as read-only relative to the
live bot, and production never imports from there. Duplication of a few
small pure functions is the price; the alternative is silent coupling.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable, Protocol
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

# Detection defaults match research/backtest_failed_breakout.py.
DEFAULT_EARLIEST_BREAKOUT = time(9, 35)
DEFAULT_BREAKOUT_CUTOFF = time(12, 0)
DEFAULT_FAILURE_WINDOW_MINUTES = 60


# ---- Bar primitive --------------------------------------------------------

@dataclass(frozen=True)
class Bar:
    """One OHLCV bar. ts is timezone-aware UTC."""
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class FailedBreakout:
    """A detected failed breakout. Returned by find_failed_breakout, written
    to scanner_events by the scanner."""
    ticker: str
    day: date
    breakout_ts: datetime
    breakout_price: float
    failure_ts: datetime
    failure_price: float
    prior_high: float
    volume_ratio: float | None


# ---- Pure detection logic -------------------------------------------------

def et_time(ts: datetime) -> time:
    """Wall-clock ET time for a timezone-aware UTC datetime."""
    return ts.astimezone(ET).timetz().replace(tzinfo=None)


def et_date(ts: datetime) -> date:
    """ET trading-day date for a timezone-aware UTC datetime."""
    return ts.astimezone(ET).date()


def find_failed_breakout(
    ticker: str,
    intraday_bars: list[Bar],
    prior_high: float,
    *,
    earliest_breakout: time = DEFAULT_EARLIEST_BREAKOUT,
    breakout_cutoff: time = DEFAULT_BREAKOUT_CUTOFF,
    failure_window_minutes: int = DEFAULT_FAILURE_WINDOW_MINUTES,
    baseline_volume: float | None = None,
) -> FailedBreakout | None:
    """Scan one ticker's intraday bars for the first failed-breakout event.

    Mirrors `research.backtest_failed_breakout.find_failed_breakouts` but
    scoped to one day at a time (the live scanner only ever sees today's
    bars; the backtest iterates many days).

    Returns None when:
      - no bar in [earliest_breakout, breakout_cutoff) closes above prior_high
        (no breakout yet today)
      - a breakout exists but no subsequent bar within failure_window_minutes
        closes back below prior_high (breakout held — success, not our event)
      - intraday_bars is empty

    Returns a FailedBreakout otherwise. Only the *first* breakout of the day
    is considered — later breakouts on the same day happen in a different
    regime (failed-then-recovered) and are out of scope.
    """
    if not intraday_bars or prior_high <= 0:
        return None

    bars = sorted(intraday_bars, key=lambda b: b.ts)

    # Find the first breakout in the allowed window.
    breakout_idx: int | None = None
    for i, bar in enumerate(bars):
        t = et_time(bar.ts)
        if t < earliest_breakout:
            continue
        if t >= breakout_cutoff:
            break
        if bar.close > prior_high:
            breakout_idx = i
            break
    if breakout_idx is None:
        return None
    breakout_bar = bars[breakout_idx]

    # Look forward for the failure: first bar closing back below prior_high
    # within the failure window.
    deadline = breakout_bar.ts + timedelta(minutes=failure_window_minutes)
    failure_bar: Bar | None = None
    for bar in bars[breakout_idx + 1:]:
        if bar.ts > deadline:
            break
        if bar.close < prior_high:
            failure_bar = bar
            break
    if failure_bar is None:
        return None  # breakout held

    if baseline_volume and baseline_volume > 0:
        ratio: float | None = failure_bar.volume / baseline_volume
    else:
        ratio = None

    return FailedBreakout(
        ticker=ticker,
        day=et_date(breakout_bar.ts),
        breakout_ts=breakout_bar.ts,
        breakout_price=breakout_bar.close,
        failure_ts=failure_bar.ts,
        failure_price=failure_bar.close,
        prior_high=prior_high,
        volume_ratio=ratio,
    )


# ---- Scanner runtime ------------------------------------------------------

class BarSource(Protocol):
    """Minimal data-layer contract the scanner depends on.

    Implemented in production by `data_service.DataService` (already has
    `get_underlying_bars` and a daily-bars analogue used elsewhere). Tests
    pass an in-memory fake so detection logic can be exercised without a
    live Alpaca connection.
    """

    def get_intraday_bars(self, ticker: str, day: date) -> list[Bar]: ...
    def get_prior_day_high(self, ticker: str, day: date) -> float | None: ...


class FailedBreakoutScanner:
    """One scan pass per call. Stateless across calls.

    The scan() method iterates the configured universe, runs detection per
    ticker, and writes any new events via `record_event`. Idempotency is
    enforced at the DB layer (UNIQUE on scanner_name + ticker + event_day);
    duplicate inserts on the same day silently no-op.

    `now` is injected — no `datetime.now()` here so tests stay deterministic.
    """

    SCANNER_NAME = "failed_breakout"

    def __init__(
        self,
        *,
        universe: Iterable[str],
        bar_source: BarSource,
        record_event: "RecordEventCallable",
        baseline_volume: float | None = None,
    ) -> None:
        self._universe = tuple(t.strip().upper() for t in universe if t.strip())
        self._bar_source = bar_source
        self._record_event = record_event
        self._baseline_volume = baseline_volume

    def scan(self, now: datetime) -> list[FailedBreakout]:
        """Run one scan pass. Returns the list of NEW events recorded.

        Already-recorded events for today are not re-emitted — the DB
        UNIQUE constraint blocks the insert and we treat that as "skip".

        Exceptions for a single ticker are caught and logged so one bad
        symbol can't poison the rest of the scan.
        """
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        today_et = et_date(now)
        new_events: list[FailedBreakout] = []

        for ticker in self._universe:
            try:
                bars = self._bar_source.get_intraday_bars(ticker, today_et)
                prior_high = self._bar_source.get_prior_day_high(ticker, today_et)
            except Exception:  # noqa: BLE001
                logger.exception("scanner: bar fetch failed for %s", ticker)
                continue

            if prior_high is None or not bars:
                continue

            try:
                event = find_failed_breakout(
                    ticker,
                    bars,
                    prior_high,
                    baseline_volume=self._baseline_volume,
                )
            except Exception:  # noqa: BLE001
                logger.exception("scanner: detection raised for %s", ticker)
                continue

            if event is None:
                continue

            try:
                inserted = self._record_event(self.SCANNER_NAME, event, now)
            except Exception:  # noqa: BLE001
                logger.exception("scanner: record_event raised for %s", ticker)
                continue

            if inserted:
                new_events.append(event)
                logger.info(
                    "scanner: failed_breakout %s prior=%.4f failure=%.4f vol_ratio=%s",
                    ticker, event.prior_high, event.failure_price,
                    f"{event.volume_ratio:.2f}" if event.volume_ratio else "n/a",
                )

        return new_events


# ---- Types for record_event callback --------------------------------------

class RecordEventCallable(Protocol):
    """The scanner is decoupled from the journal so tests can inject an
    in-memory recorder. Production wires this to `journal.insert_scanner_event`
    (added in the same change), which returns True on insert and False on
    UNIQUE conflict (the already-recorded fast path)."""

    def __call__(
        self,
        scanner_name: str,
        event: FailedBreakout,
        now: datetime,
    ) -> bool: ...


# ---- DataService adapter --------------------------------------------------

class DataServiceBarSource:
    """Adapts the production DataService to the BarSource protocol.

    Two responsibilities:
      1. Fetch today's 5-min intraday bars and filter to the ET trading day
         the scanner asks about — DataService returns recent bars regardless
         of date so we apply the date filter here.
      2. Resolve prior_day_high by fetching recent daily bars and selecting
         the most-recent bar BEFORE the target ET date.

    DataService stays unaware of the scanner; this adapter keeps the
    coupling one-directional and small.
    """

    def __init__(self, data_service: "Any") -> None:
        self._ds = data_service

    def get_intraday_bars(self, ticker: str, day: date) -> list[Bar]:
        # Use a "now" anchored on the target day so the data_service's
        # lookback computation makes sense even if the scanner runs after
        # market hours (e.g. an end-of-day backfill).
        now = datetime.combine(day, time(20, 0), tzinfo=ET).astimezone(timezone.utc)
        # 78 5-min bars in a 6.5h RTH session; ask for 80 to be safe.
        ohlc_bars = self._ds.get_underlying_bars(
            ticker, now, timeframe_minutes=5, limit=80
        )
        out: list[Bar] = []
        for b in ohlc_bars:
            if et_date(b.ts) != day:
                continue
            try:
                out.append(Bar(
                    ts=b.ts if b.ts.tzinfo else b.ts.replace(tzinfo=timezone.utc),
                    open=float(b.open),
                    high=float(b.high),
                    low=float(b.low),
                    close=float(b.close),
                    volume=float(b.volume),
                ))
            except Exception:  # noqa: BLE001
                continue
        return out

    def get_prior_day_high(self, ticker: str, day: date) -> float | None:
        now = datetime.combine(day, time(20, 0), tzinfo=ET).astimezone(timezone.utc)
        daily = self._ds.get_recent_daily_bars(ticker, now, limit=5)
        # daily is oldest-first; the most-recent bar strictly BEFORE `day`
        # is the prior trading day.
        candidates = [b for b in daily if et_date(b.ts) < day]
        if not candidates:
            return None
        return float(candidates[-1].high)


# ---- DEFAULT_UNIVERSE -----------------------------------------------------

# Starter list for Phase A. Mix of:
#   - tickers the existing X-account strategy has actually traded (we know
#     they're optionable and have non-trivial chains)
#   - popular small/mid-cap optionable names with recent retail flow
# Configurable via env in main.py — this is the fallback when no env set.
DEFAULT_UNIVERSE: tuple[str, ...] = (
    # Recent X-traded names — guaranteed optionable
    "ATI", "MRCY", "OOMA", "ELF", "DCTH", "XMTR", "POWI", "OSS",
    "AADX", "PRCH", "QNC", "INOD",
    # Popular retail / mid-cap names
    "PLTR", "SOFI", "RIVN", "NIO", "COIN", "ROKU", "SHOP",
    "BBAI", "MARA", "RIOT", "AFRM", "HOOD", "U",
)
