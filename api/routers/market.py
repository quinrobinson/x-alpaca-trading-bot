"""GET /market — current VIX, SPY/QQQ trend, sector heatmap.
GET /market/bars — OHLC bars for an underlying ticker (chart on position cards).

The Home view polls this so MarketContext shows real data even when
there are no open positions. The orchestrator's snapshot scheduler only
captures market state alongside position monitors, so without a
position the dashboard would otherwise be blank.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

router = APIRouter(prefix="/market", tags=["market"])

# Supported chart timeframes. Restrict to a small whitelist so we don't
# accept arbitrary user input into the Alpaca request layer.
_TIMEFRAME_MINUTES: dict[str, int] = {
    "1m": 1,
    "5m": 5,
    "15m": 15,
}


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


@router.get("/bars", summary="OHLC bars for an underlying ticker")
def get_bars(
    request: Request,
    ticker: str = Query(..., min_length=1, max_length=8),
    timeframe: str = Query("5m"),
    limit: int = Query(60, ge=1, le=200),
) -> list[dict[str, Any]]:
    """Return up to `limit` OHLC bars for the given underlying.

    Used by the open-position-card mini chart. Feeds Alpaca IEX bars
    directly through the data_service so we don't need to cache. Returns
    bars oldest-first with `ts` as ISO 8601 UTC and prices as strings to
    preserve Decimal precision through JSON.
    """
    ds = getattr(request.app.state, "data_service", None)
    if ds is None:
        raise HTTPException(status_code=503, detail="data_service not initialized")

    minutes = _TIMEFRAME_MINUTES.get(timeframe.strip().lower())
    if minutes is None:
        raise HTTPException(
            status_code=400,
            detail=f"timeframe must be one of {sorted(_TIMEFRAME_MINUTES)}",
        )

    try:
        bars = ds.get_underlying_bars(
            ticker.upper(),
            datetime.now(timezone.utc),
            timeframe_minutes=minutes,
            limit=limit,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"bars unavailable: {exc}") from exc

    return [
        {
            "ts": b.ts.isoformat(),
            "open": str(b.open),
            "high": str(b.high),
            "low": str(b.low),
            "close": str(b.close),
            "volume": b.volume,
        }
        for b in bars
    ]


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
