"""Scanner endpoints — observability for the Phase-A failed-breakout scanner.

The scanner runs as a background thread inside the orchestrator and writes
detected events to scanner_events. Phase A is log-only: these endpoints
expose what the scanner has logged so the operator can decide whether
detections look like real setups before promoting to Phase B (live trading).

Endpoints:
    GET /scanner/status — config + universe + last-event timestamp
    GET /scanner/events — paginated events with optional ticker/window filters
    GET /scanner/daily  — per-day event counts over the last N days
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Query, Request

router = APIRouter(prefix="/scanner", tags=["scanner"])


@router.get("/status", summary="Scanner enablement + universe + recent activity")
def scanner_status(request: Request) -> dict[str, Any]:
    """Return everything the dashboard needs to render the top status row.

    Reads scanner config from the orchestrator (so changes via env on
    restart are surfaced) and the most-recent detection from the DB.
    """
    orch = getattr(request.app.state, "orchestrator", None)
    enabled = False
    universe: list[str] = []
    interval_seconds: int | None = None

    if orch is not None:
        cfg = orch._cfg
        enabled = bool(cfg.scanner_enabled)
        interval_seconds = int(cfg.scanner_interval_seconds)
        if getattr(orch, "_scanner", None) is not None:
            universe = list(orch._scanner._universe)
        elif cfg.scanner_universe:
            universe = list(cfg.scanner_universe)
        else:
            # Mirror the default the orchestrator would have used.
            from x_alpaca_trading_bot.scanners.failed_breakout import DEFAULT_UNIVERSE
            universe = list(DEFAULT_UNIVERSE)

    conn = request.app.state.conn
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT MAX(detected_at) AS last_event,
                   COUNT(*) AS total_events,
                   COUNT(*) FILTER (WHERE detected_at::date = (NOW() AT TIME ZONE 'America/New_York')::date) AS events_today
            FROM scanner_events
            WHERE scanner_name = 'failed_breakout'
            """
        )
        row = cur.fetchone()
    last_event = row[0].isoformat() if row and row[0] else None
    total = int(row[1]) if row else 0
    today = int(row[2]) if row else 0

    return {
        "enabled": enabled,
        "phase": "A",  # log-only — bump to "B" when we wire trades
        "scanner_name": "failed_breakout",
        "interval_seconds": interval_seconds,
        "universe": universe,
        "universe_size": len(universe),
        "last_event_at": last_event,
        "events_total": total,
        "events_today": today,
        "as_of": datetime.now(timezone.utc).isoformat(),
    }


@router.get("", summary="Recent scanner events (most recent first)")
def list_events(
    request: Request,
    limit: int = Query(default=50, ge=1, le=500),
    since_days: int = Query(default=7, ge=1, le=90),
    ticker: str | None = Query(default=None, max_length=10),
) -> list[dict[str, Any]]:
    """Return detected events ordered most-recent-first.

    `since_days` is a rolling window so the dashboard can show today /
    last week / last month without cursor pagination — events are
    naturally sparse so a 30-day window stays small.
    """
    where = ["scanner_name = 'failed_breakout'",
             "detected_at > NOW() - INTERVAL '%s days'" % int(since_days)]
    params: list[Any] = []
    if ticker:
        where.append("ticker = %s")
        params.append(ticker.upper())
    params.append(limit)
    sql = f"""
        SELECT id, ticker, detected_at, event_day,
               breakout_ts, breakout_price,
               failure_ts, failure_price,
               prior_high, volume_ratio
        FROM scanner_events
        WHERE {' AND '.join(where)}
        ORDER BY detected_at DESC
        LIMIT %s
    """
    conn = request.app.state.conn
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    out: list[dict[str, Any]] = []
    for r in rows:
        prior = float(r[8]) if r[8] is not None else None
        failure = float(r[7]) if r[7] is not None else None
        failure_depth_pct = (
            (prior - failure) / prior if prior and prior > 0 and failure is not None else None
        )
        out.append({
            "id": int(r[0]),
            "ticker": r[1],
            "detected_at": r[2].isoformat() if r[2] else None,
            "event_day": r[3].isoformat() if r[3] else None,
            "breakout_ts": r[4].isoformat() if r[4] else None,
            "breakout_price": str(r[5]) if r[5] is not None else None,
            "failure_ts": r[6].isoformat() if r[6] else None,
            "failure_price": str(r[7]) if r[7] is not None else None,
            "prior_high": str(r[8]) if r[8] is not None else None,
            "volume_ratio": str(r[9]) if r[9] is not None else None,
            "failure_depth_pct": failure_depth_pct,
        })
    return out


@router.get("/daily", summary="Per-day event counts over a rolling window")
def daily_counts(
    request: Request,
    days: int = Query(default=14, ge=1, le=90),
) -> list[dict[str, Any]]:
    """Bucket events by event_day for the mini chart. Includes zero-rows
    for days inside the window with no detections — the dashboard renders
    those as empty bars instead of gaps."""
    conn = request.app.state.conn
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH days AS (
              SELECT generate_series(
                (NOW() AT TIME ZONE 'America/New_York')::date - (%s::int - 1),
                (NOW() AT TIME ZONE 'America/New_York')::date,
                '1 day'::interval
              )::date AS d
            )
            SELECT days.d AS day,
                   COUNT(e.id) AS events
            FROM days
            LEFT JOIN scanner_events e
              ON e.event_day = days.d AND e.scanner_name = 'failed_breakout'
            GROUP BY days.d
            ORDER BY days.d
            """,
            (days,),
        )
        rows = cur.fetchall()
    return [{"day": r[0].isoformat(), "events": int(r[1])} for r in rows]
