"""GET /signals — recent signal history from the signals table."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request

router = APIRouter(prefix="/signals", tags=["signals"])


@router.get("", summary="Recent signals (most recent first)")
def list_signals(
    request: Request,
    limit: int = Query(default=50, ge=1, le=500),
) -> list[dict[str, Any]]:
    """Return up to `limit` signals, most recent first."""
    conn = request.app.state.conn
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, x_post_id, parsed_at, ticker, option_type, strike,
                   expiration, posted_price, live_ask, taken, rejection_reason
            FROM signals
            ORDER BY id DESC
            LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()
    return [
        {
            "id": r[0],
            "x_post_id": r[1],
            "parsed_at": r[2].isoformat() if r[2] else None,
            "ticker": r[3],
            "option_type": r[4],
            "strike": str(r[5]),
            "expiration": r[6].isoformat() if r[6] else None,
            "posted_price": str(r[7]),
            "live_ask": str(r[8]) if r[8] is not None else None,
            "taken": bool(r[9]),
            "rejection_reason": r[10],
        }
        for r in rows
    ]
