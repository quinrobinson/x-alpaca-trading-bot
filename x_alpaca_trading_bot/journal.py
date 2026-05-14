"""journal — DB writes for the bot.

Phase 2: x_posts. Phase 3: signals. Phase 5: events. Phase 6: orders + fills.
Phase 7: indicator_snapshots + trades.

This module owns every INSERT/UPDATE against the database. Later phases add
pnl_snapshots. Telegram alerts also live here per spec §2.2.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from decimal import Decimal
from typing import Any

import psycopg

logger = logging.getLogger(__name__)


def insert_raw_post(
    conn: psycopg.Connection,
    *,
    post_id: str,
    post_text: str,
    posted_at: datetime,
    received_at: datetime,
    parse_result: dict[str, Any] | None,
    actionable: bool,
) -> int:
    """Insert (or upsert) a row into x_posts; return the row id.

    Upsert semantics: stream re-deliveries on reconnect must not crash. The
    post_id column is UNIQUE; on conflict we update parse_result and
    actionable in case the parse rerun produced a better classification.

    parse_result must already be JSON-serializable (e.g. via
    parser.parse_result_to_journal_dict). Pass None for a non-parsed write.
    """
    payload = json.dumps(parse_result) if parse_result is not None else None
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO x_posts
                (posted_at, received_at, post_id, post_text, parse_result, actionable)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (post_id) DO UPDATE
              SET parse_result = EXCLUDED.parse_result,
                  actionable   = EXCLUDED.actionable
            RETURNING id
            """,
            (posted_at, received_at, post_id, post_text, payload, actionable),
        )
        row = cur.fetchone()
    conn.commit()
    assert row is not None
    return int(row[0])


def insert_signal(
    conn: psycopg.Connection,
    *,
    x_post_id: int,
    parsed_at: datetime,
    ticker: str,
    option_type: str,
    strike: Decimal,
    expiration: "datetime | Any",  # date or datetime — caller passes date
    posted_price: Decimal,
    live_ask: Decimal | None,
    taken: bool,
    rejection_reason: str | None,
    gate_results: dict[str, Any],
) -> int:
    """Insert a row into signals; return the new id.

    Every validated (or rejected) signal gets a row here regardless of outcome,
    so the post-trade analysis can compare what we skipped vs. what we took.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO signals
                (x_post_id, parsed_at, ticker, option_type, strike, expiration,
                 posted_price, live_ask, taken, rejection_reason, gate_results)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                x_post_id,
                parsed_at,
                ticker,
                option_type,
                strike,
                expiration,
                posted_price,
                live_ask,
                taken,
                rejection_reason,
                json.dumps(gate_results),
            ),
        )
        row = cur.fetchone()
    conn.commit()
    assert row is not None
    return int(row[0])


def update_signal_rejection(
    conn: psycopg.Connection,
    *,
    signal_id: int,
    rejection_reason: str,
) -> None:
    """Backfill a rejection_reason on a signal that was validated-accepted
    but rejected downstream (risk kill switch, spend cap, fill timeout).

    The signal row was inserted at validate-time with rejection_reason=NULL
    so post-hoc analysis can tell why the bot skipped it. No-op if the
    row is gone (shouldn't happen, but defensive).
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE signals SET rejection_reason = %s "
            "WHERE id = %s AND rejection_reason IS NULL",
            (rejection_reason, signal_id),
        )
    conn.commit()


def insert_event(
    conn: psycopg.Connection,
    *,
    ts: datetime,
    severity: str,
    category: str,
    message: str,
    context: dict[str, Any] | None = None,
) -> int:
    """Insert a row into the events table; return the new id.

    Used by risk_manager.evaluate_and_log() and by the orchestrator for
    kill-switch trips, connection events, errors, and other system events.

    severity: 'info' | 'warning' | 'error' | 'critical'
    category: 'risk' | 'kill_switch' | 'fill' | 'system' | 'connection' | ...
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO events (ts, severity, category, message, context)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                ts,
                severity,
                category,
                message,
                json.dumps(_jsonable(context)) if context is not None else None,
            ),
        )
        row = cur.fetchone()
    conn.commit()
    assert row is not None
    return int(row[0])


def insert_order(
    conn: psycopg.Connection,
    *,
    signal_id: int | None,
    alpaca_order_id: str,
    submitted_at: datetime,
    symbol: str,
    side: str,
    qty: int,
    order_type: str,
    limit_price: Decimal | None,
    stop_price: Decimal | None,
    status: str,
    raw: dict[str, Any],
) -> int:
    """Insert a row into orders; return the row id.

    Upserts on alpaca_order_id (UNIQUE) so retries during reconciliation
    don't duplicate rows. Status + raw payload are refreshed on conflict.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO orders
                (signal_id, alpaca_order_id, submitted_at, symbol, side, qty,
                 order_type, limit_price, stop_price, status, raw)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (alpaca_order_id) DO UPDATE
              SET status = EXCLUDED.status,
                  raw    = EXCLUDED.raw
            RETURNING id
            """,
            (
                signal_id,
                alpaca_order_id,
                submitted_at,
                symbol,
                side,
                qty,
                order_type,
                limit_price,
                stop_price,
                status,
                json.dumps(_jsonable(raw)),
            ),
        )
        row = cur.fetchone()
    conn.commit()
    assert row is not None
    return int(row[0])


def insert_fill(
    conn: psycopg.Connection,
    *,
    order_id: int,
    filled_at: datetime,
    symbol: str,
    side: str,
    qty: int,
    fill_price: Decimal,
    commission: Decimal = Decimal(0),
) -> int:
    """Insert a row into fills; return the row id.

    order_id is the local journal row id from insert_order, NOT alpaca's
    UUID. The orchestrator looks up the local id after upserting the
    order, then inserts the fill linked to it.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO fills
                (order_id, filled_at, symbol, side, qty, fill_price, commission)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (order_id, filled_at, symbol, side, qty, fill_price, commission),
        )
        row = cur.fetchone()
    conn.commit()
    assert row is not None
    return int(row[0])


def insert_indicator_snapshot(
    conn: psycopg.Connection,
    *,
    signal_id: int,
    ts: datetime,
    snapshot_type: str,
    delta: Decimal | None = None,
    gamma: Decimal | None = None,
    theta: Decimal | None = None,
    vega: Decimal | None = None,
    iv: Decimal | None = None,
    iv_rank: Decimal | None = None,
    iv_percentile: Decimal | None = None,
    rsi_14: Decimal | None = None,
    macd: Decimal | None = None,
    macd_signal: Decimal | None = None,
    vwap: Decimal | None = None,
    ema_9: Decimal | None = None,
    ema_21: Decimal | None = None,
    atr_14: Decimal | None = None,
    bb_position: Decimal | None = None,
    options_volume: int | None = None,
    open_interest: int | None = None,
    put_call_ratio: Decimal | None = None,
    bid_ask_spread_pct: Decimal | None = None,
    vix: Decimal | None = None,
    spy_vs_ema21: str | None = None,
    sector_etf_trend: str | None = None,
    option_bid: Decimal | None = None,
    option_ask: Decimal | None = None,
    option_mid: Decimal | None = None,
    underlying_price: Decimal | None = None,
) -> int:
    """Insert a row into indicator_snapshots; return the new id.

    snapshot_type is one of: 'entry', 'monitor', 'exit'. All indicator
    fields are nullable — missing values become NULL per spec §4.7
    ("if a data source is unavailable, write null with an event log
    entry, don't crash").
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO indicator_snapshots
                (signal_id, ts, snapshot_type,
                 delta, gamma, theta, vega,
                 iv, iv_rank, iv_percentile,
                 rsi_14, macd, macd_signal, vwap, ema_9, ema_21, atr_14, bb_position,
                 options_volume, open_interest, put_call_ratio, bid_ask_spread_pct,
                 vix, spy_vs_ema21, sector_etf_trend,
                 option_bid, option_ask, option_mid, underlying_price)
            VALUES (%s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s, %s)
            RETURNING id
            """,
            (
                signal_id, ts, snapshot_type,
                delta, gamma, theta, vega,
                iv, iv_rank, iv_percentile,
                rsi_14, macd, macd_signal, vwap, ema_9, ema_21, atr_14, bb_position,
                options_volume, open_interest, put_call_ratio, bid_ask_spread_pct,
                vix, spy_vs_ema21, sector_etf_trend,
                option_bid, option_ask, option_mid, underlying_price,
            ),
        )
        row = cur.fetchone()
    conn.commit()
    assert row is not None
    return int(row[0])


def insert_trade(
    conn: psycopg.Connection,
    *,
    signal_id: int | None,
    opened_at: datetime,
    closed_at: datetime,
    ticker: str,
    option_type: str,
    strike: Decimal,
    expiration: Any,         # date
    entry_price: Decimal,
    exit_price: Decimal,
    qty: int,
    exit_reason: str,
    max_gain_pct: Decimal | None = None,
    max_loss_pct: Decimal | None = None,
) -> int:
    """Insert a row into trades on position close; return the new id.

    gross_pnl, pnl_pct, hold_minutes are derived here rather than supplied
    by the caller, so the math lives in one place.
    """
    gross_pnl = (exit_price - entry_price) * Decimal(qty)
    if entry_price > 0:
        pnl_pct = (exit_price - entry_price) / entry_price
    else:
        pnl_pct = Decimal(0)
    hold_minutes = int((closed_at - opened_at).total_seconds() / 60)

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO trades
                (signal_id, opened_at, closed_at, ticker, option_type, strike,
                 expiration, entry_price, exit_price, qty, gross_pnl, pnl_pct,
                 exit_reason, hold_minutes, max_gain_pct, max_loss_pct)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                signal_id, opened_at, closed_at, ticker, option_type, strike,
                expiration, entry_price, exit_price, qty, gross_pnl, pnl_pct,
                exit_reason, hold_minutes, max_gain_pct, max_loss_pct,
            ),
        )
        row = cur.fetchone()
    conn.commit()
    assert row is not None
    return int(row[0])


def _jsonable(value: Any) -> Any:
    """Coerce Decimals/datetimes to JSON-friendly primitives recursively."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value
