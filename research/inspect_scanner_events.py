#!/usr/bin/env python3
"""Inspect what the failed-breakout scanner has logged.

Phase A is observation-only — this script gives you the most recent
events plus a small summary so you can decide whether the scanner is
catching real setups before wiring it to take trades in Phase B.

Per research/README.md: read-only against production tables, no
imports from x_alpaca_trading_bot/, no orchestration.

Usage
-----
    export DATABASE_URL=postgresql://...
    python3 research/inspect_scanner_events.py
    python3 research/inspect_scanner_events.py --since-days 14
    python3 research/inspect_scanner_events.py --ticker AAPL
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

try:
    import psycopg
except ImportError:
    print("psycopg not installed. Run: pip install 'psycopg[binary]'", file=sys.stderr)
    sys.exit(1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since-days", type=int, default=7,
                        help="Only show events from the last N days (default: 7)")
    parser.add_argument("--ticker", type=str, default=None,
                        help="Filter to a single ticker")
    parser.add_argument("--scanner", type=str, default="failed_breakout",
                        help="Filter by scanner_name (default: failed_breakout)")
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    args = parser.parse_args(argv)

    if not args.database_url:
        print("DATABASE_URL not set", file=sys.stderr)
        return 2

    filters = ["scanner_name = %s", "detected_at > NOW() - INTERVAL %s"]
    params: list = [args.scanner, f"{args.since_days} days"]
    if args.ticker:
        filters.append("ticker = %s")
        params.append(args.ticker.upper())
    where = " AND ".join(filters)

    with psycopg.connect(args.database_url) as conn, conn.cursor() as cur:
        # Summary
        cur.execute(
            f"""
            SELECT COUNT(*),
                   COUNT(DISTINCT ticker),
                   COUNT(DISTINCT event_day),
                   MIN(detected_at), MAX(detected_at)
            FROM scanner_events
            WHERE {where}
            """,
            params,
        )
        n, unique_tickers, unique_days, first, last = cur.fetchone() or (0, 0, 0, None, None)

        # Per-ticker counts
        cur.execute(
            f"""
            SELECT ticker, COUNT(*) AS events,
                   ROUND(AVG(volume_ratio)::numeric, 2) AS avg_vol_ratio
            FROM scanner_events
            WHERE {where}
            GROUP BY ticker
            ORDER BY events DESC, ticker ASC
            LIMIT 25
            """,
            params,
        )
        per_ticker = cur.fetchall()

        # Recent rows
        cur.execute(
            f"""
            SELECT detected_at, event_day, ticker,
                   ROUND(prior_high::numeric, 4) AS prior_high,
                   ROUND(breakout_price::numeric, 4) AS breakout_px,
                   ROUND(failure_price::numeric, 4) AS failure_px,
                   ROUND(volume_ratio::numeric, 2) AS vol_ratio
            FROM scanner_events
            WHERE {where}
            ORDER BY detected_at DESC
            LIMIT 25
            """,
            params,
        )
        recent = cur.fetchall()

    print(f"\nScanner: {args.scanner}")
    print(f"Window:  last {args.since_days} days")
    if args.ticker:
        print(f"Ticker:  {args.ticker.upper()}")
    print(f"Events:  {n}  unique tickers: {unique_tickers}  unique days: {unique_days}")
    if first and last:
        print(f"Range:   {first.isoformat()}  ->  {last.isoformat()}")

    if per_ticker:
        print("\nPer-ticker (top 25):")
        print(f"  {'ticker':<8} {'events':>6} {'avg_vol_ratio':>14}")
        for ticker, count, ratio in per_ticker:
            ratio_str = "—" if ratio is None else f"{ratio}"
            print(f"  {ticker:<8} {count:>6} {ratio_str:>14}")

    if recent:
        print("\nMost recent 25 events:")
        for detected_at, day, ticker, prior, bo_px, fail_px, vr in recent:
            vr_str = "—" if vr is None else f"{vr}x"
            print(
                f"  {detected_at:%Y-%m-%d %H:%M}  {ticker:<6} day={day}  "
                f"prior={prior}  breakout={bo_px}  failure={fail_px}  vol={vr_str}"
            )

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
