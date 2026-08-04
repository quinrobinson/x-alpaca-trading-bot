"""GET /timeline — unified "what happened" feed across both books.

Returns a most-recent-first merge of three sources:
  - The legacy X chain (post → signal → trade). Frozen history since the
    X strategy's retirement (SCANNER_PROGRAM.md), kept inline as the
    baseline record.
  - Scanner lab detections (scanner_events) — one entry per event.
  - Scanner equity trades (scanner_trades) — the S2 book.

Every item carries `ts` (the merge key) and a globally unique `key`
(x-/se-/st- prefixed) for frontend list identity.

The frontend uses `kind` to pick a render style:

    trade_closed         — X options trade closed (won/lost). Show P&L.
    position_open        — X signal taken, still open.
    signal_rejected      — parsed cleanly but validator/risk refused.
    signal_unactionable  — post received but parse said "not a signal".
    scanner_event        — a lab hypothesis logged a detection.
    scanner_trade_open   — S2 short currently open.
    scanner_trade_closed — S2 short closed. Show P&L.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request

from api.db_dep import resolve_conn

router = APIRouter(prefix="/timeline", tags=["timeline"])


@router.get("", summary="Unified feed across the X archive + scanner program")
def get_timeline(
    request: Request,
    limit: int = Query(default=50, ge=1, le=500),
    include_rejected: bool = Query(default=True),
) -> list[dict[str, Any]]:
    # Each source is fetched up to `limit` rows, then merged by `ts` and
    # cut — so the newest `limit` entries win regardless of which book
    # they came from.
    with resolve_conn(request) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                x.id, x.posted_at, x.received_at, x.post_text,
                x.parse_result, x.actionable,
                s.id, s.parsed_at, s.ticker, s.option_type, s.strike,
                s.expiration, s.posted_price, s.live_ask, s.taken,
                s.rejection_reason,
                t.id, t.opened_at, t.closed_at, t.entry_price, t.exit_price,
                t.qty, t.gross_pnl, t.pnl_pct, t.exit_reason, t.hold_minutes,
                t.max_gain_pct, t.max_loss_pct
            FROM x_posts x
            LEFT JOIN signals s ON s.x_post_id = x.id
            LEFT JOIN trades  t ON t.signal_id = s.id
            ORDER BY x.posted_at DESC, x.id DESC
            LIMIT %s
            """,
            (limit,),
        )
        x_rows = cur.fetchall()
        cur.execute(
            """
            SELECT id, scanner_name, ticker, detected_at,
                   breakout_ts, breakout_price, failure_ts, failure_price,
                   prior_high, volume_ratio
            FROM scanner_events
            ORDER BY detected_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        ev_rows = cur.fetchall()
        cur.execute(
            """
            SELECT id, scanner_name, ticker, qty, entry_price, opened_at,
                   closed_at, exit_price, gross_pnl, pnl_pct, exit_reason
            FROM scanner_trades
            ORDER BY COALESCE(closed_at, opened_at) DESC
            LIMIT %s
            """,
            (limit,),
        )
        tr_rows = cur.fetchall()

    items: list[dict[str, Any]] = []
    for r in x_rows:
        item = _row_to_item(r)
        if not include_rejected and item["kind"] == "signal_rejected":
            continue
        items.append(item)
    items.extend(_scanner_event_item(r) for r in ev_rows)
    items.extend(_scanner_trade_item(r) for r in tr_rows)

    items.sort(key=lambda i: i.get("ts") or "", reverse=True)
    return items[:limit]


def _scanner_event_item(r: tuple) -> dict[str, Any]:
    (ev_id, scanner_name, ticker, detected_at,
     breakout_ts, breakout_price, failure_ts, failure_price,
     prior_high, volume_ratio) = r
    return {
        "kind": "scanner_event",
        "key": f"se-{ev_id}",
        "ts": detected_at.isoformat() if detected_at else None,
        "scanner_name": scanner_name,
        "ticker": ticker,
        "breakout_ts": breakout_ts.isoformat() if breakout_ts else None,
        "breakout_price": str(breakout_price) if breakout_price is not None else None,
        "failure_ts": failure_ts.isoformat() if failure_ts else None,
        "failure_price": str(failure_price) if failure_price is not None else None,
        "prior_high": str(prior_high) if prior_high is not None else None,
        "volume_ratio": str(volume_ratio) if volume_ratio is not None else None,
    }


def _scanner_trade_item(r: tuple) -> dict[str, Any]:
    (tr_id, scanner_name, ticker, qty, entry_price, opened_at,
     closed_at, exit_price, gross_pnl, pnl_pct, exit_reason) = r
    is_open = closed_at is None
    ts = opened_at if is_open else closed_at
    return {
        "kind": "scanner_trade_open" if is_open else "scanner_trade_closed",
        "key": f"st-{tr_id}",
        "ts": ts.isoformat() if ts else None,
        "scanner_name": scanner_name,
        "ticker": ticker,
        "qty": int(qty),
        "entry_price": str(entry_price) if entry_price is not None else None,
        "opened_at": opened_at.isoformat() if opened_at else None,
        "closed_at": closed_at.isoformat() if closed_at else None,
        "exit_price": str(exit_price) if exit_price is not None else None,
        "gross_pnl": str(gross_pnl) if gross_pnl is not None else None,
        "pnl_pct": str(pnl_pct) if pnl_pct is not None else None,
        "exit_reason": exit_reason,
    }


def _row_to_item(r: tuple) -> dict[str, Any]:
    (
        x_id, x_posted_at, x_received_at, x_text, x_parse, x_actionable,
        s_id, s_parsed_at, s_ticker, s_type, s_strike,
        s_exp, s_posted_price, s_live_ask, s_taken, s_reject,
        t_id, t_opened_at, t_closed_at, t_entry, t_exit,
        t_qty, t_gross, t_pnl_pct, t_reason, t_hold,
        t_max_gain, t_max_loss,
    ) = r

    signal: dict[str, Any] | None = None
    if s_id is not None:
        signal = {
            "id": s_id,
            "parsed_at": s_parsed_at.isoformat() if s_parsed_at else None,
            "ticker": s_ticker,
            "option_type": s_type,
            "strike": str(s_strike) if s_strike is not None else None,
            "expiration": s_exp.isoformat() if s_exp else None,
            "posted_price": str(s_posted_price) if s_posted_price is not None else None,
            "live_ask": str(s_live_ask) if s_live_ask is not None else None,
            "taken": bool(s_taken) if s_taken is not None else None,
            "rejection_reason": s_reject,
        }

    trade: dict[str, Any] | None = None
    if t_id is not None:
        trade = {
            "id": t_id,
            "opened_at": t_opened_at.isoformat() if t_opened_at else None,
            "closed_at": t_closed_at.isoformat() if t_closed_at else None,
            "entry_price": str(t_entry) if t_entry is not None else None,
            "exit_price": str(t_exit) if t_exit is not None else None,
            "qty": t_qty,
            "gross_pnl": str(t_gross) if t_gross is not None else None,
            "pnl_pct": str(t_pnl_pct) if t_pnl_pct is not None else None,
            "exit_reason": t_reason,
            "hold_minutes": t_hold,
            "max_gain_pct": str(t_max_gain) if t_max_gain is not None else None,
            "max_loss_pct": str(t_max_loss) if t_max_loss is not None else None,
        }

    # Classify for the frontend.
    if trade is not None:
        kind = "trade_closed"
    elif signal is None:
        kind = "signal_unactionable"
    elif signal["taken"] is True:
        kind = "position_open"            # taken but no trade row → still open
    else:
        kind = "signal_rejected"

    # ts drives the cross-source merge: a closed trade sorts by its close
    # (when it became news), otherwise the post's own timestamp.
    if trade is not None and t_closed_at is not None:
        ts = t_closed_at
    else:
        ts = x_posted_at or x_received_at

    return {
        "kind": kind,
        "key": f"x-{x_id}",
        "ts": ts.isoformat() if ts else None,
        "x_post_id": x_id,
        "posted_at": x_posted_at.isoformat() if x_posted_at else None,
        "received_at": x_received_at.isoformat() if x_received_at else None,
        "post_text": x_text,
        "actionable": bool(x_actionable) if x_actionable is not None else False,
        "signal": signal,
        "trade": trade,
    }
