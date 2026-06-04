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
    """Ride to +60% peak, then pull back below the trail — exit locks in profit.

    Continuous trail (2026-06): trail activates at +5%, runs 5% behind
    peak until peak gain >= +40%, then tightens to 3%. On this path:
        +20% (peak 2.40) -> trail active, stop = 2.40 * 0.95 = 2.28
        +30% (peak 2.60) -> stop = 2.60 * 0.95 = 2.47
        +40% (peak 2.80) -> aggressive, stop = 2.80 * 0.97 = 2.716
        +60% (peak 3.20) -> stop = 3.20 * 0.97 = 3.104
        pullback to 2.50 — well below stop, exit fires at 2.50
    Locked-in PnL is still +25%; ratchet_level is now 2 (was 4 under the
    old discrete table).
    """
    csv_path = tmp_path / "ratchet.csv"
    entry = Decimal("2.00")
    expiry = date(2026, 6, 20)
    t0 = datetime(2026, 5, 12, 13, 30, tzinfo=timezone.utc)
    _write_csv(csv_path, [
        _row("t1", entry, expiry, t0, entry),
        _row("t1", entry, expiry, t0 + timedelta(minutes=5), Decimal("2.40")),
        _row("t1", entry, expiry, t0 + timedelta(minutes=10), Decimal("2.60")),
        _row("t1", entry, expiry, t0 + timedelta(minutes=15), Decimal("2.80")),
        _row("t1", entry, expiry, t0 + timedelta(minutes=20), Decimal("3.20")),
        _row("t1", entry, expiry, t0 + timedelta(minutes=25), Decimal("2.50")),
    ])

    results = bts.run_backtest(csv_path, stop_pct=Decimal("0.20"))
    assert len(results) == 1
    r = results[0]
    assert r.exit_reason == "stop_loss"
    assert r.ratchet_level == 2          # aggressive regime reached
    assert r.exit_price == Decimal("2.50")
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
    """The 15:55 ET rule is DTE-gated as of 2026-06: only flattens contracts
    with DTE <= 3. Use a near-expiry contract so the rule actually fires."""
    csv_path = tmp_path / "time.csv"
    entry = Decimal("2.00")
    # 12:00 ET start, then a tick at 15:55 ET
    t0 = datetime(2026, 5, 12, 12, 0, tzinfo=ET).astimezone(timezone.utc)
    t1 = datetime(2026, 5, 12, 15, 55, tzinfo=ET).astimezone(timezone.utc)
    expiry = t0.astimezone(ET).date() + timedelta(days=2)  # DTE=2, within threshold
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
