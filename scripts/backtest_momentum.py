#!/usr/bin/env python3
"""Backtest a momentum-breakout signal on the underlying stock.

Research script — NOT part of the live bot. Answers one question before we
spend effort on an options-trade simulator:

    After a stock breaks above its prior-day high on the way up, does the
    UNDERLYING actually keep going up over the next 30-60 minutes?

If the underlying has no forward edge, no options strategy layered on top of
it can work either, and we've saved the bigger build. If it does have edge,
the options layer is worth simulating next.

Signal definition
-----------------
For each trading day, the breakout event is the FIRST 5-minute bar whose
close prints above the prior trading day's high, provided it happens before
a cutoff (default 3:00 PM ET — we don't want to enter in the last hour).
Each event is tagged with a volume ratio: the breakout bar's volume divided
by the average 5-minute bar volume across the whole sample. We then split
events into a high-volume and low-volume bucket to see whether requiring a
volume surge adds edge.

What it measures
----------------
Forward return of the underlying from the breakout bar's close at:
    +30m, +60m, and to-the-close.
Reported against an unconditional benchmark — the average to-close return of
ANY pre-cutoff bar — so we can tell breakout edge apart from "stocks drift
up in a bull market."

Data
----
Daily + 5-minute bars from Alpaca's free IEX feed. Needs ALPACA_API_KEY and
ALPACA_SECRET_KEY in the environment (or a .env file in the repo root).

Usage
-----
    python scripts/backtest_momentum.py
    python scripts/backtest_momentum.py --symbols AAPL,NVDA,AMD --days 90
    python scripts/backtest_momentum.py --cutoff 14 --vol-threshold 2.0 --json
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import namedtuple
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# A bar of OHLCV data. ts is a timezone-aware UTC datetime; prices/volume floats.
Bar = namedtuple("Bar", "ts open high low close volume")

# A detected breakout. breakout_bar is the 5-min bar that closed above the
# prior-day high; volume_ratio is that bar's volume / sample-average bar volume.
Breakout = namedtuple("Breakout", "day breakout_bar prior_high volume_ratio")

# Forward returns of the underlying from a breakout, as fractions (0.01 == +1%).
ForwardReturns = namedtuple("ForwardReturns", "ret_30m ret_60m ret_close")

# One fully analysed event — what the report iterates over.
Event = namedtuple("Event", "symbol breakout returns")

# A liquid, options-active default basket. Override with --symbols.
DEFAULT_SYMBOLS = (
    "AAPL", "MSFT", "NVDA", "AMD", "TSLA", "AMZN", "META", "GOOGL",
    "NFLX", "AVGO", "CRM", "QCOM", "INTC", "MU", "BABA",
)


# ---- Pure analysis functions (no network — unit-tested) --------------------

def et_date(ts: datetime) -> date:
    """Calendar date of a UTC timestamp in US/Eastern."""
    return ts.astimezone(ET).date()


def et_time(ts: datetime) -> time:
    """Wall-clock time of a UTC timestamp in US/Eastern."""
    return ts.astimezone(ET).timetz().replace(tzinfo=None)


def prior_day_highs(daily: list[Bar]) -> dict[date, float]:
    """Map each trading day -> the *previous* trading day's high.

    The first day in the series has no predecessor and is omitted.
    """
    ordered = sorted(daily, key=lambda b: b.ts)
    highs: dict[date, float] = {}
    for i in range(1, len(ordered)):
        highs[et_date(ordered[i].ts)] = ordered[i - 1].high
    return highs


def regular_session(
    bars: list[Bar],
    *,
    open_: time = time(9, 30),
    close_: time = time(16, 0),
) -> list[Bar]:
    """Keep only regular-trading-hours bars (default 09:30-16:00 ET).

    Alpaca's minute feed includes pre/post-market bars; a breakout signal
    should never fire on a thin pre-market print, so we drop them here.
    """
    return [b for b in bars if open_ <= et_time(b.ts) < close_]


def group_by_day(intraday: list[Bar]) -> dict[date, list[Bar]]:
    """Bucket intraday bars by ET trading day; each bucket sorted by time."""
    buckets: dict[date, list[Bar]] = {}
    for bar in intraday:
        buckets.setdefault(et_date(bar.ts), []).append(bar)
    for bars in buckets.values():
        bars.sort(key=lambda b: b.ts)
    return buckets


def find_breakouts(
    daily: list[Bar],
    intraday: list[Bar],
    *,
    cutoff: time = time(15, 0),
    baseline_volume: float | None = None,
) -> list[Breakout]:
    """First pre-cutoff 5-min bar each day to close above the prior-day high.

    `baseline_volume` is the divisor for each event's volume ratio. When None,
    it is the mean volume of every intraday bar supplied — a stable, sample-wide
    yardstick so high- vs low-volume buckets are comparable.
    """
    highs = prior_day_highs(daily)
    by_day = group_by_day(intraday)

    if baseline_volume is None:
        vols = [b.volume for b in intraday if b.volume > 0]
        baseline_volume = (sum(vols) / len(vols)) if vols else None

    breakouts: list[Breakout] = []
    for day in sorted(by_day):
        prior_high = highs.get(day)
        if prior_high is None:
            continue  # no prior-day high on record — can't evaluate
        for bar in by_day[day]:
            if et_time(bar.ts) >= cutoff:
                break  # bars are time-sorted; nothing past the cutoff qualifies
            if bar.close > prior_high:
                ratio = (
                    bar.volume / baseline_volume
                    if baseline_volume
                    else None
                )
                breakouts.append(Breakout(day, bar, prior_high, ratio))
                break  # only the first breakout of the day
    return breakouts


def _close_at_or_after(bars: list[Bar], target: datetime) -> float | None:
    """Close of the first bar at/after `target`. None if no such bar."""
    for bar in bars:
        if bar.ts >= target:
            return bar.close
    return None


def forward_returns(breakout: Breakout, day_bars: list[Bar]) -> ForwardReturns:
    """Underlying return from the breakout close at +30m, +60m, and to-close."""
    entry = breakout.breakout_bar.close
    entry_ts = breakout.breakout_bar.ts
    if entry <= 0 or not day_bars:
        return ForwardReturns(None, None, None)

    after = [b for b in day_bars if b.ts > entry_ts]

    def ret(price: float | None) -> float | None:
        return (price / entry - 1.0) if price is not None else None

    px_30 = _close_at_or_after(after, entry_ts + timedelta(minutes=30))
    px_60 = _close_at_or_after(after, entry_ts + timedelta(minutes=60))
    px_close = after[-1].close if after else entry
    return ForwardReturns(ret(px_30), ret(px_60), ret(px_close))


def unconditional_to_close(
    intraday: list[Bar],
    *,
    cutoff: time = time(15, 0),
) -> list[float]:
    """Benchmark: to-close return of EVERY pre-cutoff bar, all days pooled.

    This is the move you'd capture entering at a random time — the bar to
    beat. A breakout signal only has edge if it clears this.
    """
    rets: list[float] = []
    for bars in group_by_day(intraday).values():
        if not bars:
            continue
        day_close = bars[-1].close
        for bar in bars:
            if et_time(bar.ts) >= cutoff:
                break
            if bar.close > 0:
                rets.append(day_close / bar.close - 1.0)
    return rets


def analyze_symbol(
    symbol: str,
    daily: list[Bar],
    intraday: list[Bar],
    *,
    cutoff: time = time(15, 0),
    baseline_volume: float | None = None,
) -> list[Event]:
    """Find breakouts for one symbol and attach forward returns."""
    by_day = group_by_day(intraday)
    events: list[Event] = []
    for bo in find_breakouts(
        daily, intraday, cutoff=cutoff, baseline_volume=baseline_volume
    ):
        events.append(
            Event(symbol, bo, forward_returns(bo, by_day.get(bo.day, [])))
        )
    return events


# ---- Stats -----------------------------------------------------------------

def _mean(xs: list[float]) -> float | None:
    return statistics.fmean(xs) if xs else None


def _hit_rate(xs: list[float]) -> float | None:
    return (sum(1 for x in xs if x > 0) / len(xs)) if xs else None


def summarize(events: list[Event], *, vol_threshold: float) -> dict:
    """Aggregate stats overall and split by the volume-ratio bucket."""

    def bucket_stats(evs: list[Event]) -> dict:
        r30 = [e.returns.ret_30m for e in evs if e.returns.ret_30m is not None]
        r60 = [e.returns.ret_60m for e in evs if e.returns.ret_60m is not None]
        rcl = [e.returns.ret_close for e in evs if e.returns.ret_close is not None]
        return {
            "n": len(evs),
            "mean_30m": _mean(r30),
            "mean_60m": _mean(r60),
            "mean_close": _mean(rcl),
            "median_close": (statistics.median(rcl) if rcl else None),
            "hit_rate_close": _hit_rate(rcl),
        }

    high = [e for e in events if (e.breakout.volume_ratio or 0) >= vol_threshold]
    low = [e for e in events if (e.breakout.volume_ratio or 0) < vol_threshold]
    return {
        "all": bucket_stats(events),
        "high_volume": bucket_stats(high),
        "low_volume": bucket_stats(low),
        "vol_threshold": vol_threshold,
    }


# ---- Alpaca data fetch (network) -------------------------------------------

def _to_bars(alpaca_bars) -> list[Bar]:
    """Convert Alpaca SDK bar objects to our Bar namedtuple."""
    out: list[Bar] = []
    for b in alpaca_bars:
        out.append(
            Bar(
                ts=b.timestamp,
                open=float(b.open),
                high=float(b.high),
                low=float(b.low),
                close=float(b.close),
                volume=float(b.volume),
            )
        )
    return out


def fetch_bars(
    client,
    symbol: str,
    *,
    start: datetime,
    end: datetime,
    minutes: int | None,
):
    """Fetch daily (minutes=None) or N-minute bars for one symbol via IEX."""
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    tf = TimeFrame.Day if minutes is None else TimeFrame(minutes, TimeFrameUnit.Minute)
    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=tf,
        start=start,
        end=end,
        feed=DataFeed.IEX,
    )
    resp = client.get_stock_bars(req)
    data = getattr(resp, "data", {}) or {}
    return _to_bars(data.get(symbol, []))


def _load_alpaca_keys() -> tuple[str, str]:
    """API key + secret from the environment, loading a repo .env if present."""
    key = os.environ.get("ALPACA_API_KEY")
    secret = os.environ.get("ALPACA_SECRET_KEY")
    if not (key and secret):
        try:
            from dotenv import load_dotenv

            load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)
            key = os.environ.get("ALPACA_API_KEY")
            secret = os.environ.get("ALPACA_SECRET_KEY")
        except Exception:  # noqa: BLE001
            pass
    if not (key and secret):
        raise RuntimeError(
            "ALPACA_API_KEY / ALPACA_SECRET_KEY not set. Export them or add a .env."
        )
    return key, secret


# ---- Reporting -------------------------------------------------------------

def _pct(x: float | None) -> str:
    return "   n/a" if x is None else f"{x * 100:+6.2f}%"


def _print_events(events: list[Event]) -> None:
    header = (
        f"{'symbol':<8}{'date':<12}{'entry(ET)':<11}"
        f"{'prior_hi':>10}{'entry':>10}{'vol_x':>8}"
        f"{'+30m':>9}{'+60m':>9}{'close':>9}"
    )
    print(header)
    print("-" * len(header))
    for e in sorted(events, key=lambda x: (x.breakout.day, x.symbol)):
        bo = e.breakout
        entry_time = et_time(bo.breakout_bar.ts).strftime("%H:%M")
        vol = "  n/a" if bo.volume_ratio is None else f"{bo.volume_ratio:5.2f}"
        print(
            f"{e.symbol:<8}{str(bo.day):<12}{entry_time:<11}"
            f"{bo.prior_high:>10.2f}{bo.breakout_bar.close:>10.2f}{vol:>8}"
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
        f"hit {s['hit_rate_close'] * 100:4.0f}%"
    )


def _print_summary(
    events: list[Event],
    summary: dict,
    benchmark: list[float],
) -> None:
    print()
    print("=" * 72)
    print(f"BREAKOUT EVENTS: {len(events)}")
    print(f"volume bucket split at {summary['vol_threshold']:.2f}x average bar volume")
    print()
    _print_bucket("all", summary["all"])
    _print_bucket("high volume", summary["high_volume"])
    _print_bucket("low volume", summary["low_volume"])
    print()
    bench_mean = _mean(benchmark)
    bench_hit = _hit_rate(benchmark)
    if bench_mean is not None:
        print(
            f"  {'benchmark':<14} n={len(benchmark):<4} "
            f"mean to-close {_pct(bench_mean)}  hit {bench_hit * 100:4.0f}%"
        )
        print("  (benchmark = to-close return of any pre-cutoff bar — the bar to beat)")
    edge = summary["all"]["mean_close"]
    if edge is not None and bench_mean is not None:
        delta = edge - bench_mean
        verdict = "EDGE" if delta > 0 else "no edge"
        print()
        print(f"  breakout close return minus benchmark: {_pct(delta)}  -> {verdict}")
    print("=" * 72)


def _events_to_json(events: list[Event], summary: dict, benchmark: list[float]) -> str:
    payload = {
        "summary": summary,
        "benchmark": {
            "n": len(benchmark),
            "mean_to_close": _mean(benchmark),
            "hit_rate": _hit_rate(benchmark),
        },
        "events": [
            {
                "symbol": e.symbol,
                "date": str(e.breakout.day),
                "entry_ts": e.breakout.breakout_bar.ts.isoformat(),
                "prior_high": e.breakout.prior_high,
                "entry_price": e.breakout.breakout_bar.close,
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
        help="Comma-separated tickers (default: a liquid 15-name basket)",
    )
    parser.add_argument(
        "--days", type=int, default=60,
        help="Calendar-day lookback for intraday bars (default 60)",
    )
    parser.add_argument(
        "--cutoff", type=int, default=15,
        help="Latest ET hour to accept a breakout entry (default 15 = 3 PM)",
    )
    parser.add_argument(
        "--minutes", type=int, default=5,
        help="Intraday bar size in minutes (default 5)",
    )
    parser.add_argument(
        "--vol-threshold", type=float, default=1.5,
        help="Volume-ratio split between high/low buckets (default 1.5)",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of tables")
    args = parser.parse_args(argv)

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        print("error: no symbols", file=sys.stderr)
        return 1
    cutoff = time(args.cutoff, 0)

    try:
        key, secret = _load_alpaca_keys()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    from alpaca.data.historical.stock import StockHistoricalDataClient

    client = StockHistoricalDataClient(api_key=key, secret_key=secret)

    end = datetime.now(timezone.utc)
    intraday_start = end - timedelta(days=args.days)
    # Daily bars need extra lead time so day 1 of intraday has a prior-day high.
    daily_start = intraday_start - timedelta(days=10)

    all_events: list[Event] = []
    benchmark: list[float] = []
    for symbol in symbols:
        if not args.json:
            print(f"fetching {symbol} ...", file=sys.stderr)
        try:
            daily = fetch_bars(client, symbol, start=daily_start, end=end, minutes=None)
            intraday = fetch_bars(
                client, symbol, start=intraday_start, end=end, minutes=args.minutes
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  skip {symbol}: {exc}", file=sys.stderr)
            continue
        intraday = regular_session(intraday)
        if not daily or not intraday:
            print(f"  skip {symbol}: no data", file=sys.stderr)
            continue
        # Benchmark accumulates per-symbol — pooling raw bars across symbols
        # would divide one ticker's price by another's.
        benchmark.extend(unconditional_to_close(intraday, cutoff=cutoff))
        all_events.extend(
            analyze_symbol(symbol, daily, intraday, cutoff=cutoff)
        )

    if not all_events:
        print("No breakout events found.", file=sys.stderr)
        return 1

    summary = summarize(all_events, vol_threshold=args.vol_threshold)

    if args.json:
        print(_events_to_json(all_events, summary, benchmark))
    else:
        _print_events(all_events)
        _print_summary(all_events, summary, benchmark)
    return 0


if __name__ == "__main__":
    sys.exit(main())
