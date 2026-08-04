"""Scanner endpoints — observability for the scanner program.

SCANNER_PROGRAM.md is the program authority. The scanner lab (Phase S1)
runs several hypotheses side by side, each logging to scanner_events
under its own scanner_name; the S2 equity book records its trades in
scanner_trades. These endpoints expose both so the operator can compare
hypotheses and watch the trading book without SQL.

Endpoints:
    GET /scanner/status — config + universe + per-hypothesis activity + arm state
    GET /scanner/events — paginated events with optional scanner/ticker filters
    GET /scanner/daily  — per-day event counts over the last N days
    GET /scanner/trades — S2 equity book: open + recent closed + summary stats
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Query, Request

from api.db_dep import resolve_conn

router = APIRouter(prefix="/scanner", tags=["scanner"])

# Whitelist for the ?scanner= filter — interpolating user input into SQL
# is never OK, and hypothesis names are a small closed set anyway.
_KNOWN_SCANNERS = ("failed_breakout", "vwap_reject", "gap_fade", "prior_low_break")


@router.get("/status", summary="Scanner enablement + universe + per-hypothesis activity")
def scanner_status(request: Request) -> dict[str, Any]:
    """Everything the dashboard's top card needs.

    Reads scanner + S2 trading config from the orchestrator (surfacing
    env changes on restart) and per-hypothesis detection counts from the
    DB. All orchestrator reads are getattr-guarded so the endpoint stays
    up with a partially-constructed or fake orchestrator.
    """
    orch = getattr(request.app.state, "orchestrator", None)
    enabled = False
    universe: list[str] = []
    interval_seconds: int | None = None
    hypotheses: list[str] = []
    trading: dict[str, Any] = {"enabled": False, "open_positions": 0}

    cfg = getattr(orch, "_cfg", None)
    if cfg is not None:
        enabled = bool(getattr(cfg, "scanner_enabled", False))
        interval_seconds = int(getattr(cfg, "scanner_interval_seconds", 300))
        trading = {
            "enabled": bool(getattr(cfg, "scanner_trading_enabled", False)),
            "notional": str(getattr(cfg, "scanner_trade_notional", "")) or None,
            "max_concurrent": getattr(cfg, "scanner_max_concurrent", None),
            "min_volume_ratio": (
                str(getattr(cfg, "scanner_min_volume_ratio", "")) or None
            ),
            "open_positions": len(getattr(orch, "_equity_positions", {}) or {}),
        }

    scanner = getattr(orch, "_scanner", None)
    if scanner is not None:
        universe = list(getattr(scanner, "_universe", ()) or ())
        hypotheses = sorted(getattr(scanner, "_hypotheses", {}) or {})
    if not universe and cfg is not None and getattr(cfg, "scanner_universe", None):
        universe = list(cfg.scanner_universe)
    if not universe and cfg is not None:
        # Mirror the default the orchestrator would have used.
        from x_alpaca_trading_bot.scanners.failed_breakout import DEFAULT_UNIVERSE
        universe = list(DEFAULT_UNIVERSE)
    if not hypotheses:
        hypotheses = list(_KNOWN_SCANNERS)

    with resolve_conn(request) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT scanner_name,
                   COUNT(*) AS total,
                   COUNT(*) FILTER (
                     WHERE detected_at::date = (NOW() AT TIME ZONE 'America/New_York')::date
                   ) AS today,
                   MAX(detected_at) AS last_event
            FROM scanner_events
            GROUP BY scanner_name
            ORDER BY scanner_name
            """
        )
        rows = cur.fetchall()

    by_scanner = [
        {
            "scanner_name": r[0],
            "events_total": int(r[1]),
            "events_today": int(r[2]),
            "last_event_at": r[3].isoformat() if r[3] else None,
        }
        for r in rows
    ]
    last_events = [r[3] for r in rows if r[3] is not None]

    return {
        "enabled": enabled,
        "phase": "S2" if trading.get("enabled") else "S1",
        "hypotheses": hypotheses,
        "by_scanner": by_scanner,
        "trading": trading,
        "interval_seconds": interval_seconds,
        "universe": universe,
        "universe_size": len(universe),
        "last_event_at": max(last_events).isoformat() if last_events else None,
        "events_total": sum(b["events_total"] for b in by_scanner),
        "events_today": sum(b["events_today"] for b in by_scanner),
        "as_of": datetime.now(timezone.utc).isoformat(),
    }


@router.get("", summary="Recent scanner events (most recent first)")
def list_events(
    request: Request,
    limit: int = Query(default=50, ge=1, le=500),
    since_days: int = Query(default=7, ge=1, le=90),
    ticker: str | None = Query(default=None, max_length=10),
    scanner: str | None = Query(default=None, max_length=32),
) -> list[dict[str, Any]]:
    """Events across ALL hypotheses by default; `scanner` narrows to one.

    `since_days` is a rolling window so the dashboard can show today /
    last week / last month without cursor pagination.
    """
    where = ["detected_at > NOW() - INTERVAL '%s days'" % int(since_days)]
    params: list[Any] = []
    if scanner and scanner in _KNOWN_SCANNERS:
        where.append("scanner_name = %s")
        params.append(scanner)
    if ticker:
        where.append("ticker = %s")
        params.append(ticker.upper())
    params.append(limit)
    sql = f"""
        SELECT id, scanner_name, ticker, detected_at, event_day,
               breakout_ts, breakout_price,
               failure_ts, failure_price,
               prior_high, volume_ratio
        FROM scanner_events
        WHERE {' AND '.join(where)}
        ORDER BY detected_at DESC
        LIMIT %s
    """
    with resolve_conn(request) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    out: list[dict[str, Any]] = []
    for r in rows:
        prior = float(r[9]) if r[9] is not None else None
        failure = float(r[8]) if r[8] is not None else None
        failure_depth_pct = (
            (prior - failure) / prior if prior and prior > 0 and failure is not None else None
        )
        out.append({
            "id": int(r[0]),
            "scanner_name": r[1],
            "ticker": r[2],
            "detected_at": r[3].isoformat() if r[3] else None,
            "event_day": r[4].isoformat() if r[4] else None,
            "breakout_ts": r[5].isoformat() if r[5] else None,
            "breakout_price": str(r[6]) if r[6] is not None else None,
            "failure_ts": r[7].isoformat() if r[7] else None,
            "failure_price": str(r[8]) if r[8] is not None else None,
            "prior_high": str(r[9]) if r[9] is not None else None,
            "volume_ratio": str(r[10]) if r[10] is not None else None,
            "failure_depth_pct": failure_depth_pct,
        })
    return out


@router.get("/daily", summary="Per-day event counts over a rolling window")
def daily_counts(
    request: Request,
    days: int = Query(default=14, ge=1, le=90),
    scanner: str | None = Query(default=None, max_length=32),
) -> list[dict[str, Any]]:
    """Bucket events by event_day for the mini chart, all hypotheses
    pooled unless `scanner` narrows it. Zero-rows included so the chart
    renders empty bars instead of gaps."""
    scanner_filter = ""
    params: list[Any] = [days]
    if scanner and scanner in _KNOWN_SCANNERS:
        scanner_filter = "AND e.scanner_name = %s"
        params.append(scanner)
    with resolve_conn(request) as conn, conn.cursor() as cur:
        cur.execute(
            f"""
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
              ON e.event_day = days.d {scanner_filter}
            GROUP BY days.d
            ORDER BY days.d
            """,
            params,
        )
        rows = cur.fetchall()
    return [{"day": r[0].isoformat(), "events": int(r[1])} for r in rows]


@router.get("/trades", summary="S2 equity book: open + recent closed + stats")
def scanner_trades(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    """The scanner trading book. Open positions first (closed_at IS
    NULL), then the most recent closed trades, plus roll-up stats the
    dashboard shows next to the arm badge."""
    with resolve_conn(request) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, scanner_name, ticker, event_day, side, qty,
                   entry_price, opened_at, closed_at, exit_price,
                   gross_pnl, pnl_pct, exit_reason
            FROM scanner_trades
            ORDER BY (closed_at IS NULL) DESC, opened_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()
        cur.execute(
            """
            SELECT COUNT(*) FILTER (WHERE closed_at IS NOT NULL) AS n_closed,
                   COUNT(*) FILTER (WHERE gross_pnl > 0) AS winners,
                   COALESCE(SUM(gross_pnl), 0) AS total_pnl
            FROM scanner_trades
            """
        )
        n_closed, winners, total_pnl = cur.fetchone()

    trades = [
        {
            "id": int(r[0]),
            "scanner_name": r[1],
            "ticker": r[2],
            "event_day": r[3].isoformat() if r[3] else None,
            "side": r[4],
            "qty": int(r[5]),
            "entry_price": str(r[6]) if r[6] is not None else None,
            "opened_at": r[7].isoformat() if r[7] else None,
            "closed_at": r[8].isoformat() if r[8] else None,
            "exit_price": str(r[9]) if r[9] is not None else None,
            "gross_pnl": str(r[10]) if r[10] is not None else None,
            "pnl_pct": str(r[11]) if r[11] is not None else None,
            "exit_reason": r[12],
            "is_open": r[8] is None,
        }
        for r in rows
    ]
    return {
        "trades": trades,
        "stats": {
            "n_closed": int(n_closed or 0),
            "winners": int(winners or 0),
            "total_pnl": str(total_pnl if total_pnl is not None else "0"),
        },
    }
