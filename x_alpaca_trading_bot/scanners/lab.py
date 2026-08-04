"""Scanner lab — multiple log-only hypotheses over one shared data pass.

Phase S1 of SCANNER_PROGRAM.md. The failed-breakout scanner proved the
methodology (hypothesize → log-only → score forward returns → promote);
this module generalizes the runtime so several hypotheses run side by
side without multiplying API calls: bars, prior-day levels, and the
volume baseline are fetched once per ticker per tick, then every enabled
detector sees the same data.

Detectors are pure functions (no I/O, no clocks) mirroring
failed_breakout.find_failed_breakout, and every one returns the same
FailedBreakout event shape so journal.insert_scanner_event and the
scanner_events schema stay untouched. Column semantics per hypothesis:

    scanner_name      breakout_* (trigger)        failure_* (confirm)      prior_high (ref level)
    ---------------   -------------------------   ----------------------   ----------------------
    failed_breakout   first close > prior high    close back < prior high  prior-day high
    vwap_reject       first close >= VWAP+0.3%    close back below VWAP    running VWAP at confirm
    gap_fade          session open (gap >= 2%)    close < opening-range lo opening-range low
    prior_low_break   first close < prior low     second close < prior low prior-day low

Generic names were considered and rejected: a rename would force a
migration plus dashboard/API churn for zero analytical value — the
evaluation script only needs failure_ts/failure_price (entry point) and
scanner_name (which hypothesis).

All detectors share the lab's morning trigger window (09:35–10:30 ET) —
the live scoring of the first 132 failed-breakout events showed
late-morning signals carried no edge, and until a hypothesis's own data
argues otherwise the conservative window applies to all of them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable, Iterable, Mapping, Protocol

from x_alpaca_trading_bot.scanners.failed_breakout import (
    DEFAULT_BREAKOUT_CUTOFF,
    DEFAULT_EARLIEST_BREAKOUT,
    DEFAULT_FAILURE_WINDOW_MINUTES,
    ET,
    Bar,
    DataServiceBarSource,
    FailedBreakout,
    RecordEventCallable,
    et_date,
    et_time,
    find_failed_breakout,
)

logger = logging.getLogger(__name__)


# ---- Shared prior-day context ---------------------------------------------

@dataclass(frozen=True)
class PriorDay:
    """The prior trading day's levels every detector keys off."""
    high: float
    low: float
    close: float


# A detector inspects one ticker-day and returns at most one event.
# Signature: (ticker, intraday_bars, prior_day, baseline_volume) -> event
Detector = Callable[
    [str, list[Bar], PriorDay, "float | None"], "FailedBreakout | None"
]


def _volume_ratio(bar: Bar, baseline: float | None) -> float | None:
    if baseline and baseline > 0:
        return bar.volume / baseline
    return None


# ---- Hypothesis: failed_breakout (adapter) --------------------------------

def detect_failed_breakout(
    ticker: str,
    bars: list[Bar],
    prior: PriorDay,
    baseline_volume: float | None,
) -> FailedBreakout | None:
    """The live Phase A hypothesis, unchanged — delegates to the original
    pure function so lab and standalone scanner can never drift apart."""
    return find_failed_breakout(
        ticker, bars, prior.high, baseline_volume=baseline_volume
    )


# ---- Hypothesis: vwap_reject ----------------------------------------------

# Trigger requires the close this far ABOVE running VWAP — a real push,
# not a wobble around the mean.
VWAP_STRENGTH_PCT = 0.003


def _running_vwap(bars: list[Bar]) -> list[float | None]:
    """Cumulative session VWAP after each bar (typical price × volume).
    None entries while cumulative volume is still zero."""
    out: list[float | None] = []
    pv = 0.0
    vol = 0.0
    for b in bars:
        typical = (b.high + b.low + b.close) / 3.0
        pv += typical * b.volume
        vol += b.volume
        out.append(pv / vol if vol > 0 else None)
    return out


def detect_vwap_reject(
    ticker: str,
    bars: list[Bar],
    prior: PriorDay,
    baseline_volume: float | None,
) -> FailedBreakout | None:
    """Morning strength above VWAP that gets rejected back below it.

    Trigger: first bar in the morning window closing >= VWAP * 1.003.
    Confirm: a bar within the failure window closing back below its own
    running VWAP. Ref level (prior_high column): running VWAP at confirm.
    """
    if not bars:
        return None
    bars = sorted(bars, key=lambda b: b.ts)
    vwaps = _running_vwap(bars)

    trigger_idx: int | None = None
    for i, bar in enumerate(bars):
        t = et_time(bar.ts)
        if t < DEFAULT_EARLIEST_BREAKOUT:
            continue
        if t >= DEFAULT_BREAKOUT_CUTOFF:
            break
        vwap = vwaps[i]
        if vwap is not None and bar.close >= vwap * (1 + VWAP_STRENGTH_PCT):
            trigger_idx = i
            break
    if trigger_idx is None:
        return None
    trigger_bar = bars[trigger_idx]

    deadline = trigger_bar.ts + timedelta(minutes=DEFAULT_FAILURE_WINDOW_MINUTES)
    for j in range(trigger_idx + 1, len(bars)):
        bar = bars[j]
        if bar.ts > deadline:
            break
        vwap = vwaps[j]
        if vwap is not None and bar.close < vwap:
            return FailedBreakout(
                ticker=ticker,
                day=et_date(trigger_bar.ts),
                breakout_ts=trigger_bar.ts,
                breakout_price=trigger_bar.close,
                failure_ts=bar.ts,
                failure_price=bar.close,
                prior_high=vwap,
                volume_ratio=_volume_ratio(bar, baseline_volume),
            )
    return None


# ---- Hypothesis: gap_fade --------------------------------------------------

GAP_MIN_PCT = 0.02          # open must gap >= 2% above prior close
OPENING_RANGE_BARS = 3      # first 15 minutes on 5-min bars
GAP_CONFIRM_CUTOFF = time(11, 30)


def detect_gap_fade(
    ticker: str,
    bars: list[Bar],
    prior: PriorDay,
    baseline_volume: float | None,
) -> FailedBreakout | None:
    """Gap-up that loses its opening range — trapped gap-buyers.

    Trigger: session opens >= 2% above prior close (trigger bar = first
    bar of the day). Confirm: a bar after the opening range, up to 11:30
    ET, closing below the opening-range low. Ref level: the OR low.

    The confirm window is wider than the lab's 10:30 trigger window on
    purpose — the trigger (the gap) happens AT the open; 10:30 would
    give the fade only an hour to develop. 11:30 mirrors the original
    failed-breakout geometry (last trigger 10:30 + 60-min window).
    """
    if prior.close <= 0 or len(bars) <= OPENING_RANGE_BARS:
        return None
    bars = sorted(bars, key=lambda b: b.ts)

    open_bar = bars[0]
    if open_bar.open < prior.close * (1 + GAP_MIN_PCT):
        return None

    or_low = min(b.low for b in bars[:OPENING_RANGE_BARS])
    for bar in bars[OPENING_RANGE_BARS:]:
        if et_time(bar.ts) >= GAP_CONFIRM_CUTOFF:
            break
        if bar.close < or_low:
            return FailedBreakout(
                ticker=ticker,
                day=et_date(open_bar.ts),
                breakout_ts=open_bar.ts,
                breakout_price=open_bar.open,
                failure_ts=bar.ts,
                failure_price=bar.close,
                prior_high=or_low,
                volume_ratio=_volume_ratio(bar, baseline_volume),
            )
    return None


# ---- Hypothesis: prior_low_break -------------------------------------------

def detect_prior_low_break(
    ticker: str,
    bars: list[Bar],
    prior: PriorDay,
    baseline_volume: float | None,
) -> FailedBreakout | None:
    """Momentum breakdown through the prior-day low.

    This is the lab's control hypothesis: the other three bet on
    trap-reversion; this one bets on plain downside follow-through. If
    it scores while the others don't (or vice versa), that tells us
    which regime the market is actually paying.

    Trigger: first morning-window bar closing below the prior-day low.
    Confirm: the NEXT bar also closes below it (filters one-bar stop
    sweeps). Ref level: the prior-day low.
    """
    if prior.low <= 0 or not bars:
        return None
    bars = sorted(bars, key=lambda b: b.ts)

    for i, bar in enumerate(bars):
        t = et_time(bar.ts)
        if t < DEFAULT_EARLIEST_BREAKOUT:
            continue
        if t >= DEFAULT_BREAKOUT_CUTOFF:
            break
        if bar.close < prior.low:
            if i + 1 >= len(bars):
                return None
            confirm = bars[i + 1]
            if confirm.close < prior.low:
                return FailedBreakout(
                    ticker=ticker,
                    day=et_date(bar.ts),
                    breakout_ts=bar.ts,
                    breakout_price=bar.close,
                    failure_ts=confirm.ts,
                    failure_price=confirm.close,
                    prior_high=prior.low,
                    volume_ratio=_volume_ratio(confirm, baseline_volume),
                )
            return None  # single-bar sweep — not our event
    return None


# ---- Registry --------------------------------------------------------------

HYPOTHESES: dict[str, Detector] = {
    "failed_breakout": detect_failed_breakout,
    "vwap_reject": detect_vwap_reject,
    "gap_fade": detect_gap_fade,
    "prior_low_break": detect_prior_low_break,
}


def _make_failed_breakout_detector(cutoff: time) -> Detector:
    """failed_breakout with the operator's SCANNER_BREAKOUT_CUTOFF bound
    in — only this hypothesis honors that env var (it predates the lab)."""
    def _detect(
        ticker: str,
        bars: list[Bar],
        prior: PriorDay,
        baseline_volume: float | None,
    ) -> FailedBreakout | None:
        return find_failed_breakout(
            ticker, bars, prior.high,
            breakout_cutoff=cutoff,
            baseline_volume=baseline_volume,
        )
    return _detect


def resolve_hypotheses(
    names: Iterable[str] | None,
    *,
    failed_breakout_cutoff: time | None = None,
) -> dict[str, Detector]:
    """Map hypothesis names to detectors; None means all. Unknown names
    raise so a typo'd SCANNER_HYPOTHESES fails startup loudly instead of
    silently scanning the wrong set."""
    if names is None:
        out = dict(HYPOTHESES)
    else:
        out = {}
        for raw in names:
            name = raw.strip().lower()
            if not name:
                continue
            if name not in HYPOTHESES:
                raise RuntimeError(
                    f"Unknown scanner hypothesis {name!r}. "
                    f"Valid: {', '.join(sorted(HYPOTHESES))}"
                )
            out[name] = HYPOTHESES[name]
        if not out:
            raise RuntimeError("SCANNER_HYPOTHESES resolved to an empty set")

    if failed_breakout_cutoff is not None and "failed_breakout" in out:
        out["failed_breakout"] = _make_failed_breakout_detector(
            failed_breakout_cutoff
        )
    return out


# ---- Lab bar source --------------------------------------------------------

class LabBarSource(Protocol):
    """Data contract for the lab runtime. Production: LabDataBarSource;
    tests: an in-memory fake."""

    def get_intraday_bars(self, ticker: str, day: date) -> list[Bar]: ...
    def get_prior_day_bar(self, ticker: str, day: date) -> PriorDay | None: ...
    def get_baseline_bar_volume(self, ticker: str, day: date) -> float | None: ...


class LabDataBarSource(DataServiceBarSource):
    """DataServiceBarSource plus full prior-day levels (the base class
    only exposes the prior high; gap_fade and prior_low_break need close
    and low from the same daily fetch)."""

    def get_prior_day_bar(self, ticker: str, day: date) -> PriorDay | None:
        now = datetime.combine(day, time(20, 0), tzinfo=ET).astimezone(timezone.utc)
        daily = self._ds.get_recent_daily_bars(ticker, now, limit=5)
        candidates = [b for b in daily if et_date(b.ts) < day]
        if not candidates:
            return None
        prior = candidates[-1]
        return PriorDay(
            high=float(prior.high),
            low=float(prior.low),
            close=float(prior.close),
        )


# ---- Lab runtime -----------------------------------------------------------

class ScannerLab:
    """One scan pass runs every enabled hypothesis over the universe.

    Drop-in replacement for FailedBreakoutScanner in the orchestrator's
    scanner thread: same scan(now) entrypoint, same record_event bridge
    (events are recorded under each hypothesis's own scanner_name, and
    the per-(scanner_name, ticker, event_day) DB UNIQUE keeps re-scans
    idempotent exactly as before).

    Data discipline: intraday bars, prior-day levels, and the volume
    baseline are fetched ONCE per ticker per pass and shared by all
    detectors — adding a hypothesis costs zero additional API calls.
    """

    def __init__(
        self,
        *,
        universe: Iterable[str],
        bar_source: LabBarSource,
        record_event: RecordEventCallable,
        hypotheses: Mapping[str, Detector] | None = None,
        baseline_volume: float | None = None,
    ) -> None:
        self._universe = tuple(t.strip().upper() for t in universe if t.strip())
        self._bar_source = bar_source
        self._record_event = record_event
        self._hypotheses = dict(hypotheses) if hypotheses is not None else dict(HYPOTHESES)
        self._baseline_volume = baseline_volume

    def scan(self, now: datetime) -> list[tuple[str, FailedBreakout]]:
        """Run one pass. Returns (scanner_name, event) pairs newly recorded."""
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        today_et = et_date(now)
        new_events: list[tuple[str, FailedBreakout]] = []

        for ticker in self._universe:
            try:
                bars = self._bar_source.get_intraday_bars(ticker, today_et)
                prior = self._bar_source.get_prior_day_bar(ticker, today_et)
            except Exception:  # noqa: BLE001
                logger.exception("lab: bar fetch failed for %s", ticker)
                continue
            if prior is None or not bars:
                continue

            baseline = self._baseline_volume
            if baseline is None:
                try:
                    baseline = self._bar_source.get_baseline_bar_volume(
                        ticker, today_et
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("lab: baseline fetch failed for %s", ticker)
                    baseline = None

            for name, detector in self._hypotheses.items():
                try:
                    event = detector(ticker, bars, prior, baseline)
                except Exception:  # noqa: BLE001
                    logger.exception("lab: %s raised for %s", name, ticker)
                    continue
                if event is None:
                    continue
                try:
                    inserted = self._record_event(name, event, now)
                except Exception:  # noqa: BLE001
                    logger.exception("lab: record_event raised for %s/%s", name, ticker)
                    continue
                if inserted:
                    new_events.append((name, event))
                    logger.info(
                        "lab: %s %s ref=%.4f confirm=%.4f vol_ratio=%s",
                        name, ticker, event.prior_high, event.failure_price,
                        f"{event.volume_ratio:.2f}" if event.volume_ratio else "n/a",
                    )
        return new_events
