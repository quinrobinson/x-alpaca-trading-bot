"""GET /positions — currently-open positions read from orchestrator state."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

router = APIRouter(prefix="/positions", tags=["positions"])


@router.get("", summary="List currently open positions")
def list_open_positions(request: Request) -> list[dict[str, Any]]:
    """Return the orchestrator's in-memory open positions.

    Each entry now also carries the originating tweet so the dashboard can
    show the post that triggered the trade alongside the live state.
    Orchestrator owns the open-position truth; the tweet is looked up by
    signal_id from the journal.
    """
    orch = request.app.state.orchestrator
    if orch is None:
        return []

    records = list(orch._open_positions.values())
    if not records:
        return []

    # One DB roundtrip to fetch all originating posts for the open signals.
    conn = request.app.state.conn
    posts_by_signal_id: dict[int, dict[str, Any]] = {}
    if conn is not None:
        signal_ids = [r.signal_id for r in records]
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.id, x.post_text, x.posted_at
                FROM signals s
                JOIN x_posts x ON x.id = s.x_post_id
                WHERE s.id = ANY(%s)
                """,
                (signal_ids,),
            )
            for sid, text, posted_at in cur.fetchall():
                posts_by_signal_id[sid] = {
                    "post_text": text,
                    "posted_at": posted_at.isoformat() if posted_at else None,
                }

    out: list[dict[str, Any]] = []
    for record in records:
        sp = record.strategy_position
        out.append({
            "signal_id": record.signal_id,
            "ticker": record.ticker,
            "contract_symbol": record.contract_symbol,
            "option_type": record.option_type,
            "strike": str(record.strike),
            "expiration": record.expiration.isoformat(),
            "qty": record.qty,
            "entry_price": str(record.entry_price),
            "opened_at": record.opened_at.isoformat(),
            "current_stop_price": str(sp.stop_price),
            "ratchet_level": sp.ratchet_level,
            "stop_order_id": record.stop_order_id,
            "source_post": posts_by_signal_id.get(record.signal_id),
        })
    return out
