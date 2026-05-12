"""Unit tests for executor — Phase 6 logic coverage.

These run against a hand-rolled `FakeTradingClient` so the full order
lifecycle is exercised without touching real Alpaca. The integration-test
suite (test_executor_integration.py) provides a read-only smoke check
against the live paper account.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, time as datetime_time, timedelta, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from x_alpaca_trading_bot.executor import (
    Executor,
    OpenPosition,
    PaperFill,
    PaperOrder,
    ReconciliationSnapshot,
)


ET = ZoneInfo("America/New_York")
NOW_UTC = datetime(2026, 5, 12, 14, 30, tzinfo=timezone.utc)


# ---- Fake Alpaca trading client ------------------------------------------

@dataclass
class FakeOrder:
    id: str
    client_order_id: str
    symbol: str
    side: str
    type: str
    status: str
    qty: int
    limit_price: float | None = None
    stop_price: float | None = None
    filled_avg_price: float | None = None
    submitted_at: datetime = field(default_factory=lambda: NOW_UTC)
    filled_at: datetime | None = None


@dataclass
class FakePosition:
    symbol: str
    qty: int
    avg_entry_price: float
    market_value: float | None = None
    current_price: float | None = None


class FakeTradingClient:
    """In-memory Alpaca-compatible client for unit tests."""

    def __init__(self) -> None:
        self.orders: dict[str, FakeOrder] = {}
        self.positions: list[FakePosition] = []
        self.submitted: list[FakeOrder] = []  # in submission order
        self.cancellations: list[str] = []
        self.close_all_calls: int = 0
        # Optional override that lets a test script status transitions.
        self.next_status: str | None = None

    # ---- Alpaca surface ----

    def submit_order(self, req: Any) -> FakeOrder:
        # alpaca-py request objects carry side/type as Enums; in tests we pass
        # alpaca-py's real request classes via Executor, so .side, .type are
        # set. Coerce.
        side = _enum_str(getattr(req, "side", "buy")).lower()
        # alpaca-py exposes the order type via different attrs depending on
        # the request class; try a few.
        for attr in ("type", "order_type"):
            type_val = getattr(req, attr, None)
            if type_val is not None:
                break
        order_type = _enum_str(type_val).lower() if type_val is not None else _infer_type(req)

        order = FakeOrder(
            id=f"fake-{uuid.uuid4().hex[:8]}",
            client_order_id=req.client_order_id,
            symbol=req.symbol,
            side=side,
            type=order_type,
            status=self.next_status or "new",
            qty=int(req.qty),
            limit_price=getattr(req, "limit_price", None),
            stop_price=getattr(req, "stop_price", None),
        )
        self.orders[order.id] = order
        self.submitted.append(order)
        return order

    def get_order_by_id(self, order_id: str) -> FakeOrder:
        return self.orders[order_id]

    def cancel_order_by_id(self, order_id: str) -> None:
        self.cancellations.append(order_id)
        if order_id in self.orders:
            self.orders[order_id].status = "canceled"

    def get_orders(self, *, filter: Any = None) -> list[FakeOrder]:  # noqa: A002
        return [o for o in self.orders.values() if o.status in ("new", "accepted", "partially_filled")]

    def get_all_positions(self) -> list[FakePosition]:
        return list(self.positions)

    def close_all_positions(self, *, cancel_orders: bool = True) -> list[FakeOrder]:
        self.close_all_calls += 1
        close_orders = []
        for p in self.positions:
            o = FakeOrder(
                id=f"fake-close-{uuid.uuid4().hex[:8]}",
                client_order_id=f"close-{p.symbol}",
                symbol=p.symbol,
                side="sell",
                type="market",
                status="accepted",
                qty=abs(p.qty),
            )
            self.orders[o.id] = o
            close_orders.append(o)
        self.positions.clear()
        return close_orders

    # ---- Test helpers ----

    def mark_filled(self, order_id: str, fill_price: Decimal, *, filled_at: datetime | None = None) -> None:
        o = self.orders[order_id]
        o.status = "filled"
        o.filled_avg_price = float(fill_price)
        o.filled_at = filled_at or NOW_UTC

    def add_position(self, symbol: str, qty: int, avg_entry: Decimal) -> None:
        self.positions.append(FakePosition(symbol=symbol, qty=qty, avg_entry_price=float(avg_entry)))


def _enum_str(v: Any) -> str:
    if v is None:
        return ""
    if hasattr(v, "value"):
        return str(v.value)
    return str(v)


def _infer_type(req: Any) -> str:
    name = req.__class__.__name__.lower()
    if "limit" in name:
        return "limit"
    if "stop" in name:
        return "stop"
    return "market"


@pytest.fixture
def fake_client() -> FakeTradingClient:
    return FakeTradingClient()


@pytest.fixture
def executor(fake_client: FakeTradingClient) -> Executor:
    return Executor(trading_client=fake_client)


# ---- Paper-only enforcement -----------------------------------------------

def test_constructor_refuses_non_paper_url() -> None:
    with pytest.raises(RuntimeError, match="paper-only"):
        Executor(
            alpaca_api_key="k",
            alpaca_secret_key="s",
            alpaca_base_url="https://api.alpaca.markets",  # LIVE
        )


def test_constructor_requires_creds_or_client() -> None:
    with pytest.raises(ValueError, match="trading_client"):
        Executor()


# ---- submit_limit_buy -----------------------------------------------------

def test_submit_limit_buy_records_order(executor: Executor, fake_client: FakeTradingClient) -> None:
    order = executor.submit_limit_buy("AAPL260620C00185000", qty=1, limit_price=Decimal("2.55"))
    assert isinstance(order, PaperOrder)
    assert order.side == "buy"
    assert order.order_type == "limit"
    assert order.qty == 1
    assert order.limit_price == Decimal("2.55")
    assert order.symbol == "AAPL260620C00185000"
    assert order.client_order_id.startswith("xab-buy-")
    assert len(fake_client.submitted) == 1


def test_submit_limit_buy_accepts_explicit_client_order_id(executor: Executor) -> None:
    cid = "stable-id-1"
    order = executor.submit_limit_buy("AAPL260620C00185000", 1, Decimal("2.55"), client_order_id=cid)
    assert order.client_order_id == cid


# ---- submit_stop_sell -----------------------------------------------------

def test_submit_stop_sell_records_order(executor: Executor) -> None:
    order = executor.submit_stop_sell("AAPL260620C00185000", qty=1, stop_price=Decimal("2.00"))
    assert order.side == "sell"
    assert order.order_type == "stop"
    assert order.stop_price == Decimal("2.00")


# ---- submit_market_sell ---------------------------------------------------

def test_submit_market_sell_records_order(executor: Executor) -> None:
    order = executor.submit_market_sell("AAPL260620C00185000", qty=1)
    assert order.side == "sell"
    assert order.order_type == "market"


# ---- Order lifecycle ------------------------------------------------------

def test_wait_for_fill_returns_fill_when_filled(executor: Executor, fake_client: FakeTradingClient) -> None:
    order = executor.submit_limit_buy("AAPL260620C00185000", 1, Decimal("2.55"))
    fake_client.mark_filled(order.alpaca_order_id, Decimal("2.55"))

    # Inject a synchronous clock so the loop doesn't actually sleep.
    fill = executor.wait_for_fill(
        order.alpaca_order_id,
        timeout_seconds=5,
        poll_seconds=0.0,
        clock=_fake_clock(),
        sleeper=lambda _s: None,
    )
    assert isinstance(fill, PaperFill)
    assert fill.fill_price == Decimal("2.55")
    assert fill.qty == 1
    assert fill.side == "buy"


def test_wait_for_fill_returns_none_when_canceled(executor: Executor, fake_client: FakeTradingClient) -> None:
    order = executor.submit_limit_buy("AAPL260620C00185000", 1, Decimal("2.55"))
    executor.cancel_order(order.alpaca_order_id)
    fill = executor.wait_for_fill(
        order.alpaca_order_id,
        timeout_seconds=1,
        poll_seconds=0.0,
        clock=_fake_clock(),
        sleeper=lambda _s: None,
    )
    assert fill is None


def test_wait_for_fill_returns_none_on_timeout(executor: Executor) -> None:
    order = executor.submit_limit_buy("AAPL260620C00185000", 1, Decimal("2.55"))
    # Order stays "new". Clock advances past deadline immediately.
    clock = _fake_clock(start=0.0, step=10.0)
    fill = executor.wait_for_fill(
        order.alpaca_order_id,
        timeout_seconds=5,
        poll_seconds=0.0,
        clock=clock,
        sleeper=lambda _s: None,
    )
    assert fill is None


def test_cancel_order_swallows_errors_for_idempotency(executor: Executor, fake_client: FakeTradingClient) -> None:
    """Calling cancel on a missing order should log+continue, not raise."""
    def boom(_oid: str) -> None:
        raise RuntimeError("already filled")

    fake_client.cancel_order_by_id = boom  # type: ignore[assignment]
    executor.cancel_order("missing-id")  # no exception


# ---- modify_stop ----------------------------------------------------------

def test_modify_stop_cancels_old_then_submits_new(executor: Executor, fake_client: FakeTradingClient) -> None:
    initial = executor.submit_stop_sell("AAPL260620C00185000", 1, Decimal("2.00"))
    new = executor.modify_stop(
        initial.alpaca_order_id,
        symbol="AAPL260620C00185000",
        qty=1,
        new_stop_price=Decimal("2.25"),
    )
    assert initial.alpaca_order_id in fake_client.cancellations
    assert new.stop_price == Decimal("2.25")
    assert new.alpaca_order_id != initial.alpaca_order_id


# ---- list + reconcile -----------------------------------------------------

def test_list_open_orders_filters_terminal(executor: Executor, fake_client: FakeTradingClient) -> None:
    o1 = executor.submit_limit_buy("AAPL260620C00185000", 1, Decimal("2.55"))
    o2 = executor.submit_stop_sell("AAPL260620C00185000", 1, Decimal("2.00"))
    fake_client.mark_filled(o1.alpaca_order_id, Decimal("2.55"))
    open_orders = executor.list_open_orders()
    open_ids = {o.alpaca_order_id for o in open_orders}
    assert o1.alpaca_order_id not in open_ids
    assert o2.alpaca_order_id in open_ids


def test_list_open_positions_returns_alpaca_state(executor: Executor, fake_client: FakeTradingClient) -> None:
    fake_client.add_position("AAPL260620C00185000", qty=2, avg_entry=Decimal("2.55"))
    fake_client.add_position("TSLA260620C00230000", qty=1, avg_entry=Decimal("4.20"))
    positions = executor.list_open_positions()
    assert len(positions) == 2
    aapl = next(p for p in positions if p.symbol.startswith("AAPL"))
    assert aapl.qty == 2
    assert aapl.avg_entry_price == Decimal("2.55")


def test_reconcile_captures_orders_and_positions(executor: Executor, fake_client: FakeTradingClient) -> None:
    executor.submit_stop_sell("AAPL260620C00185000", 1, Decimal("2.00"))
    fake_client.add_position("AAPL260620C00185000", qty=1, avg_entry=Decimal("2.55"))
    snap = executor.reconcile(now=NOW_UTC)
    assert isinstance(snap, ReconciliationSnapshot)
    assert snap.captured_at == NOW_UTC
    assert len(snap.open_orders) == 1
    assert len(snap.open_positions) == 1


# ---- flatten_all ----------------------------------------------------------

def test_flatten_all_cancels_orders_and_closes_positions(executor: Executor, fake_client: FakeTradingClient) -> None:
    open_stop = executor.submit_stop_sell("AAPL260620C00185000", 1, Decimal("2.00"))
    fake_client.add_position("AAPL260620C00185000", qty=1, avg_entry=Decimal("2.55"))
    fake_client.add_position("TSLA260620C00230000", qty=1, avg_entry=Decimal("4.20"))

    close_ids = executor.flatten_all()
    assert open_stop.alpaca_order_id in fake_client.cancellations
    assert fake_client.close_all_calls == 1
    assert len(close_ids) == 2
    assert fake_client.positions == []


def test_flatten_all_with_nothing_open_is_safe(executor: Executor, fake_client: FakeTradingClient) -> None:
    close_ids = executor.flatten_all()
    assert close_ids == []
    assert fake_client.close_all_calls == 1


# ---- is_at_or_past_close ---------------------------------------------------

def test_is_at_or_past_close_true_at_1555(executor: Executor) -> None:
    at_1555 = datetime(2026, 5, 12, 15, 55, tzinfo=ET).astimezone(timezone.utc)
    assert executor.is_at_or_past_close(at_1555) is True


def test_is_at_or_past_close_false_at_1554(executor: Executor) -> None:
    at_1554 = datetime(2026, 5, 12, 15, 54, tzinfo=ET).astimezone(timezone.utc)
    assert executor.is_at_or_past_close(at_1554) is False


def test_is_at_or_past_close_custom_time(executor: Executor) -> None:
    at_1500 = datetime(2026, 5, 12, 15, 0, tzinfo=ET).astimezone(timezone.utc)
    assert executor.is_at_or_past_close(at_1500, close_time=datetime_time(15, 0)) is True
    assert executor.is_at_or_past_close(at_1500, close_time=datetime_time(15, 5)) is False


def test_is_at_or_past_close_rejects_naive(executor: Executor) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        executor.is_at_or_past_close(datetime(2026, 5, 12, 15, 55))


# ---- Helpers --------------------------------------------------------------

def _fake_clock(start: float = 0.0, step: float = 0.5):
    """Build an injectable clock that advances `step` seconds per call."""
    state = {"t": start}

    def tick() -> float:
        v = state["t"]
        state["t"] += step
        return v

    return tick
