#!/usr/bin/env python3
"""Backtest a failed-breakout (bull-trap) signal — the put-side hypothesis.

This is the inverse of the momentum-breakout test in `backtest_momentum.py`.
The momentum test asked "after a breakout, does the underlying keep going
up?" and answered: not enough to overcome options friction. This script
asks the opposite question:

    After a stock breaks above its prior-day high but then CLOSES back
    below it within a short window, does the underlying actually keep
    going down over the next 30-60 minutes?

If yes, that's the put-side edge — buy puts the moment the failure
confirms, profit as trapped longs cover. If no, then the put angle the
user asked about earlier ("what if we flip to puts?") doesn't have
edge either, and we need a fundamentally different hypothesis.

Signal definition
-----------------
For each trading day:
  1. Find the FIRST 5-minute bar that closes above the prior trading
     day's high, between 09:35 ET and a breakout cutoff (default 12:00).
  2. Watch the next N bars (default 12 = 60 minutes). If any of those
     bars CLOSES back below the prior-day high, that's the failure-
     confirmation bar — the event.
  3. The failure bar's close is the "entry" — we measure forward
     returns of the underlying from there.

Each event carries a volume ratio for the failure bar (vs the sample
mean intraday volume), so we can test whether a high-volume failure
(real distribution) has more edge than a low-volume fade (noise).

What we measure
---------------
Forward return of the underlying from the failure bar's close at:
    +30m, +60m, and to-close.

For a put-side signal we want NEGATIVE returns. Reported against the
unconditional benchmark (the same one as the momentum test) so we can
tell put edge apart from just "stocks drift down at lunch."

Usage
-----
    python research/backtest_failed_breakout.py
    python research/backtest_failed_breakout.py --symbols AAPL,NVDA --days 90
    python research/backtest_failed_breakout.py --breakout-cutoff 11 --failure-window 30
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import namedtuple
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

# Reuse the shared primitives from the momentum test — Bar, day grouping,
# prior-day highs, RTH filter, benchmark, Alpaca fetch, key loading.
SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

import backtest_momentum as bm  # noqa: E402


# A detected failed breakout. `breakout_bar` is the bar that closed above
# prior-day high; `failure_bar` is the first subsequent bar that closed
# back below it. `volume_ratio` is the failure bar's volume / sample mean.
FailedBreakout = namedtuple(
    "FailedBreakout",
    "day breakout_bar failure_bar prior_high volume_ratio",
)

Event = namedtuple("Event", "symbol breakout returns")

DEFAULT_SYMBOLS = bm.DEFAULT_SYMBOLS


# ---- Pure analysis ---------------------------------------------------------

def find_failed_breakouts(
    daily: list[bm.Bar],
    intraday: list[bm.Bar],
    *,
    earliest_breakout: time = time(9, 35),
    breakout_cutoff: time = time(12, 0),
    failure_window_minutes: int = 60,
    baseline_volume: float | None = None,
) -> list[FailedBreakout]:
    """First-of-day breakout above prior-day high that fails within the window.

    Per trading day:
      - Scan bars in [earliest_breakout, breakout_cutoff). The first one
        whose close > prior-day high is the candidate breakout.
      - Scan the next N minutes after the breakout (N = failure_window_
        minutes). The first bar in that window whose close < prior-day
        high is the failure-confirmation bar. That's our event.
      - If no bar fails inside the window, no event (the breakout held —
        success, not a fade).

    Only one event per day — we use the first breakout. Later breakouts
    on the same day are ignored because they happen in a different
    regime (the failed-then-recovered context).
    """
    highs = bm.prior_day_highs(daily)
    by_day = bm.group_by_day(intraday)

    if baseline_volume is None:
        vols = [b.volume for b in intraday if b.volume > 0]
        baseline_volume = (sum(vols) / len(vols)) if vols else None

    out: list[FailedBreakout] = []
    for day in sorted(by_day):
        prior_high = highs.get(day)
        if prior_high is None:
            continue
        bars = by_day[day]

        # Find the first breakout in the allowed window.
        breakout_idx: int | None = None
        for i, bar in enumerate(bars):
            t = bm.et_time(bar.ts)
            if t < earliest_breakout:
                continue
            if t >= breakout_cutoff:
                break
            if bar.close > prior_high:
                breakout_idx = i
                break
        if breakout_idx is None:
            continue
        breakout_bar = bars[breakout_idx]

        # Scan forward for the failure: first bar that closes back BELOW
        # the prior-day high, within failure_window_minutes.
        deadline = breakout_bar.ts + timedelta(minutes=failure_window_minutes)
        failure_bar = None
        for bar in bars[breakout_idx + 1:]:
            if bar.ts > deadline:
                break
            if bar.close < prior_high:
                failure_bar = bar
                break
        if failure_bar is None:
            continue  # breakout held — not our event

        ratio = (
            failure_bar.volume / baseline_volume
            if baseline_volume else None
        )
        out.append(FailedBreakout(
            day=day,
            breakout_bar=breakout_bar,
            failure_bar=failure_bar,
            prior_high=prior_high,
            volume_ratio=ratio,
        ))
    return out


def forward_returns_from_failure(
    event: FailedBreakout,
    day_bars: list[bm.Bar],
) -> bm.ForwardReturns:
    """Underlying return from the FAILURE bar's close at +30m/+60m/to-close.

    Negative = good for puts. Uses the same _close_at_or_after helper as
    the momentum test for an apples-to-apples comparison.
    """
    entry = event.failure_bar.close
    entry_ts = event.failure_bar.ts
    if entry <= 0 or not day_bars:
        return bm.ForwardReturns(None, None, None)

    after = [b for b in day_bars if b.ts > entry_ts]

    def ret(price: float | None) -> float | None:
        return (price / entry - 1.0) if price is not None else None

    px_30 = bm._close_at_or_after(after, entry_ts + timedelta(minutes=30))
    px_60 = bm._close_at_or_after(after, entry_ts + timedelta(minutes=60))
    px_close = after[-1].close if after else entry
    return bm.ForwardReturns(ret(px_30), ret(px_60), ret(px_close))


def analyze_symbol(
    symbol: str,
    daily: list[bm.Bar],
    intraday: list[bm.Bar],
    *,
    earliest_breakout: time = time(9, 35),
    breakout_cutoff: time = time(12, 0),
    failure_window_minutes: int = 60,
    baseline_volume: float | None = None,
) -> list[Event]:
    by_day = bm.group_by_day(intraday)
    events: list[Event] = []
    for fb in find_failed_breakouts(
        daily, intraday,
        earliest_breakout=earliest_breakout,
        breakout_cutoff=breakout_cutoff,
        failure_window_minutes=failure_window_minutes,
        baseline_volume=baseline_volume,
    ):
        events.append(
            Event(symbol, fb, forward_returns_from_failure(fb, by_day.get(fb.day, [])))
        )
    return events


# ---- Stats -----------------------------------------------------------------

def _mean(xs: list[float]) -> float | None:
    return statistics.fmean(xs) if xs else None


def _put_hit_rate(xs: list[float]) -> float | None:
    """Fraction of events where the underlying went DOWN — i.e. the put
    side won. For the momentum test this would be the call-side hit rate
    (return > 0); inverted here so we're measuring what we care about."""
    return (sum(1 for x in xs if x < 0) / len(xs)) if xs else None


def summarize(events: list[Event], *, vol_threshold: float) -> dict:
    def bucket(evs: list[Event]) -> dict:
        r30 = [e.returns.ret_30m for e in evs if e.returns.ret_30m is not None]
        r60 = [e.returns.ret_60m for e in evs if e.returns.ret_60m is not None]
        rcl = [e.returns.ret_close for e in evs if e.returns.ret_close is not None]
        return {
            "n": len(evs),
            "mean_30m": _mean(r30),
            "mean_60m": _mean(r60),
            "mean_close": _mean(rcl),
            "median_close": (statistics.median(rcl) if rcl else None),
            "put_hit_rate_close": _put_hit_rate(rcl),
        }

    high = [e for e in events if (e.breakout.volume_ratio or 0) >= vol_threshold]
    low = [e for e in events if (e.breakout.volume_ratio or 0) < vol_threshold]
    return {
        "all": bucket(events),
        "high_volume": bucket(high),
        "low_volume": bucket(low),
        "vol_threshold": vol_threshold,
    }


# ---- Reporting -------------------------------------------------------------

def _pct(x: float | None) -> str:
    return "   n/a" if x is None else f"{x * 100:+6.2f}%"


def _print_events(events: list[Event]) -> None:
    header = (
        f"{'symbol':<8}{'date':<12}{'breakout':<10}{'failure':<10}"
        f"{'prior_hi':>10}{'entry':>10}{'vol_x':>8}"
        f"{'+30m':>9}{'+60m':>9}{'close':>9}"
    )
    print(header)
    print("-" * len(header))
    for e in sorted(events, key=lambda x: (x.breakout.day, x.symbol)):
        fb = e.breakout
        bo_t = bm.et_time(fb.breakout_bar.ts).strftime("%H:%M")
        fa_t = bm.et_time(fb.failure_bar.ts).strftime("%H:%M")
        vol = "  n/a" if fb.volume_ratio is None else f"{fb.volume_ratio:5.2f}"
        print(
            f"{e.symbol:<8}{str(fb.day):<12}{bo_t:<10}{fa_t:<10}"
            f"{fb.prior_high:>10.2f}{fb.failure_bar.close:>10.2f}{vol:>8}"
            f"{_pct(e.returns.ret_30m):>9}{_pct(e.returns.ret_60m):>9}"
            f"{_pct(e.returns.ret_close):>9}"
        )


def _print_bucket(name: str, s: dict) -> None:
    if not s["n"]:
        print(f"  {name:<14} n=0")
        return
    print(
        f"  {name:<14} n={s['n']:<4} "
        f"mean +30m {_pct(s['mean_30m'])}  "
        f"+60m {_pct(s['mean_60m'])}  "
        f"close {_pct(s['mean_close'])}  "
        f"put-hit {s['put_hit_rate_close'] * 100:4.0f}%"
    )


def _print_summary(
    events: list[Event],
    summary: dict,
    benchmark: list[float],
) -> None:
    print()
    print("=" * 72)
    print(f"FAILED-BREAKOUT EVENTS: {len(events)}")
    print(f"volume bucket split at {summary['vol_threshold']:.2f}x average bar volume")
    print("(negative returns = good for puts)")
    print()
    _print_bucket("all", summary["all"])
    _print_bucket("high volume", summary["high_volume"])
    _print_bucket("low volume", summary["low_volume"])
    print()
    bench_mean = _mean(benchmark)
    if bench_mean is not None:
        # For the put benchmark we want the fraction of bars where the
        # close was LOWER than the bar — invert the hit-rate sign.
        bench_put_hit = (
            sum(1 for x in benchmark if x < 0) / len(benchmark)
            if benchmark else None
        )
        print(
            f"  {'benchmark':<14} n={len(benchmark):<4} "
            f"mean to-close {_pct(bench_mean)}  "
            f"put-hit {bench_put_hit * 100:4.0f}%"
        )
        print("  (benchmark = to-close return of any pre-cutoff bar)")
    edge = summary["all"]["mean_close"]
    if edge is not None and bench_mean is not None:
        # Put edge: signal is good when it's MORE NEGATIVE than benchmark.
        delta = bench_mean - edge
        verdict = "PUT EDGE" if delta > 0 else "no edge"
        print()
        print(
            f"  benchmark minus failure close: {_pct(delta)}  -> {verdict}"
        )
        print(
            "  (positive delta means failure events fall more than the "
            "average bar — that's the put-side edge)"
        )
    print("=" * 72)


def _events_to_json(events: list[Event], summary: dict, benchmark: list[float]) -> str:
    payload = {
        "summary": summary,
        "benchmark": {
            "n": len(benchmark),
            "mean_to_close": _mean(benchmark),
        },
        "events": [
            {
                "symbol": e.symbol,
                "date": str(e.breakout.day),
                "breakout_ts": e.breakout.breakout_bar.ts.isoformat(),
                "failure_ts": e.breakout.failure_bar.ts.isoformat(),
                "prior_high": e.breakout.prior_high,
                "entry_price": e.breakout.failure_bar.close,
                "volume_ratio": e.breakout.volume_ratio,
                "ret_30m": e.returns.ret_30m,
                "ret_60m": e.returns.ret_60m,
                "ret_close": e.returns.ret_close,
            }
            for e in events
        ],
    }
    return json.dumps(payload, indent=2)


# ---- CLI -------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--symbols",
        default=",".join(DEFAULT_SYMBOLS),
        help="Comma-separated tickers (default: liquid 15-name basket)",
    )
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument(
        "--earliest-breakout", type=int, default=9,
        help="Earliest ET hour for the breakout (default 9 = 09:35 effectively)",
    )
    parser.add_argument(
        "--breakout-cutoff", type=int, default=12,
        help="Latest ET hour for the breakout to fire (default 12 = noon)",
    )
    parser.add_argument(
        "--failure-window", type=int, default=60,
        help="Minutes after breakout to wait for failure (default 60)",
    )
    parser.add_argument(
        "--minutes", type=int, default=5,
        help="Intraday bar size in minutes (default 5)",
    )
    parser.add_argument(
        "--vol-threshold", type=float, default=1.5,
        help="Volume-ratio split between high/low buckets (default 1.5)",
    )
    parser.add_argument(
        "--vol-quantile", type=float, default=None,
        help="If set, split at this quantile of event volume ratios "
             "(e.g. 0.80 = top 20%% / top quintile). Overrides --vol-threshold.",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        print("error: no symbols", file=sys.stderr)
        return 1
    earliest = time(args.earliest_breakout, 35) if args.earliest_breakout == 9 else time(args.earliest_breakout, 0)
    cutoff = time(args.breakout_cutoff, 0)

    try:
        key, secret = bm._load_alpaca_keys()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    from alpaca.data.historical.stock import StockHistoricalDataClient

    client = StockHistoricalDataClient(api_key=key, secret_key=secret)

    end = datetime.now(timezone.utc)
    intraday_start = end - timedelta(days=args.days)
    daily_start = intraday_start - timedelta(days=10)

    all_events: list[Event] = []
    benchmark: list[float] = []
    for symbol in symbols:
        if not args.json:
            print(f"fetching {symbol} ...", file=sys.stderr)
        try:
            daily = bm.fetch_bars(client, symbol, start=daily_start, end=end, minutes=None)
            intraday = bm.fetch_bars(
                client, symbol, start=intraday_start, end=end, minutes=args.minutes
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  skip {symbol}: {exc}", file=sys.stderr)
            continue
        intraday = bm.regular_session(intraday)
        if not daily or not intraday:
            print(f"  skip {symbol}: no data", file=sys.stderr)
            continue
        # Per-symbol benchmark — same caveat as the momentum test.
        benchmark.extend(bm.unconditional_to_close(intraday, cutoff=time(15, 0)))
        all_events.extend(
            analyze_symbol(
                symbol, daily, intraday,
                earliest_breakout=earliest,
                breakout_cutoff=cutoff,
                failure_window_minutes=args.failure_window,
            )
        )

    if not all_events:
        print("No failed-breakout events found.", file=sys.stderr)
        return 1

    # Resolve the volume threshold. When --vol-quantile is set, compute
    # the split point from the empirical distribution of event volume
    # ratios so the "high volume" bucket is always exactly that quantile.
    if args.vol_quantile is not None:
        ratios = sorted(
            e.breakout.volume_ratio for e in all_events
            if e.breakout.volume_ratio is not None
        )
        if ratios:
            idx = max(0, min(len(ratios) - 1, int(args.vol_quantile * len(ratios))))
            vol_threshold = ratios[idx]
        else:
            vol_threshold = args.vol_threshold
    else:
        vol_threshold = args.vol_threshold

    summary = summarize(all_events, vol_threshold=vol_threshold)

    if args.json:
        print(_events_to_json(all_events, summary, benchmark))
    else:
        _print_events(all_events)
        _print_summary(all_events, summary, benchmark)
    return 0


if __name__ == "__main__":
    sys.exit(main())
