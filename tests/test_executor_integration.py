"""Read-only integration tests for executor against real Alpaca paper.

These verify connectivity, constructor wiring, and the listing/reconcile
primitives without placing any orders. The destructive Phase 6 acceptance
gates (place order, fill detection, stop modification, reconciliation
adopting an open position, 15:55 flatten leaves no positions) are exercised
by `scripts/executor_manual_smoke.py` under operator supervision.

All tests skip cleanly when Alpaca creds are missing or are placeholders.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import datetime, timezone

import pytest
from dotenv import load_dotenv

from x_alpaca_trading_bot.executor import Executor, ReconciliationSnapshot


REQUIRED = ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ALPACA_BASE_URL")


def _creds() -> dict[str, str] | None:
    load_dotenv(override=True)
    out: dict[str, str] = {}
    for name in REQUIRED:
        v = os.environ.get(name) or ""
        if not v or "PLACEHOLDER" in v:
            return None
        out[name] = v
    return out


@pytest.fixture(scope="module")
def executor() -> Iterator[Executor]:
    creds = _creds()
    if creds is None:
        pytest.skip("Alpaca paper creds missing or placeholder")
    yield Executor(
        alpaca_api_key=creds["ALPACA_API_KEY"],
        alpaca_secret_key=creds["ALPACA_SECRET_KEY"],
        alpaca_base_url=creds["ALPACA_BASE_URL"],
    )


def test_list_open_orders_returns_list(executor: Executor) -> None:
    orders = executor.list_open_orders()
    assert isinstance(orders, list)


def test_list_open_positions_returns_list(executor: Executor) -> None:
    positions = executor.list_open_positions()
    assert isinstance(positions, list)


def test_reconcile_returns_snapshot(executor: Executor) -> None:
    snap = executor.reconcile()
    assert isinstance(snap, ReconciliationSnapshot)
    assert isinstance(snap.open_orders, list)
    assert isinstance(snap.open_positions, list)
    assert snap.captured_at.tzinfo is not None
