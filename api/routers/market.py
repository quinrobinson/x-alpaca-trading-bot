"""GET /market — current VIX, SPY/QQQ trend, sector heatmap.

The Home view polls this so MarketContext shows real data even when
there are no open positions. The orchestrator's snapshot scheduler only
captures market state alongside position monitors, so without a
position the dashboard would otherwise be blank.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/market", tags=["market"])


@router.get("", summary="Snapshot of VIX, SPY/QQQ trend, sector heatmap")
def get_market(request: Request) -> dict[str, Any]:
    ds = getattr(request.app.state, "data_service", None)
    if ds is None:
        raise HTTPException(status_code=503, detail="data_service not initialized")
    try:
        ctx = ds.get_market_context(datetime.now(timezone.utc))
    except Exception as exc:  # noqa: BLE001
        # Polygon / Alpaca outages shouldn't 500 the dashboard — return a
        # null payload so the UI can show "—".
        raise HTTPException(status_code=502, detail=f"market data unavailable: {exc}") from exc
    return {
        "vix": _as_str(ctx.vix),
        "spy_vs_ema21": ctx.spy_vs_ema21,
        "qqq_vs_ema21": ctx.qqq_vs_ema21,
        "sector_etf_trend": ctx.sector_etf_trend,
    }


def _as_str(value: Decimal | None) -> str | None:
    return None if value is None else str(value)
