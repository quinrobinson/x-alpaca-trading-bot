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
        # The WS-driven path serializes sector trend as the CSV string
        # "XLK+1.67%,XLF+0.69%,...". The dashboard's MarketContext parses
        # that shape, so format the dict the same way here for a single
        # render path.
        "sector_etf_trend": _format_sector_trend(ctx.sector_etf_trend),
    }


def _as_str(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _format_sector_trend(trend: Any) -> str | None:
    if not trend:
        return None
    # data_service returns a dict[str, Decimal]. Serialize as
    # "XLK+1.67%,XLF+0.69%,..." with 2 decimals, ordered by absolute
    # change descending so the strongest movers anchor the heatmap.
    items: list[tuple[str, Decimal]] = []
    for symbol, value in trend.items():
        try:
            items.append((symbol, Decimal(str(value))))
        except (ArithmeticError, ValueError):
            continue
    items.sort(key=lambda kv: abs(kv[1]), reverse=True)
    chunks = []
    for symbol, value in items:
        pct = value * Decimal("100")
        sign = "+" if pct >= 0 else "-"
        chunks.append(f"{symbol}{sign}{abs(pct):.2f}%")
    return ",".join(chunks) if chunks else None
