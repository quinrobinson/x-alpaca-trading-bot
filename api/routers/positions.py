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

    conn = request.app.state.conn
    signal_ids = [r.signal_id for r in records]
    posts_by_signal_id: dict[int, dict[str, Any]] = {}
    snapshot_by_signal_id: dict[int, dict[str, Any]] = {}

    if conn is not None:
        # Originating tweets.
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

        # Latest indicator snapshot per open position — feeds the
        # dashboard's Greeks & indicators panel. DISTINCT ON keeps only
        # the most recent ts for each signal_id.
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (signal_id)
                    signal_id, ts, snapshot_type,
                    delta, gamma, theta, vega, iv,
                    rsi_14, vwap, atr_14, option_mid
                FROM indicator_snapshots
                WHERE signal_id = ANY(%s)
                ORDER BY signal_id, ts DESC
                """,
                (signal_ids,),
            )
            cols = [
                "signal_id", "ts", "snapshot_type",
                "delta", "gamma", "theta", "vega", "iv",
                "rsi_14", "vwap", "atr_14", "option_mid",
            ]
            for row in cur.fetchall():
                d = dict(zip(cols, row))
                sid = d.pop("signal_id")
                # Stringify numerics + the timestamp for clean JSON.
                snapshot_by_signal_id[sid] = {
                    k: (v.isoformat() if k == "ts" and v is not None
                        else str(v) if v is not None else None)
                    for k, v in d.items()
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
            # Live option mid the orchestrator saw on its last tick (~5s
            # fresh). None until the first tick after the position opens.
            # getattr keeps this resilient to any record shape that
            # predates the field.
            "live_mid": (
                str(getattr(record, "last_option_mid", None))
                if getattr(record, "last_option_mid", None) is not None else None
            ),
            "snapshot": snapshot_by_signal_id.get(record.signal_id),
            "source_post": posts_by_signal_id.get(record.signal_id),
        })
    return out
