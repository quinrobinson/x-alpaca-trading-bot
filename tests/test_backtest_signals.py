"""Tests for the backtest CLI runner — Phase 4 acceptance gate.

Generates synthetic tick CSVs covering each distinct exit reason and
verifies the runner produces the expected trade-by-trade outcomes.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

# Make scripts/ importable in tests.
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import backtest_signals as bts  # noqa: E402


ET = bts.strategy.ET


def _row(trade_id: str, entry: Decimal, expiration: date, ts: datetime, price: Decimal) -> str:
    return f"{trade_id},{entry},{expiration.isoformat()},{ts.isoformat()},{price}\n"


def _write_csv(path: Path, rows: list[str]) -> None:
    path.write_text("trade_id,entry_price,expiration,tick_ts,tick_price\n" + "".join(rows))


def test_stop_loss_trade_exits_at_minus_20(tmp_path: Path) -> None:
    csv_path = tmp_path / "stop_loss.csv"
    entry = Decimal("2.50")
    expiry = date(2026, 6, 20)
    t0 = datetime(2026, 5, 12, 13, 30, tzinfo=timezone.utc)
    _write_csv(csv_path, [
        _row("t1", entry, expiry, t0, entry),
        _row("t1", entry, expiry, t0 + timedelta(minutes=5), Decimal("2.40")),
        _row("t1", entry, expiry, t0 + timedelta(minutes=10), Decimal("2.10")),
        _row("t1", entry, expiry, t0 + timedelta(minutes=15), Decimal("2.00")),  # hits stop
        _row("t1", entry, expiry, t0 + timedelta(minutes=20), Decimal("1.80")),  # noise after
    ])

    results = bts.run_backtest(csv_path, stop_pct=Decimal("0.20"))
    assert len(results) == 1
    r = results[0]
    assert r.exit_reason == "stop_loss"
    assert r.exit_price == Decimal("2.00")
    assert r.pnl_pct == Decimal("-0.20")
    assert r.ratchet_level == 0


def test_ratchet_trade_locks_in_profit(tmp_path: Path) -> None:
    """Ride to +60%, then pull back below the +30% ratchet stop — exit locks in profit.

    New ratchet table (May 2026): triggers are +20/+30/+40/+60. Stops move
    to breakeven / +10% / +20% / +30% respectively.
    """
    csv_path = tmp_path / "ratchet.csv"
    entry = Decimal("2.00")
    expiry = date(2026, 6, 20)
    t0 = datetime(2026, 5, 12, 13, 30, tzinfo=timezone.utc)
    _write_csv(csv_path, [
        _row("t1", entry, expiry, t0, entry),
        _row("t1", entry, expiry, t0 + timedelta(minutes=5), Decimal("2.40")),   # +20% level 1
        _row("t1", entry, expiry, t0 + timedelta(minutes=10), Decimal("2.60")),  # +30% level 2
        _row("t1", entry, expiry, t0 + timedelta(minutes=15), Decimal("2.80")),  # +40% level 3
        _row("t1", entry, expiry, t0 + timedelta(minutes=20), Decimal("3.20")),  # +60% level 4
        _row("t1", entry, expiry, t0 + timedelta(minutes=25), Decimal("2.50")),  # below stop ($2.60)
    ])

    results = bts.run_backtest(csv_path, stop_pct=Decimal("0.20"))
    assert len(results) == 1
    r = results[0]
    assert r.exit_reason == "stop_loss"
    assert r.ratchet_level == 4
    assert r.exit_price == Decimal("2.50")
    # PnL is +25% (exited at $2.50 from $2.00 entry); ratchet locked in real gain
    assert r.pnl_pct == Decimal("0.25")


def test_dte_close_fires(tmp_path: Path) -> None:
    """Expiration today → first tick after entry triggers dte_close."""
    csv_path = tmp_path / "dte.csv"
    entry = Decimal("1.50")
    t0 = datetime(2026, 5, 12, 13, 30, tzinfo=timezone.utc)
    expiry = t0.astimezone(ET).date()
    _write_csv(csv_path, [
        _row("t1", entry, expiry, t0, entry),
        _row("t1", entry, expiry, t0 + timedelta(minutes=1), Decimal("1.55")),
    ])
    results = bts.run_backtest(csv_path, stop_pct=Decimal("0.20"))
    assert len(results) == 1
    assert results[0].exit_reason == "dte_close"


def test_time_stop_1555_fires(tmp_path: Path) -> None:
    csv_path = tmp_path / "time.csv"
    entry = Decimal("2.00")
    expiry = date(2026, 6, 20)
    # 12:00 ET start, then a tick at 15:55 ET
    t0 = datetime(2026, 5, 12, 12, 0, tzinfo=ET).astimezone(timezone.utc)
    t1 = datetime(2026, 5, 12, 15, 55, tzinfo=ET).astimezone(timezone.utc)
    _write_csv(csv_path, [
        _row("t1", entry, expiry, t0, entry),
        _row("t1", entry, expiry, t0 + timedelta(minutes=10), Decimal("2.05")),
        _row("t1", entry, expiry, t1, Decimal("2.10")),  # at 15:55 ET → flatten
    ])
    results = bts.run_backtest(csv_path, stop_pct=Decimal("0.20"))
    assert len(results) == 1
    assert results[0].exit_reason == "time_stop_1555"
    assert results[0].exit_price == Decimal("2.10")


def test_end_of_data_when_no_exit_fires(tmp_path: Path) -> None:
    """Short, mild data run with no exit triggers: marked to last tick."""
    csv_path = tmp_path / "noexit.csv"
    entry = Decimal("2.00")
    expiry = date(2026, 6, 20)
    t0 = datetime(2026, 5, 12, 12, 0, tzinfo=ET).astimezone(timezone.utc)
    _write_csv(csv_path, [
        _row("t1", entry, expiry, t0, entry),
        _row("t1", entry, expiry, t0 + timedelta(minutes=5), Decimal("2.04")),
        _row("t1", entry, expiry, t0 + timedelta(minutes=10), Decimal("2.06")),
    ])
    results = bts.run_backtest(csv_path, stop_pct=Decimal("0.20"))
    assert len(results) == 1
    assert results[0].exit_reason == "end_of_data"
    assert results[0].exit_price == Decimal("2.06")
    assert results[0].pnl_pct == Decimal("0.03")


def test_multi_trade_summary_groups_correctly(tmp_path: Path) -> None:
    csv_path = tmp_path / "multi.csv"
    entry = Decimal("2.00")
    expiry = date(2026, 6, 20)
    t0 = datetime(2026, 5, 12, 13, 30, tzinfo=timezone.utc)
    rows = []
    # Three independent trades, intentionally interleaved in CSV order.
    for trade_id, exit_price in [("A", Decimal("1.60")), ("B", Decimal("2.40")), ("C", Decimal("2.05"))]:
        rows.append(_row(trade_id, entry, expiry, t0, entry))
        rows.append(_row(trade_id, entry, expiry, t0 + timedelta(minutes=5), exit_price))
    _write_csv(csv_path, rows)

    results = bts.run_backtest(csv_path, stop_pct=Decimal("0.20"))
    assert {r.trade_id for r in results} == {"A", "B", "C"}
    by_id = {r.trade_id: r for r in results}
    assert by_id["A"].exit_reason == "stop_loss"            # hit -20% stop
    assert by_id["B"].exit_reason == "end_of_data"          # +20% locks ratchet but no exit
    assert by_id["C"].exit_reason == "end_of_data"          # +2.5%, no exit


def test_json_output_is_valid_and_complete(tmp_path: Path) -> None:
    """Smoke test for the --json flag."""
    import io
    import json
    from contextlib import redirect_stdout

    csv_path = tmp_path / "j.csv"
    entry = Decimal("2.00")
    expiry = date(2026, 6, 20)
    t0 = datetime(2026, 5, 12, 13, 30, tzinfo=timezone.utc)
    _write_csv(csv_path, [
        _row("t1", entry, expiry, t0, entry),
        _row("t1", entry, expiry, t0 + timedelta(minutes=5), Decimal("1.60")),
    ])

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = bts.main([str(csv_path), "--json"])
    assert rc == 0
    payload = json.loads(buf.getvalue())
    assert isinstance(payload, list)
    assert len(payload) == 1
    assert payload[0]["exit_reason"] == "stop_loss"
    assert payload[0]["entry_price"] == "2.00"  # Decimal serialized as string
