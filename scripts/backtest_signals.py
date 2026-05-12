#!/usr/bin/env python3
"""Replay historical option price ticks through strategy.evaluate().

Phase 4 task 4. Standalone CLI; reads CSV ticks and emits a per-trade summary
plus aggregate stats so we can sanity-check the strategy logic against any
synthetic or recorded dataset before live paper trading begins.

Usage:
    python scripts/backtest_signals.py path/to/ticks.csv [--stop-pct 0.20] [--json]

CSV schema (header required):
    trade_id     - groups ticks belonging to one trade
    entry_price  - decimal; same for every row of a trade
    expiration   - ISO date (YYYY-MM-DD); same for every row of a trade
    tick_ts      - ISO datetime, timezone-aware (e.g. 2026-05-12T13:30:00+00:00)
    tick_price   - decimal option price at that tick

The first tick of each trade is treated as the entry tick. Subsequent ticks
are fed to strategy.evaluate(). If no exit fires before the last tick, the
trade is "marked to market" at the final tick with reason "end_of_data".
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from x_alpaca_trading_bot import strategy


@dataclass
class TradeResult:
    trade_id: str
    entry_price: Decimal
    exit_price: Decimal
    exit_reason: str
    exit_ts: datetime
    pnl: Decimal
    pnl_pct: Decimal
    ratchet_level: int
    hold_minutes: int


def load_trades(path: Path) -> list[tuple[str, list[tuple[datetime, Decimal]], Decimal, date]]:
    """Load CSV into [(trade_id, [(ts, price)], entry_price, expiration), ...].

    Within each trade, ticks are sorted by ts (we don't trust input order).
    """
    rows_by_trade: dict[str, list[tuple[datetime, Decimal]]] = defaultdict(list)
    meta: dict[str, tuple[Decimal, date]] = {}
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            tid = row["trade_id"]
            tick_ts = datetime.fromisoformat(row["tick_ts"])
            tick_price = Decimal(row["tick_price"])
            rows_by_trade[tid].append((tick_ts, tick_price))
            if tid not in meta:
                meta[tid] = (
                    Decimal(row["entry_price"]),
                    date.fromisoformat(row["expiration"]),
                )

    trades: list[tuple[str, list[tuple[datetime, Decimal]], Decimal, date]] = []
    for tid, ticks in rows_by_trade.items():
        ticks.sort(key=lambda t: t[0])
        entry_price, expiration = meta[tid]
        trades.append((tid, ticks, entry_price, expiration))
    return trades


def backtest_one(
    trade_id: str,
    ticks: list[tuple[datetime, Decimal]],
    entry_price: Decimal,
    expiration: date,
    *,
    stop_pct: Decimal,
) -> TradeResult | None:
    """Replay one trade through the strategy. Returns its outcome, or None if empty."""
    if not ticks:
        return None
    open_ts, _open_price = ticks[0]
    position = strategy.open_position(
        entry_price=entry_price,
        qty=1,
        opened_at=open_ts,
        expiration=expiration,
        initial_stop_pct=stop_pct,
    )

    final_exit = None
    final_price = ticks[-1][1]
    final_ts = ticks[-1][0]

    for ts, price in ticks[1:]:  # first tick is entry — already represented by open_position
        result = strategy.evaluate(position, price, ts)
        position = result.position
        if result.exit is not None:
            final_exit = result.exit
            break

    if final_exit is not None:
        exit_price = final_exit.exit_price
        exit_reason = final_exit.reason
        exit_ts = final_exit.triggered_at
    else:
        exit_price = final_price
        exit_reason = "end_of_data"
        exit_ts = final_ts

    pnl = exit_price - entry_price
    pnl_pct = pnl / entry_price
    hold_minutes = int((exit_ts - open_ts).total_seconds() / 60)
    return TradeResult(
        trade_id=trade_id,
        entry_price=entry_price,
        exit_price=exit_price,
        exit_reason=exit_reason,
        exit_ts=exit_ts,
        pnl=pnl,
        pnl_pct=pnl_pct,
        ratchet_level=position.ratchet_level,
        hold_minutes=hold_minutes,
    )


def run_backtest(path: Path, *, stop_pct: Decimal) -> list[TradeResult]:
    return [
        r
        for tid, ticks, entry, exp in load_trades(path)
        if (r := backtest_one(tid, ticks, entry, exp, stop_pct=stop_pct)) is not None
    ]


def _print_table(results: list[TradeResult]) -> None:
    header = f"{'trade_id':<12}{'entry':>9}{'exit':>9}{'reason':>22}{'pnl%':>10}{'hold(m)':>9}{'ratchet':>9}"
    print(header)
    print("-" * len(header))
    for r in results:
        print(
            f"{r.trade_id:<12}"
            f"{float(r.entry_price):>9.2f}"
            f"{float(r.exit_price):>9.2f}"
            f"{r.exit_reason:>22}"
            f"{float(r.pnl_pct):>10.1%}"
            f"{r.hold_minutes:>9}"
            f"{r.ratchet_level:>9}"
        )


def _print_summary(results: list[TradeResult]) -> None:
    if not results:
        print("No trades to summarize.")
        return
    wins = [r for r in results if r.pnl > 0]
    losses = [r for r in results if r.pnl <= 0]
    print()
    print(f"n={len(results)}  wins={len(wins)} ({len(wins) / len(results):.0%})  losses={len(losses)}")
    if wins:
        avg_win = sum((r.pnl_pct for r in wins), start=Decimal(0)) / len(wins)
        print(f"avg win pnl%:  {float(avg_win):+.1%}")
    if losses:
        avg_loss = sum((r.pnl_pct for r in losses), start=Decimal(0)) / len(losses)
        print(f"avg loss pnl%: {float(avg_loss):+.1%}")
    # Profit factor: sum(wins) / abs(sum(losses))
    win_sum = sum((r.pnl_pct for r in wins), start=Decimal(0))
    loss_sum = sum((r.pnl_pct for r in losses), start=Decimal(0))
    if loss_sum < 0:
        pf = win_sum / abs(loss_sum)
        print(f"profit factor: {float(pf):.2f}")


def _results_to_json(results: list[TradeResult]) -> str:
    def encode(o: Any) -> Any:
        if isinstance(o, Decimal):
            return str(o)
        if isinstance(o, datetime):
            return o.isoformat()
        raise TypeError(type(o))

    return json.dumps([asdict(r) for r in results], default=encode, indent=2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_path", type=Path, help="CSV file with tick data")
    parser.add_argument(
        "--stop-pct", type=Decimal, default=Decimal("0.20"),
        help="Initial stop loss as a fraction (default 0.20 = 20%%)",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of a table")
    args = parser.parse_args(argv)

    if not args.csv_path.exists():
        print(f"error: {args.csv_path} not found", file=sys.stderr)
        return 1

    results = run_backtest(args.csv_path, stop_pct=args.stop_pct)
    if args.json:
        print(_results_to_json(results))
    else:
        _print_table(results)
        _print_summary(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
