"""executor — Phase 6.

Paper-only Alpaca order primitives. Validated signals become real (paper)
orders here. The orchestrator (Phase 7) composes these primitives with
strategy.evaluate() and risk_manager.evaluate() to run the full flow.

This module enforces paper-only on construction:

    from x_alpaca_trading_bot.config import assert_paper_mode
    assert_paper_mode(alpaca_base_url)

If the live URL ever sneaks in, the constructor refuses to start.

Public surface:

    Executor.submit_limit_buy(symbol, qty, limit_price, *, client_order_id)
        -> PaperOrder

    Executor.submit_stop_sell(symbol, qty, stop_price, *, client_order_id)
        -> PaperOrder

    Executor.submit_market_sell(symbol, qty, *, client_order_id)
        -> PaperOrder

    Executor.get_order(alpaca_order_id) -> PaperOrder
    Executor.cancel_order(alpaca_order_id) -> None
    Executor.wait_for_fill(alpaca_order_id, *, timeout_seconds, poll_seconds)
        -> PaperFill | None

    Executor.modify_stop(current_stop_order_id, symbol, qty, new_stop_price)
        -> PaperOrder

    Executor.list_open_orders() -> list[PaperOrder]
    Executor.list_open_positions() -> list[OpenPosition]

    Executor.flatten_all() -> list[str]
        Cancel all open orders, close all open positions. Returns the
        Alpaca order IDs of the resulting close orders.

    Executor.reconcile() -> ReconciliationSnapshot
        Snapshot of currently open Alpaca orders + positions at startup.
        The orchestrator decides which to adopt vs. abandon.

Idempotency: every submission accepts an optional `client_order_id`. Alpaca
rejects duplicates, so retries with the same id are safe.

State of mind: this module is the ONLY place orders touch the broker. Keep
it boring, predictable, and well-logged.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, time as datetime_time, timezone
from decimal import Decimal
from typing import Any, Literal
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

Side = Literal["buy", "sell"]
OrderTypeName = Literal["market", "limit", "stop"]
Status = Literal[
    "new", "partially_filled", "filled", "canceled",
    "expired", "rejected", "pending_cancel", "pending_new", "accepted",
    "done_for_day", "replaced", "held", "stopped", "suspended",
]


# ---- Public data shapes -----------------------------------------------------

@dataclass(frozen=True)
class PaperOrder:
    alpaca_order_id: str
    client_order_id: str
    symbol: str
    side: Side
    order_type: OrderTypeName
    qty: int
    limit_price: Decimal | None
    stop_price: Decimal | None
    status: str
    submitted_at: datetime
    filled_at: datetime | None
    filled_avg_price: Decimal | None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PaperFill:
    alpaca_order_id: str
    filled_at: datetime
    symbol: str
    side: Side
    qty: int
    fill_price: Decimal
    commission: Decimal


@dataclass(frozen=True)
class OpenPosition:
    symbol: str                       # OCC contract symbol
    qty: int                          # positive long, negative short (we are long-only)
    avg_entry_price: Decimal
    market_value: Decimal | None
    current_price: Decimal | None


@dataclass(frozen=True)
class ReconciliationSnapshot:
    open_orders: list[PaperOrder]
    open_positions: list[OpenPosition]
    captured_at: datetime


# ---- The Executor -----------------------------------------------------------

class Executor:
    """Paper-only Alpaca order primitives. See module docstring for API."""

    def __init__(
        self,
        *,
        alpaca_api_key: str | None = None,
        alpaca_secret_key: str | None = None,
        alpaca_base_url: str | None = None,
        trading_client: Any | None = None,
    ) -> None:
        """Either pass credentials (and construct a real TradingClient) or
        inject a `trading_client` directly (used by unit tests with a fake).
        """
        if trading_client is not None:
            self._client = trading_client
            self._paper = True
            return

        if not (alpaca_api_key and alpaca_secret_key and alpaca_base_url):
            raise ValueError(
                "Executor: pass credentials or a trading_client. "
                "Both are required when not injecting a client."
            )
        # Paper-only assertion lives in config.py so the rule is single-sourced.
        from x_alpaca_trading_bot.config import assert_paper_mode
        assert_paper_mode(alpaca_base_url)

        from alpaca.trading.client import TradingClient
        self._client = TradingClient(
            api_key=alpaca_api_key,
            secret_key=alpaca_secret_key,
            paper=True,
        )
        self._paper = True

    # ---- Submission primitives --------------------------------------------

    def submit_limit_buy(
        self,
        symbol: str,
        qty: int,
        limit_price: Decimal,
        *,
        client_order_id: str | None = None,
    ) -> PaperOrder:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest

        cid = client_order_id or _new_client_order_id("buy")
        req = LimitOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            limit_price=float(limit_price),
            client_order_id=cid,
        )
        logger.info("submit_limit_buy %s qty=%s limit=%s cid=%s", symbol, qty, limit_price, cid)
        order = self._client.submit_order(req)
        return _to_paper_order(order)

    def submit_stop_sell(
        self,
        symbol: str,
        qty: int,
        stop_price: Decimal,
        *,
        client_order_id: str | None = None,
    ) -> PaperOrder:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import StopOrderRequest

        cid = client_order_id or _new_client_order_id("stop")
        req = StopOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
            stop_price=float(stop_price),
            client_order_id=cid,
        )
        logger.info("submit_stop_sell %s qty=%s stop=%s cid=%s", symbol, qty, stop_price, cid)
        order = self._client.submit_order(req)
        return _to_paper_order(order)

    def submit_market_sell(
        self,
        symbol: str,
        qty: int,
        *,
        client_order_id: str | None = None,
    ) -> PaperOrder:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        cid = client_order_id or _new_client_order_id("close")
        req = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
            client_order_id=cid,
        )
        logger.info("submit_market_sell %s qty=%s cid=%s", symbol, qty, cid)
        order = self._client.submit_order(req)
        return _to_paper_order(order)

    # ---- Order lifecycle ---------------------------------------------------

    def get_order(self, alpaca_order_id: str) -> PaperOrder:
        order = self._client.get_order_by_id(alpaca_order_id)
        return _to_paper_order(order)

    def cancel_order(self, alpaca_order_id: str) -> None:
        logger.info("cancel_order %s", alpaca_order_id)
        try:
            self._client.cancel_order_by_id(alpaca_order_id)
        except Exception as exc:  # noqa: BLE001
            # Already canceled / filled — log and move on. Idempotency-friendly.
            logger.warning("cancel_order %s failed (likely terminal): %s", alpaca_order_id, exc)

    def wait_for_fill(
        self,
        alpaca_order_id: str,
        *,
        timeout_seconds: int,
        poll_seconds: float = 1.0,
        clock: Any = time.monotonic,
        sleeper: Any = time.sleep,
    ) -> PaperFill | None:
        """Block until the order fills, gets canceled/rejected, or timeout fires.

        Returns the PaperFill on filled, None otherwise. `clock` and `sleeper`
        are injectable so tests run synchronously.
        """
        deadline = clock() + timeout_seconds
        while True:
            order = self.get_order(alpaca_order_id)
            status = (order.status or "").lower()
            if status == "filled":
                if order.filled_at is None or order.filled_avg_price is None:
                    logger.warning("filled order missing filled_at/avg_price: %s", alpaca_order_id)
                    return None
                return PaperFill(
                    alpaca_order_id=order.alpaca_order_id,
                    filled_at=order.filled_at,
                    symbol=order.symbol,
                    side=order.side,
                    qty=order.qty,
                    fill_price=order.filled_avg_price,
                    commission=Decimal(0),  # Alpaca options paper has no commission
                )
            if status in ("canceled", "rejected", "expired"):
                logger.info("order %s terminal without fill: %s", alpaca_order_id, status)
                return None
            if clock() >= deadline:
                logger.info("order %s wait timed out at %ss", alpaca_order_id, timeout_seconds)
                return None
            sleeper(poll_seconds)

    # ---- Stop modification (cancel + replace) -----------------------------

    def modify_stop(
        self,
        current_stop_order_id: str,
        symbol: str,
        qty: int,
        new_stop_price: Decimal,
        *,
        client_order_id: str | None = None,
    ) -> PaperOrder:
        """Cancel the existing stop, place a new one at `new_stop_price`."""
        logger.info(
            "modify_stop %s -> new stop %s on %s qty=%s",
            current_stop_order_id, new_stop_price, symbol, qty,
        )
        self.cancel_order(current_stop_order_id)
        return self.submit_stop_sell(symbol, qty, new_stop_price, client_order_id=client_order_id)

    # ---- Listing ----------------------------------------------------------

    def list_open_orders(self) -> list[PaperOrder]:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest
        req = GetOrdersRequest(status=QueryOrderStatus.OPEN)
        orders = self._client.get_orders(filter=req)
        return [_to_paper_order(o) for o in orders]

    def list_open_positions(self) -> list[OpenPosition]:
        positions = self._client.get_all_positions()
        return [_to_open_position(p) for p in positions]

    # ---- Flatten + reconcile ---------------------------------------------

    def flatten_all(self) -> list[str]:
        """Cancel all open orders, then close every open position at market.

        Returns the Alpaca order IDs of the close orders generated. Safe to
        call at startup or at 15:55 ET.
        """
        for order in self.list_open_orders():
            self.cancel_order(order.alpaca_order_id)
        close_orders = self._client.close_all_positions(cancel_orders=True)
        ids = [str(getattr(o, "id", "")) for o in (close_orders or [])]
        logger.info("flatten_all generated %d close orders", len(ids))
        return ids

    def reconcile(self, *, now: datetime | None = None) -> ReconciliationSnapshot:
        """At startup, capture currently open Alpaca orders + positions.

        Caller (orchestrator) decides which to adopt into in-memory state and
        which to abandon. We don't mutate state here.
        """
        captured_at = now or datetime.now(timezone.utc)
        return ReconciliationSnapshot(
            open_orders=self.list_open_orders(),
            open_positions=self.list_open_positions(),
            captured_at=captured_at,
        )

    # ---- Time helpers ----------------------------------------------------

    def is_at_or_past_close(
        self,
        now: datetime,
        *,
        close_time: datetime_time = datetime_time(15, 55),
    ) -> bool:
        """True iff `now` (any tz) is at or past `close_time` in ET on its ET date.

        Used by the orchestrator to schedule the 15:55 flatten.
        """
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        et = now.astimezone(ET)
        target = datetime.combine(et.date(), close_time, tzinfo=ET)
        return et >= target


# ---- Conversion helpers (Alpaca SDK objects → our shapes) ------------------

def _to_paper_order(alpaca_order: Any) -> PaperOrder:
    """Convert an alpaca-py Order to our PaperOrder.

    Tolerant of SDK shape variations (fake clients in tests pass simple objects
    with the same attribute names).
    """
    side_val = _coerce_enum_str(getattr(alpaca_order, "side", "")).lower()
    type_val = _coerce_enum_str(getattr(alpaca_order, "type", "") or getattr(alpaca_order, "order_type", "")).lower()
    status_val = _coerce_enum_str(getattr(alpaca_order, "status", "")).lower()

    limit_price = _decimal_or_none(getattr(alpaca_order, "limit_price", None))
    stop_price = _decimal_or_none(getattr(alpaca_order, "stop_price", None))
    filled_avg_price = _decimal_or_none(getattr(alpaca_order, "filled_avg_price", None))

    submitted_at = getattr(alpaca_order, "submitted_at", None) or getattr(alpaca_order, "created_at", None) or datetime.now(timezone.utc)
    filled_at = getattr(alpaca_order, "filled_at", None)

    qty_raw = getattr(alpaca_order, "qty", 0)
    try:
        qty = int(Decimal(str(qty_raw)))
    except Exception:  # noqa: BLE001
        qty = int(qty_raw or 0)

    return PaperOrder(
        alpaca_order_id=str(getattr(alpaca_order, "id", "")),
        client_order_id=str(getattr(alpaca_order, "client_order_id", "") or ""),
        symbol=str(getattr(alpaca_order, "symbol", "")),
        side=side_val if side_val in ("buy", "sell") else "buy",  # type: ignore[assignment]
        order_type=type_val if type_val in ("market", "limit", "stop") else "limit",  # type: ignore[assignment]
        qty=qty,
        limit_price=limit_price,
        stop_price=stop_price,
        status=status_val,
        submitted_at=submitted_at,
        filled_at=filled_at,
        filled_avg_price=filled_avg_price,
        raw=_safe_to_dict(alpaca_order),
    )


def _to_open_position(alpaca_pos: Any) -> OpenPosition:
    qty_raw = getattr(alpaca_pos, "qty", 0)
    try:
        qty = int(Decimal(str(qty_raw)))
    except Exception:  # noqa: BLE001
        qty = int(qty_raw or 0)
    return OpenPosition(
        symbol=str(getattr(alpaca_pos, "symbol", "")),
        qty=qty,
        avg_entry_price=_decimal_or_none(getattr(alpaca_pos, "avg_entry_price", None)) or Decimal(0),
        market_value=_decimal_or_none(getattr(alpaca_pos, "market_value", None)),
        current_price=_decimal_or_none(getattr(alpaca_pos, "current_price", None)),
    )


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return None


def _coerce_enum_str(value: Any) -> str:
    """Convert Alpaca SDK enums (or plain strings) to a lowercase string."""
    if value is None:
        return ""
    # alpaca-py uses string-valued Enums; .value is the canonical token.
    if hasattr(value, "value"):
        return str(value.value)
    return str(value)


def _safe_to_dict(obj: Any) -> dict[str, Any]:
    """Best-effort serialization of an SDK object for the orders.raw JSONB.

    Falls back to a small subset of attributes if model_dump / dict aren't
    available. We don't want a serialization failure to block order
    placement, so we catch broadly and fall back.
    """
    for attr in ("model_dump", "dict"):
        method = getattr(obj, attr, None)
        if callable(method):
            try:
                return {k: _stringify(v) for k, v in method().items()}
            except Exception:  # noqa: BLE001
                continue
    fields = (
        "id", "client_order_id", "symbol", "side", "type", "status", "qty",
        "limit_price", "stop_price", "filled_avg_price", "submitted_at",
        "filled_at",
    )
    return {f: _stringify(getattr(obj, f, None)) for f in fields}


def _stringify(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "value"):  # Enum
        return value.value
    return str(value)


def _new_client_order_id(prefix: str) -> str:
    """Generate a unique client_order_id with a short prefix for diagnostics."""
    return f"xab-{prefix}-{uuid.uuid4().hex[:16]}"
