"""POST /debug/inject-post — manually push a synthetic X post into the bot.

Disabled by default. Set DEBUG_INJECT_TOKEN in .env to enable; the endpoint
then requires `Authorization: Bearer <token>` on every request.

This routes through `Orchestrator._on_x_post`, which is the exact same entry
point the real X stream uses, so the injected post gets parsed → validated
→ risk-checked → executed → snapshotted → broadcast just like a real signal.

Useful when:
- Your X Developer account is rate-limited / out of credits.
- You want to deterministically test a specific contract setup.
- You're driving an end-to-end smoke during the manual Phase 6 verification.

Example:
    curl -X POST https://x-alpaca-bot.qr-project.dev/debug/inject-post \\
        -H "Authorization: Bearer $DEBUG_INJECT_TOKEN" \\
        -H "Content-Type: application/json" \\
        -d '{"post_text":"$AAPL 6/20 $185c @ 2.50"}'
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/debug", tags=["debug"])


def _require_token(authorization: str | None = Header(default=None)) -> None:
    """Dependency: validate the Authorization header against DEBUG_INJECT_TOKEN.

    Refuses the request with 503 if the token isn't configured (production
    safety default) or 401 if the token doesn't match.
    """
    expected = os.environ.get("DEBUG_INJECT_TOKEN", "")
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="debug endpoints disabled (DEBUG_INJECT_TOKEN unset)",
        )
    if authorization != f"Bearer {expected}":
        raise HTTPException(status_code=401, detail="invalid token")


@router.post("/inject-post", dependencies=[Depends(_require_token)])
async def inject_post(payload: dict[str, Any], request: Request) -> dict[str, Any]:
    """Inject a synthetic X post into the orchestrator's event queue.

    Body:
        post_text     (str, required): the raw X post text to parse
        post_id       (str, optional): deduplication id; defaults to a random UUID
        posted_at     (str, optional): ISO-8601 timestamp; defaults to now()
    """
    orch = request.app.state.orchestrator
    if orch is None:
        raise HTTPException(status_code=503, detail="orchestrator not attached")

    post_text = payload.get("post_text")
    if not post_text or not isinstance(post_text, str):
        raise HTTPException(status_code=400, detail="post_text is required")

    post_id = payload.get("post_id") or f"manual-{uuid.uuid4().hex[:12]}"

    posted_at_raw = payload.get("posted_at")
    if posted_at_raw:
        try:
            posted_at = datetime.fromisoformat(posted_at_raw.replace("Z", "+00:00"))
            if posted_at.tzinfo is None:
                posted_at = posted_at.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError, AttributeError) as exc:
            raise HTTPException(status_code=400, detail=f"invalid posted_at: {exc}")
    else:
        posted_at = datetime.now(timezone.utc)

    logger.info("debug inject_post id=%s text=%r posted_at=%s",
                post_id, post_text[:80], posted_at)

    # Route through the same callback the real X stream uses. This pushes
    # onto the orchestrator's queue; the next tick drains it.
    try:
        orch._on_x_post(post_id, post_text, posted_at)
    except Exception as exc:  # noqa: BLE001
        logger.exception("inject_post crashed")
        raise HTTPException(status_code=500, detail=str(exc))

    return {
        "queued": True,
        "post_id": post_id,
        "posted_at": posted_at.isoformat(),
        "queue_depth": orch._post_queue.qsize(),
    }
