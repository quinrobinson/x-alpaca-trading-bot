"""GET / PATCH /config — the runtime settings endpoints.

Authentication: handled at the network edge by Cloudflare Access. The
FastAPI process trusts that any request arriving here has been gated.
The dashboard is served from the same origin so the Access cookie is
attached automatically.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, field_validator

from x_alpaca_trading_bot.config_store import BotConfigStore

router = APIRouter(prefix="/config", tags=["config"])


class ConfigResponse(BaseModel):
    """Shape returned to the dashboard."""

    max_position_spend_usd: str  # serialized Decimal — keep exact precision
    max_qty_per_position: int
    daily_loss_kill_pct: str
    disable_x_stream: bool


class ConfigPatch(BaseModel):
    """Partial update payload. All fields optional; null means "leave alone"."""

    model_config = ConfigDict(extra="forbid")

    max_position_spend_usd: Decimal | None = None
    max_qty_per_position: int | None = None
    daily_loss_kill_pct: Decimal | None = None
    disable_x_stream: bool | None = None

    @field_validator("max_position_spend_usd", "daily_loss_kill_pct", mode="before")
    @classmethod
    def _coerce_decimal(cls, v: Any) -> Any:
        # Accept strings, ints, floats. Float → Decimal goes via str to avoid
        # binary-float surprises.
        if v is None or isinstance(v, Decimal):
            return v
        if isinstance(v, (int, float)):
            try:
                return Decimal(str(v))
            except InvalidOperation as exc:
                raise ValueError(f"not a valid decimal: {v!r}") from exc
        if isinstance(v, str):
            try:
                return Decimal(v)
            except InvalidOperation as exc:
                raise ValueError(f"not a valid decimal: {v!r}") from exc
        raise ValueError(f"unsupported type for decimal: {type(v).__name__}")


def _store(request: Request) -> BotConfigStore:
    store: BotConfigStore | None = getattr(request.app.state, "config_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="config store not initialized")
    return store


@router.get("", response_model=ConfigResponse, summary="Read current bot config")
def get_config(request: Request) -> ConfigResponse:
    snap = _store(request).snapshot()
    return ConfigResponse(
        max_position_spend_usd=str(snap.max_position_spend_usd),
        max_qty_per_position=snap.max_qty_per_position,
        daily_loss_kill_pct=str(snap.daily_loss_kill_pct),
        disable_x_stream=snap.disable_x_stream,
    )


@router.patch("", response_model=ConfigResponse, summary="Update one or more settings")
def patch_config(payload: ConfigPatch, request: Request) -> ConfigResponse:
    store = _store(request)
    try:
        new_snap = store.update(
            max_position_spend_usd=payload.max_position_spend_usd,
            max_qty_per_position=payload.max_qty_per_position,
            daily_loss_kill_pct=payload.daily_loss_kill_pct,
            disable_x_stream=payload.disable_x_stream,
        )
    except ValueError as exc:
        # Out-of-bounds value: surface a 422 with the validation message.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ConfigResponse(
        max_position_spend_usd=str(new_snap.max_position_spend_usd),
        max_qty_per_position=new_snap.max_qty_per_position,
        daily_loss_kill_pct=str(new_snap.daily_loss_kill_pct),
        disable_x_stream=new_snap.disable_x_stream,
    )
