"""GET /timeline — unified feed of tweets + their downstream signals + trades.

Returns most-recent-first list. Each entry pairs:
  - The X post text (the tweet that triggered the chain)
  - The parsed signal (if any) with validation outcome
  - The resulting trade (if the signal was taken and has since closed)

The frontend uses `kind` to pick a render style:

    trade_closed      — taken, position closed (won/lost). Show P&L.
    position_open     — taken, still open. (Excluded by default; the
                        orchestrator's open position list owns that view.)
    signal_rejected   — parsed cleanly but validator/risk refused.
    signal_unactionable — post received but parse said "not a signal".
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request

router = APIRouter(prefix="/timeline", tags=["timeline"])


@router.get("", summary="Unified post/signal/trade feed, most recent first")
def get_timeline(
    request: Request,
    limit: int = Query(default=50, ge=1, le=500),
    include_rejected: bool = Query(default=True),
) -> list[dict[str, Any]]:
    conn = request.app.state.conn
    with conn.cursor() as cur:
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
        rows = cur.fetchall()

    items: list[dict[str, Any]] = []
    for r in rows:
        item = _row_to_item(r)
        if not include_rejected and item["kind"] == "signal_rejected":
            continue
        items.append(item)
    return items


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

    return {
        "kind": kind,
        "x_post_id": x_id,
        "posted_at": x_posted_at.isoformat() if x_posted_at else None,
        "received_at": x_received_at.isoformat() if x_received_at else None,
        "post_text": x_text,
        "actionable": bool(x_actionable) if x_actionable is not None else False,
        "signal": signal,
        "trade": trade,
    }
