#!/usr/bin/env python3
"""Score the live scanner's logged events with forward returns.

The failed-breakout scanner (Phase A) logs detections to scanner_events
but records no outcomes. This script closes that loop: for every logged
event it fetches the day's 5-minute bars from Alpaca and measures the
underlying's forward return from the failure price at +30m, +60m, and
to-close — the same yardsticks as research/backtest_failed_breakout.py,
so the live sample reads apples-to-apples against the backtest.

It also computes the volume ratio retroactively (failure-bar volume vs
the ticker's sample-mean bar volume). The live scanner currently runs
without a baseline_volume, so volume_ratio is NULL on every logged row —
but the backtest's edge concentrated in the high-volume bucket, making
this the split that decides whether Phase B is worth wiring up.

Per research/README.md: read-only against production tables, no imports
from x_alpaca_trading_bot/, no orders.

Usage
-----
    export DATABASE_URL=postgresql://...   # or rely on repo .env
    python3 research/evaluate_scanner_events.py
    python3 research/evaluate_scanner_events.py --vol-quantile 0.8 --json
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

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

import backtest_momentum as bm  # noqa: E402  (shared fetch + analysis helpers)

try:
    import psycopg
except ImportError:
    print("psycopg not installed. Run: pip install 'psycopg[binary]'", file=sys.stderr)
    sys.exit(1)


# One scanner_events row joined with its computed outcome.
Scored = namedtuple(
    "Scored",
    "ticker day breakout_ts failure_ts failure_price prior_high "
    "volume_ratio ret_30m ret_60m ret_close",
)


def _load_database_url(cli_value: str | None) -> str | None:
    if cli_value:
        return cli_value
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    try:
        from dotenv import load_dotenv

        load_dotenv(SCRIPTS_DIR.parent / ".env", override=False)
        return os.environ.get("DATABASE_URL")
    except Exception:  # noqa: BLE001
        return None


def fetch_events(database_url: str, scanner: str) -> list[dict]:
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT ticker, event_day, breakout_ts, failure_ts,
                   failure_price::float8, prior_high::float8
            FROM scanner_events
            WHERE scanner_name = %s
            ORDER BY ticker, event_day
            """,
            (scanner,),
        )
        rows = cur.fetchall()
    return [
        {
            "ticker": r[0],
            "day": r[1],
            "breakout_ts": r[2],
            "failure_ts": r[3],
            "failure_price": r[4],
            "prior_high": r[5],
        }
        for r in rows
    ]


def score_ticker(
    client,
    ticker: str,
    events: list[dict],
    *,
    minutes: int,
    benchmark: list[float],
) -> list[Scored]:
    """Fetch one ticker's bars covering all its events and score each one.

    Entry price is the DB's failure_price — what the scanner actually saw —
    not the refetched bar close; IEX consolidation quirks shouldn't move
    the entry. Bars only supply the forward path and the volume baseline.
    """
    first = min(e["day"] for e in events)
    last = max(e["day"] for e in events)
    start = datetime.combine(first, time(0, 0), tzinfo=timezone.utc) - timedelta(days=1)
    end = datetime.combine(last + timedelta(days=1), time(23, 59), tzinfo=timezone.utc)

    intraday = bm.regular_session(
        bm.fetch_bars(client, ticker, start=start, end=end, minutes=minutes)
    )
    if not intraday:
        return []
    by_day = bm.group_by_day(intraday)

    vols = [b.volume for b in intraday if b.volume > 0]
    baseline = (sum(vols) / len(vols)) if vols else None

    benchmark.extend(bm.unconditional_to_close(intraday, cutoff=time(15, 0)))

    out: list[Scored] = []
    for ev in events:
        day_bars = by_day.get(ev["day"], [])
        if not day_bars:
            continue
        entry = ev["failure_price"]
        failure_ts = ev["failure_ts"]
        if entry is None or entry <= 0 or failure_ts is None:
            continue

        after = [b for b in day_bars if b.ts > failure_ts]

        def ret(price: float | None) -> float | None:
            return (price / entry - 1.0) if price is not None else None

        px_30 = bm._close_at_or_after(after, failure_ts + timedelta(minutes=30))
        px_60 = bm._close_at_or_after(after, failure_ts + timedelta(minutes=60))
        px_close = after[-1].close if after else None

        # Retro volume ratio: the bar stamped at failure_ts (bar-open label
        # in Alpaca) against the ticker's sample-mean bar volume.
        failure_bar = next((b for b in day_bars if b.ts == failure_ts), None)
        vol_ratio = (
            failure_bar.volume / baseline
            if (failure_bar is not None and baseline) else None
        )

        out.append(Scored(
            ticker=ticker,
            day=ev["day"],
            breakout_ts=ev["breakout_ts"],
            failure_ts=failure_ts,
            failure_price=entry,
            prior_high=ev["prior_high"],
            volume_ratio=vol_ratio,
            ret_30m=ret(px_30),
            ret_60m=ret(px_60),
            ret_close=ret(px_close),
        ))
    return out


# ---- Stats -----------------------------------------------------------------

def _mean(xs: list[float]) -> float | None:
    return statistics.fmean(xs) if xs else None


def _put_hit(xs: list[float]) -> float | None:
    return (sum(1 for x in xs if x < 0) / len(xs)) if xs else None


def bucket(evs: list[Scored]) -> dict:
    r30 = [e.ret_30m for e in evs if e.ret_30m is not None]
    r60 = [e.ret_60m for e in evs if e.ret_60m is not None]
    rcl = [e.ret_close for e in evs if e.ret_close is not None]
    return {
        "n": len(evs),
        "mean_30m": _mean(r30),
        "mean_60m": _mean(r60),
        "mean_close": _mean(rcl),
        "median_close": (statistics.median(rcl) if rcl else None),
        "put_hit_rate_close": _put_hit(rcl),
    }


def _pct(x: float | None) -> str:
    return "   n/a" if x is None else f"{x * 100:+6.2f}%"


def _print_bucket(name: str, s: dict) -> None:
    if not s["n"]:
        print(f"  {name:<16} n=0")
        return
    hit = s["put_hit_rate_close"]
    hit_str = " n/a" if hit is None else f"{hit * 100:4.0f}%"
    print(
        f"  {name:<16} n={s['n']:<4} "
        f"mean +30m {_pct(s['mean_30m'])}  "
        f"+60m {_pct(s['mean_60m'])}  "
        f"close {_pct(s['mean_close'])}  "
        f"put-hit {hit_str}"
    )


# ---- CLI -------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--scanner", default="failed_breakout")
    parser.add_argument("--database-url", default=None)
    parser.add_argument(
        "--events-json", default=None,
        help="Path to a JSON array of scanner_events rows (ticker, "
             "event_day, breakout_ts, failure_ts, failure_price, "
             "prior_high). Skips the DB read — useful when the prod DB "
             "isn't reachable from this machine.",
    )
    parser.add_argument("--minutes", type=int, default=5)
    parser.add_argument(
        "--vol-quantile", type=float, default=0.80,
        help="High-volume bucket = events at/above this quantile of retro "
             "volume ratios (default 0.80 = top quintile, the backtest's "
             "edge bucket)",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        key, secret = bm._load_alpaca_keys()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.events_json:
        raw = json.loads(Path(args.events_json).read_text())
        events = [
            {
                "ticker": r["ticker"],
                "day": date.fromisoformat(r["event_day"]),
                "breakout_ts": datetime.fromisoformat(r["breakout_ts"]),
                "failure_ts": datetime.fromisoformat(r["failure_ts"]),
                "failure_price": float(r["failure_price"]),
                "prior_high": float(r["prior_high"]),
            }
            for r in raw
        ]
    else:
        database_url = _load_database_url(args.database_url)
        if not database_url:
            print("DATABASE_URL not set (env, .env, or --database-url)",
                  file=sys.stderr)
            return 2
        events = fetch_events(database_url, args.scanner)
    if not events:
        print("No scanner events found.", file=sys.stderr)
        return 1

    from alpaca.data.historical.stock import StockHistoricalDataClient

    client = StockHistoricalDataClient(api_key=key, secret_key=secret)

    by_ticker: dict[str, list[dict]] = {}
    for ev in events:
        by_ticker.setdefault(ev["ticker"], []).append(ev)

    scored: list[Scored] = []
    benchmark: list[float] = []
    for ticker in sorted(by_ticker):
        if not args.json:
            print(f"fetching {ticker} ...", file=sys.stderr)
        try:
            scored.extend(score_ticker(
                client, ticker, by_ticker[ticker],
                minutes=args.minutes, benchmark=benchmark,
            ))
        except Exception as exc:  # noqa: BLE001
            print(f"  skip {ticker}: {exc}", file=sys.stderr)

    if not scored:
        print("No events could be scored (no bar data).", file=sys.stderr)
        return 1

    ratios = sorted(e.volume_ratio for e in scored if e.volume_ratio is not None)
    vol_threshold: float | None = None
    if ratios:
        idx = max(0, min(len(ratios) - 1, int(args.vol_quantile * len(ratios))))
        vol_threshold = ratios[idx]

    high = [e for e in scored
            if vol_threshold is not None
            and (e.volume_ratio or 0) >= vol_threshold]
    low = [e for e in scored if e not in high]

    summary = {
        "all": bucket(scored),
        "high_volume": bucket(high),
        "low_volume": bucket(low),
        "vol_threshold": vol_threshold,
        "vol_quantile": args.vol_quantile,
        "benchmark": {"n": len(benchmark), "mean_to_close": _mean(benchmark)},
    }

    if args.json:
        payload = {
            "summary": summary,
            "events": [
                {
                    "ticker": e.ticker,
                    "date": str(e.day),
                    "failure_ts": e.failure_ts.isoformat(),
                    "entry_price": e.failure_price,
                    "volume_ratio": e.volume_ratio,
                    "ret_30m": e.ret_30m,
                    "ret_60m": e.ret_60m,
                    "ret_close": e.ret_close,
                }
                for e in scored
            ],
        }
        print(json.dumps(payload, indent=2, default=str))
        return 0

    # Per-event table
    header = (
        f"{'ticker':<8}{'date':<12}{'fail_et':<9}{'entry':>9}{'vol_x':>7}"
        f"{'+30m':>9}{'+60m':>9}{'close':>9}"
    )
    print()
    print(header)
    print("-" * len(header))
    for e in sorted(scored, key=lambda x: (x.day, x.ticker)):
        fa_t = bm.et_time(e.failure_ts).strftime("%H:%M")
        vol = "  n/a" if e.volume_ratio is None else f"{e.volume_ratio:5.2f}"
        print(
            f"{e.ticker:<8}{str(e.day):<12}{fa_t:<9}{e.failure_price:>9.2f}{vol:>7}"
            f"{_pct(e.ret_30m):>9}{_pct(e.ret_60m):>9}{_pct(e.ret_close):>9}"
        )

    # Per-ticker to-close means — which names carry the edge live.
    per_ticker: dict[str, list[float]] = {}
    for e in scored:
        if e.ret_close is not None:
            per_ticker.setdefault(e.ticker, []).append(e.ret_close)
    print()
    print(f"{'ticker':<8}{'n':>4}{'mean_close':>12}{'put_hit':>9}")
    print("-" * 33)
    for tkr in sorted(per_ticker, key=lambda t: _mean(per_ticker[t]) or 0):
        rets = per_ticker[tkr]
        print(
            f"{tkr:<8}{len(rets):>4}{_pct(_mean(rets)):>12}"
            f"{(_put_hit(rets) or 0) * 100:>8.0f}%"
        )

    print()
    print("=" * 72)
    print(f"LIVE SCANNER EVENTS SCORED: {len(scored)} "
          f"(of {len(events)} logged)")
    if vol_threshold is not None:
        print(f"volume bucket split at {vol_threshold:.2f}x "
              f"(top {(1 - args.vol_quantile) * 100:.0f}% of retro ratios)")
    print("(negative returns = the put-side edge is real)")
    print()
    _print_bucket("all", summary["all"])
    _print_bucket("high volume", summary["high_volume"])
    _print_bucket("low volume", summary["low_volume"])
    bench = summary["benchmark"]
    if bench["mean_to_close"] is not None:
        print(
            f"  {'benchmark':<16} n={bench['n']:<4} "
            f"mean to-close {_pct(bench['mean_to_close'])}"
        )
        edge = summary["all"]["mean_close"]
        if edge is not None:
            delta = bench["mean_to_close"] - edge
            verdict = "PUT EDGE" if delta > 0 else "no edge"
            print()
            print(f"  benchmark minus event close: {_pct(delta)}  -> {verdict}")
            print("  (backtest reference: +0.62% to-close on the same delta)")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
