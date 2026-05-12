"""snapshot — Phase 7.

Indicator snapshot capture + scheduling, plus trade-close summary writes.

Responsibilities:

  SnapshotScheduler
      In-memory registry of currently-open positions. Tells the orchestrator
      which positions are due for a 15-minute snapshot, and tracks running
      max-gain / max-loss percentages for the trades.max_gain_pct /
      trades.max_loss_pct columns.

  capture_snapshot(conn, ds, ctx, *, snapshot_type, now)
      Pulls Greeks, IV, indicators, market context, option quote, and
      underlying price from `MarketDataProvider`, then writes one row to
      indicator_snapshots. Per spec §4.7, every data-source failure is
      caught, the field is left null, and an event row is logged. The
      snapshot never crashes the bot.

  close_trade(conn, ds, *, ctx, scheduler, ...)
      On position close: writes the 'exit' snapshot, then the trades row
      with full P&L summary. Returns (snapshot_id, trade_id).

The scheduler is pure-in-memory and does no I/O. capture_snapshot and
close_trade do I/O against `conn` and the data provider but never call
`datetime.now()` — `now` is always a parameter.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Callable, Literal, TypeVar

import psycopg

from x_alpaca_trading_bot import journal
from x_alpaca_trading_bot.data_service import MarketDataProvider

logger = logging.getLogger(__name__)

SnapshotType = Literal["entry", "monitor", "exit"]
DEFAULT_INTERVAL = timedelta(minutes=15)

T = TypeVar("T")


@dataclass(frozen=True)
class SnapshotContext:
    """Identifies which position a snapshot belongs to."""

    signal_id: int                # FK to signals.id (snapshot row → signal)
    contract_symbol: str          # OCC, e.g. AAPL260620C00185000
    underlying_ticker: str        # e.g. AAPL


@dataclass(frozen=True)
class TrackedPosition:
    """Scheduler-side state for one open position."""

    ctx: SnapshotContext
    opened_at: datetime
    last_snapshot_at: datetime
    max_gain_pct: Decimal = Decimal(0)
    max_loss_pct: Decimal = Decimal(0)


class SnapshotScheduler:
    """Tracks open positions and tells callers when snapshots are due."""

    def __init__(self, *, interval: timedelta = DEFAULT_INTERVAL) -> None:
        if interval <= timedelta(0):
            raise ValueError(f"interval must be positive; got {interval}")
        self._interval = interval
        self._tracked: dict[int, TrackedPosition] = {}

    @property
    def interval(self) -> timedelta:
        return self._interval

    def register(
        self,
        ctx: SnapshotContext,
        *,
        opened_at: datetime,
    ) -> TrackedPosition:
        """Add a position. The entry snapshot's `last_snapshot_at` starts at
        `opened_at` so the first 'monitor' snapshot fires `interval` later.
        """
        if opened_at.tzinfo is None:
            raise ValueError("opened_at must be timezone-aware")
        position = TrackedPosition(ctx=ctx, opened_at=opened_at, last_snapshot_at=opened_at)
        self._tracked[ctx.signal_id] = position
        logger.info("scheduler: registered signal_id=%s ticker=%s", ctx.signal_id, ctx.underlying_ticker)
        return position

    def unregister(self, signal_id: int) -> None:
        if signal_id in self._tracked:
            del self._tracked[signal_id]
            logger.info("scheduler: unregistered signal_id=%s", signal_id)

    def positions_due(self, now: datetime) -> list[TrackedPosition]:
        """Return positions where `now - last_snapshot_at >= interval`."""
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        return [
            p for p in self._tracked.values()
            if (now - p.last_snapshot_at) >= self._interval
        ]

    def mark_snapshotted(self, signal_id: int, now: datetime) -> None:
        if signal_id in self._tracked:
            self._tracked[signal_id] = replace(self._tracked[signal_id], last_snapshot_at=now)

    def update_extremes(self, signal_id: int, gain_pct: Decimal) -> TrackedPosition | None:
        """Push the running max-gain / max-loss for this position.

        gain_pct is the position's current unrealized P&L as a fraction of
        entry price (e.g. Decimal("0.25") for +25%, Decimal("-0.10") for -10%).
        """
        position = self._tracked.get(signal_id)
        if position is None:
            return None
        updated = replace(
            position,
            max_gain_pct=max(position.max_gain_pct, gain_pct),
            max_loss_pct=min(position.max_loss_pct, gain_pct),
        )
        self._tracked[signal_id] = updated
        return updated

    def get(self, signal_id: int) -> TrackedPosition | None:
        return self._tracked.get(signal_id)

    def __len__(self) -> int:
        return len(self._tracked)


# ---- Capture ---------------------------------------------------------------

def _try_fetch(
    label: str,
    fn: Callable[[], T],
    *,
    conn: psycopg.Connection,
    ts: datetime,
    signal_id: int,
) -> T | None:
    """Call `fn`. On any exception, return None and log an event."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        logger.warning("snapshot fetch %s failed for signal_id=%s: %s", label, signal_id, exc)
        try:
            journal.insert_event(
                conn,
                ts=ts,
                severity="warning",
                category="snapshot",
                message=f"data_source_unavailable:{label}",
                context={"signal_id": signal_id, "error": repr(exc)},
            )
        except Exception:  # noqa: BLE001
            logger.exception("failed to write missing-source event")
        return None


def capture_snapshot(
    conn: psycopg.Connection,
    data_service: MarketDataProvider,
    ctx: SnapshotContext,
    *,
    snapshot_type: SnapshotType,
    now: datetime,
    option_expiration: date | None = None,
    option_type: str | None = None,
    strike: Decimal | None = None,
) -> int:
    """Capture one indicator_snapshots row for `ctx`.

    The full set of indicators is fetched independently from `data_service`;
    each fetch is wrapped so a single source failure produces NULL plus an
    event log, not an exception.

    Returns the new indicator_snapshots row id.

    option_expiration/type/strike are optional — pass them when known so
    the option_quote can be fetched. Without them, option_bid/ask/mid +
    bid_ask_spread_pct are NULL but the rest still captures.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    fetch = _try_fetch
    greeks = fetch("greeks", lambda: data_service.get_greeks(ctx.contract_symbol),
                   conn=conn, ts=now, signal_id=ctx.signal_id)
    iv_data = fetch("iv", lambda: data_service.get_iv_data(ctx.contract_symbol),
                    conn=conn, ts=now, signal_id=ctx.signal_id)
    indicators = fetch("indicators", lambda: data_service.get_indicators(ctx.underlying_ticker, now),
                       conn=conn, ts=now, signal_id=ctx.signal_id)
    market = fetch("market_context", lambda: data_service.get_market_context(now),
                   conn=conn, ts=now, signal_id=ctx.signal_id)
    underlying_price = fetch("underlying_price",
                             lambda: data_service.get_underlying_price(ctx.underlying_ticker),
                             conn=conn, ts=now, signal_id=ctx.signal_id)

    # Option quote requires expiration/type/strike to build the OCC symbol;
    # those come from the position the caller is snapshotting.
    quote = None
    if option_expiration is not None and option_type is not None and strike is not None:
        quote = fetch(
            "option_quote",
            lambda: data_service.get_option_quote(
                ctx.underlying_ticker, option_expiration, option_type,  # type: ignore[arg-type]
                strike,
            ),
            conn=conn, ts=now, signal_id=ctx.signal_id,
        )

    return journal.insert_indicator_snapshot(
        conn,
        signal_id=ctx.signal_id,
        ts=now,
        snapshot_type=snapshot_type,
        delta=getattr(greeks, "delta", None) if greeks else None,
        gamma=getattr(greeks, "gamma", None) if greeks else None,
        theta=getattr(greeks, "theta", None) if greeks else None,
        vega=getattr(greeks, "vega", None) if greeks else None,
        iv=getattr(iv_data, "iv", None) if iv_data else None,
        iv_rank=getattr(iv_data, "iv_rank", None) if iv_data else None,
        iv_percentile=getattr(iv_data, "iv_percentile", None) if iv_data else None,
        rsi_14=getattr(indicators, "rsi_14", None) if indicators else None,
        macd=getattr(indicators, "macd", None) if indicators else None,
        macd_signal=getattr(indicators, "macd_signal", None) if indicators else None,
        vwap=getattr(indicators, "vwap", None) if indicators else None,
        ema_9=getattr(indicators, "ema_9", None) if indicators else None,
        ema_21=getattr(indicators, "ema_21", None) if indicators else None,
        atr_14=getattr(indicators, "atr_14", None) if indicators else None,
        bb_position=getattr(indicators, "bb_position", None) if indicators else None,
        # options_volume / open_interest / put_call_ratio not yet sourced — None.
        bid_ask_spread_pct=getattr(quote, "spread_pct", None) if quote else None,
        vix=getattr(market, "vix", None) if market else None,
        spy_vs_ema21=getattr(market, "spy_vs_ema21", None) if market else None,
        sector_etf_trend=_summarize_sector(market),
        option_bid=getattr(quote, "bid", None) if quote else None,
        option_ask=getattr(quote, "ask", None) if quote else None,
        option_mid=getattr(quote, "mid", None) if quote else None,
        underlying_price=underlying_price,
    )


def _summarize_sector(market: Any) -> str | None:
    """Schema column is TEXT — pack the strongest mover into a short string."""
    if market is None:
        return None
    trend = getattr(market, "sector_etf_trend", None) or {}
    if not trend:
        return None
    # Format: "XLK+1.23,XLE-0.45,..." (top-3 absolute movers)
    items = sorted(trend.items(), key=lambda kv: abs(kv[1]), reverse=True)[:3]
    return ",".join(f"{sym}{float(pct):+.2%}" for sym, pct in items)


# ---- Close-trade orchestration --------------------------------------------

def close_trade(
    conn: psycopg.Connection,
    data_service: MarketDataProvider,
    *,
    ctx: SnapshotContext,
    scheduler: SnapshotScheduler,
    opened_at: datetime,
    closed_at: datetime,
    option_type: str,
    strike: Decimal,
    expiration: date,
    entry_price: Decimal,
    exit_price: Decimal,
    qty: int,
    exit_reason: str,
) -> tuple[int, int]:
    """Write exit snapshot AND trades row, then unregister from scheduler.

    Returns (snapshot_id, trade_id). Reads max_gain_pct / max_loss_pct from
    the scheduler's tracked state, if registered.
    """
    snapshot_id = capture_snapshot(
        conn,
        data_service,
        ctx,
        snapshot_type="exit",
        now=closed_at,
        option_expiration=expiration,
        option_type=option_type,
        strike=strike,
    )

    tracked = scheduler.get(ctx.signal_id)
    max_gain = tracked.max_gain_pct if tracked else None
    max_loss = tracked.max_loss_pct if tracked else None

    trade_id = journal.insert_trade(
        conn,
        signal_id=ctx.signal_id,
        opened_at=opened_at,
        closed_at=closed_at,
        ticker=ctx.underlying_ticker,
        option_type=option_type,
        strike=strike,
        expiration=expiration,
        entry_price=entry_price,
        exit_price=exit_price,
        qty=qty,
        exit_reason=exit_reason,
        max_gain_pct=max_gain,
        max_loss_pct=max_loss,
    )

    scheduler.unregister(ctx.signal_id)
    return snapshot_id, trade_id
