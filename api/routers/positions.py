"""GET /positions — currently-open positions read from orchestrator state."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

router = APIRouter(prefix="/positions", tags=["positions"])


@router.get("", summary="List currently open positions")
def list_open_positions(request: Request) -> list[dict[str, Any]]:
    """Return the orchestrator's in-memory open positions.

    Orchestrator owns the truth — DB tables don't have a "currently open"
    flag; instead the orchestrator's dict is the source.
    """
    orch = request.app.state.orchestrator
    if orch is None:
        return []
    out: list[dict[str, Any]] = []
    for record in orch._open_positions.values():
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
        })
    return out
