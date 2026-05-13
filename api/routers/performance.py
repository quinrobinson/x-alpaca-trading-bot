"""GET /performance — closed-trade history + aggregate stats."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Query, Request

router = APIRouter(prefix="/performance", tags=["performance"])


@router.get("", summary="Trade log + aggregate stats")
def get_performance(
    request: Request,
    limit: int = Query(default=200, ge=1, le=2000),
) -> dict[str, Any]:
    """Return: { stats: {...}, trades: [...] } from the trades table."""
    conn = request.app.state.conn
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, signal_id, opened_at, closed_at, ticker, option_type,
                   strike, expiration, entry_price, exit_price, qty, gross_pnl,
                   pnl_pct, exit_reason, hold_minutes, max_gain_pct, max_loss_pct
            FROM trades
            ORDER BY closed_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()

    trades = [
        {
            "id": r[0],
            "signal_id": r[1],
            "opened_at": r[2].isoformat() if r[2] else None,
            "closed_at": r[3].isoformat() if r[3] else None,
            "ticker": r[4],
            "option_type": r[5],
            "strike": str(r[6]),
            "expiration": r[7].isoformat() if r[7] else None,
            "entry_price": str(r[8]),
            "exit_price": str(r[9]),
            "qty": r[10],
            "gross_pnl": str(r[11]),
            "pnl_pct": str(r[12]),
            "exit_reason": r[13],
            "hold_minutes": r[14],
            "max_gain_pct": str(r[15]) if r[15] is not None else None,
            "max_loss_pct": str(r[16]) if r[16] is not None else None,
        }
        for r in rows
    ]
    return {"stats": _aggregate_stats(trades), "trades": trades}


def _aggregate_stats(trades: list[dict[str, Any]]) -> dict[str, Any]:
    if not trades:
        return {
            "total_trades": 0, "wins": 0, "losses": 0,
            "win_rate": None, "avg_win_pct": None, "avg_loss_pct": None,
            "profit_factor": None, "total_pnl": "0",
        }
    wins = [t for t in trades if Decimal(t["gross_pnl"]) > 0]
    losses = [t for t in trades if Decimal(t["gross_pnl"]) <= 0]
    total_pnl = sum((Decimal(t["gross_pnl"]) for t in trades), start=Decimal(0))

    win_pcts = [Decimal(t["pnl_pct"]) for t in wins]
    loss_pcts = [Decimal(t["pnl_pct"]) for t in losses]

    avg_win = (sum(win_pcts, start=Decimal(0)) / len(win_pcts)) if win_pcts else None
    avg_loss = (sum(loss_pcts, start=Decimal(0)) / len(loss_pcts)) if loss_pcts else None

    # Profit factor: gross wins / gross losses
    win_sum = sum(win_pcts, start=Decimal(0))
    loss_sum = sum(loss_pcts, start=Decimal(0))
    profit_factor = float(win_sum / abs(loss_sum)) if loss_sum < 0 else None

    return {
        "total_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": float(len(wins) / len(trades)),
        "avg_win_pct": str(avg_win) if avg_win is not None else None,
        "avg_loss_pct": str(avg_loss) if avg_loss is not None else None,
        "profit_factor": profit_factor,
        "total_pnl": str(total_pnl),
    }
