#!/usr/bin/env python3
"""Manual smoke walk for the Phase 6 destructive acceptance gates.

This script places real (paper) orders on your Alpaca paper account. Run
it during market hours, on the chosen symbol/strike, and confirm each step
at the prompt.

Gates exercised:
  1. Manual test signal creates order on Alpaca paper
  2. Fill detection works and writes to fills table
  3. Trailing stop modification executes correctly on paper position
  4. Reconciliation surfaces the open position
  5. 15:55 flatten (simulated) leaves no open positions

Usage:
    .venv/bin/python scripts/executor_manual_smoke.py SPY 500 2026-05-16 call
        [--qty 1] [--stop-pct 0.20]

The script will:
  - Quote the contract from Alpaca to find the live ask
  - Submit a limit buy at ask
  - Poll until filled or timeout (60s)
  - On fill: write order + fill rows to the DB
  - Place a protective stop -20% below fill
  - Bump the stop up by 10% (modify_stop)
  - Run reconcile() and print the snapshot
  - Flatten everything; verify list_open_positions is empty
  - Print a final pass/fail summary

Safety:
  - This is paper-only (config.assert_paper_mode enforced in Executor).
  - Cancels and closes anything it placed before exiting, even on Ctrl+C.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from x_alpaca_trading_bot import db, executor, journal  # noqa: E402
from x_alpaca_trading_bot.data_service import DataService, build_occ_symbol  # noqa: E402


def _confirm(prompt: str) -> None:
    """Block on operator confirmation; abort on anything that isn't y/Y."""
    resp = input(f"\n>> {prompt} (y/N): ").strip().lower()
    if resp != "y":
        print("Aborted by operator.")
        sys.exit(2)


def _load_env() -> dict[str, str]:
    load_dotenv(override=True)
    required = (
        "ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ALPACA_BASE_URL",
        "POLYGON_API_KEY", "DATABASE_URL",
    )
    env = {}
    missing = []
    for name in required:
        v = os.environ.get(name) or ""
        if not v or "PLACEHOLDER" in v:
            missing.append(name)
        env[name] = v
    if missing:
        print(f"Missing real values for: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)
    return env


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ticker", help="e.g. SPY")
    parser.add_argument("strike", type=Decimal, help="e.g. 500")
    parser.add_argument("expiration", help="ISO date, e.g. 2026-05-16")
    parser.add_argument("option_type", choices=["call", "put"])
    parser.add_argument("--qty", type=int, default=1)
    parser.add_argument("--stop-pct", type=Decimal, default=Decimal("0.20"))
    parser.add_argument("--fill-timeout", type=int, default=60)
    args = parser.parse_args(argv)

    env = _load_env()
    expiration = date.fromisoformat(args.expiration)
    symbol = build_occ_symbol(args.ticker, expiration, args.option_type, args.strike)
    print(f"Target contract: {symbol}")

    ds = DataService(
        alpaca_api_key=env["ALPACA_API_KEY"],
        alpaca_secret_key=env["ALPACA_SECRET_KEY"],
        alpaca_base_url=env["ALPACA_BASE_URL"],
        polygon_api_key=env["POLYGON_API_KEY"],
    )
    ex = executor.Executor(
        alpaca_api_key=env["ALPACA_API_KEY"],
        alpaca_secret_key=env["ALPACA_SECRET_KEY"],
        alpaca_base_url=env["ALPACA_BASE_URL"],
    )
    conn = db.connect(env["DATABASE_URL"])
    db.run_migrations(conn, ROOT / "deploy")

    # Step 0: market open?
    if not ex._client.get_clock().is_open:  # type: ignore[attr-defined]
        print("WARNING: market is closed; orders may not fill.")

    # Step 1: quote
    print("\n--- Step 1: quote ---")
    quote = ds.get_option_quote(args.ticker, expiration, args.option_type, args.strike)
    if quote is None:
        print(f"No quote for {symbol}. Aborting.")
        return 1
    print(f"bid={quote.bid}  ask={quote.ask}  mid={quote.mid}  spread%={quote.spread_pct:.2%}")
    _confirm(f"Submit LIMIT BUY {args.qty} @ {quote.ask}?")

    # Step 2: submit limit buy
    print("\n--- Step 2: submit_limit_buy ---")
    entry_order = ex.submit_limit_buy(symbol, args.qty, quote.ask)
    print(f"alpaca_order_id={entry_order.alpaca_order_id}  client_id={entry_order.client_order_id}")
    order_row_id = journal.insert_order(
        conn,
        signal_id=None,
        alpaca_order_id=entry_order.alpaca_order_id,
        submitted_at=entry_order.submitted_at,
        symbol=entry_order.symbol,
        side=entry_order.side,
        qty=entry_order.qty,
        order_type=entry_order.order_type,
        limit_price=entry_order.limit_price,
        stop_price=None,
        status=entry_order.status,
        raw=entry_order.raw,
    )
    print(f"orders.id={order_row_id}")

    # Step 3: wait for fill
    print(f"\n--- Step 3: wait_for_fill (timeout {args.fill_timeout}s) ---")
    fill = ex.wait_for_fill(entry_order.alpaca_order_id, timeout_seconds=args.fill_timeout)
    if fill is None:
        print("Entry did not fill. Canceling and exiting.")
        ex.cancel_order(entry_order.alpaca_order_id)
        return 1
    print(f"FILLED at {fill.fill_price} ({fill.qty} contract{'s' if fill.qty != 1 else ''})")
    fill_row_id = journal.insert_fill(
        conn,
        order_id=order_row_id,
        filled_at=fill.filled_at,
        symbol=fill.symbol,
        side=fill.side,
        qty=fill.qty,
        fill_price=fill.fill_price,
    )
    print(f"fills.id={fill_row_id}")

    try:
        # Step 4: protective stop
        print("\n--- Step 4: submit protective stop ---")
        stop_price = (fill.fill_price * (Decimal(1) - args.stop_pct)).quantize(Decimal("0.01"))
        print(f"Placing stop sell at {stop_price} (-{args.stop_pct:.0%} from fill)")
        _confirm("OK to place stop?")
        stop_order = ex.submit_stop_sell(symbol, args.qty, stop_price)
        print(f"stop alpaca_order_id={stop_order.alpaca_order_id}")

        # Step 5: modify_stop (bump up 10%)
        print("\n--- Step 5: modify_stop ---")
        new_stop = (stop_price * Decimal("1.10")).quantize(Decimal("0.01"))
        _confirm(f"Bump stop to {new_stop}?")
        new_stop_order = ex.modify_stop(stop_order.alpaca_order_id, symbol, args.qty, new_stop)
        print(f"new stop alpaca_order_id={new_stop_order.alpaca_order_id}")

        # Step 6: reconcile
        print("\n--- Step 6: reconcile ---")
        snap = ex.reconcile(now=datetime.now(timezone.utc))
        print(f"open_orders={len(snap.open_orders)}  open_positions={len(snap.open_positions)}")
        for o in snap.open_orders:
            print(f"  order {o.alpaca_order_id} {o.side} {o.order_type} {o.symbol} @ {o.stop_price or o.limit_price}")
        for p in snap.open_positions:
            print(f"  position {p.symbol} qty={p.qty} avg={p.avg_entry_price}")

        # Step 7: flatten_all
        print("\n--- Step 7: flatten_all ---")
        _confirm("OK to flatten everything?")
        close_ids = ex.flatten_all()
        print(f"flatten generated {len(close_ids)} close orders")
        # Give Alpaca a moment to settle
        time.sleep(2)
        remaining = ex.list_open_positions()
        print(f"open_positions after flatten: {len(remaining)}")
        if remaining:
            print("WARNING: positions remain. Investigate:")
            for p in remaining:
                print(f"  {p.symbol} qty={p.qty}")
            return 1

    except KeyboardInterrupt:
        print("\nInterrupted — flattening to clean up.")
        ex.flatten_all()
        return 130
    except Exception as exc:
        print(f"\nERROR: {exc}. Flattening to clean up.")
        ex.flatten_all()
        raise

    print("\nAll Phase 6 destructive gates passed manually.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
