"""risk_manager — Phase 5.

Kill switches and position limits per X_ALPACA_OPTIONS_HANDOFF.md §1.6:

    1. daily_loss          — realized + unrealized P&L <= -daily_loss_kill_pct
                             of starting equity. Resets at next session.
    2. consecutive_losses  — N consecutive losing trades in a row. Requires
                             manual operator clear.
    3. x_stream_disconnected — X stream stalled >stall_seconds during market
                             hours. Auto-clears on reconnect (orchestrator).
    4. alpaca_disconnected   — Alpaca connection stalled >stall_seconds
                             during market hours. Auto-clears on reconnect.

Architecture:

    evaluate()  — pure function. Takes a `SessionState` snapshot + tunables
                  and returns a `RiskDecision` listing which switches should
                  be tripped right now. No I/O. No `datetime.now()`.

    realized_pnl_today()
    consecutive_loss_count()
                — thin SQL helpers against the `trades` table. The
                  orchestrator (Phase 7) uses these to assemble a SessionState
                  before each risk check.

    evaluate_and_log()
                — convenience that wraps evaluate() with a journal.insert_event
                  write so the spec's "every risk decision written to events
                  table" gate is satisfied with one call.

No `datetime.now()` here (spec §2.3 rule 3). All money in `Decimal`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import psycopg

from x_alpaca_trading_bot import journal

logger = logging.getLogger(__name__)

# All kill-switch names this module is aware of.
KILL_SWITCH_NAMES: tuple[str, ...] = (
    "daily_loss",
    "consecutive_losses",
    "x_stream_disconnected",
    "alpaca_disconnected",
)

DEFAULT_CONNECTION_STALL_SECONDS = 60


@dataclass(frozen=True)
class SessionState:
    """All inputs the kill-switch evaluator needs.

    The orchestrator assembles this each tick from:
      - configured starting equity for the session
      - current equity (realized P&L today + unrealized via data_service)
      - current consecutive loss streak (from trades table)
      - last heartbeat timestamps for X stream and Alpaca
      - the set of switches the orchestrator currently considers tripped
        (so a tripped 'consecutive_losses' persists across calls until the
        operator clears it)
    """

    starting_equity: Decimal
    current_equity: Decimal
    consecutive_losses: int
    last_x_received_at: datetime | None      # tz-aware
    last_alpaca_ok_at: datetime | None       # tz-aware
    market_open: bool
    active_switches: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class RiskDecision:
    accepted: bool
    tripped_switches: tuple[str, ...]        # all switches that are/should be on now
    newly_tripped: tuple[str, ...]           # subset that flipped during this call
    reason: str | None                       # the first failing switch, for journaling


def evaluate(
    state: SessionState,
    now: datetime,
    *,
    daily_loss_kill_pct: Decimal,
    max_consecutive_losses: int,
    connection_stall_seconds: int = DEFAULT_CONNECTION_STALL_SECONDS,
) -> RiskDecision:
    """Pure check: which kill switches should be tripped given current state?

    Returns a RiskDecision describing the union of `state.active_switches`
    and any *newly* tripped switches. `accepted` is True iff none are on.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if state.starting_equity <= 0:
        raise ValueError(f"starting_equity must be positive; got {state.starting_equity}")

    new: set[str] = set()

    # 1. Daily loss kill switch — equity dropped >= threshold below start.
    loss_pct = (state.starting_equity - state.current_equity) / state.starting_equity
    if loss_pct >= daily_loss_kill_pct:
        new.add("daily_loss")

    # 2. Consecutive losses — strict threshold (>= max_consecutive_losses).
    if state.consecutive_losses >= max_consecutive_losses:
        new.add("consecutive_losses")

    # 3 & 4. Connection switches only fire during market hours; off-hours,
    # we expect the streams to be quiet.
    if state.market_open:
        stall = timedelta(seconds=connection_stall_seconds)
        if state.last_x_received_at is None or (now - state.last_x_received_at) > stall:
            new.add("x_stream_disconnected")
        if state.last_alpaca_ok_at is None or (now - state.last_alpaca_ok_at) > stall:
            new.add("alpaca_disconnected")

    all_tripped = frozenset(state.active_switches) | frozenset(new)
    newly_tripped = frozenset(new) - frozenset(state.active_switches)
    reason = _first_reason(all_tripped)
    return RiskDecision(
        accepted=not all_tripped,
        tripped_switches=tuple(sorted(all_tripped)),
        newly_tripped=tuple(sorted(newly_tripped)),
        reason=reason,
    )


def evaluate_and_log(
    conn: psycopg.Connection,
    state: SessionState,
    now: datetime,
    *,
    daily_loss_kill_pct: Decimal,
    max_consecutive_losses: int,
    connection_stall_seconds: int = DEFAULT_CONNECTION_STALL_SECONDS,
    context: dict[str, Any] | None = None,
) -> RiskDecision:
    """evaluate() + persist a row to events. One call satisfies Phase 5 gate 3."""
    decision = evaluate(
        state,
        now,
        daily_loss_kill_pct=daily_loss_kill_pct,
        max_consecutive_losses=max_consecutive_losses,
        connection_stall_seconds=connection_stall_seconds,
    )
    severity = "warning" if not decision.accepted else "info"
    if decision.newly_tripped:
        severity = "critical"

    payload: dict[str, Any] = {
        "accepted": decision.accepted,
        "tripped_switches": list(decision.tripped_switches),
        "newly_tripped": list(decision.newly_tripped),
        "starting_equity": str(state.starting_equity),
        "current_equity": str(state.current_equity),
        "consecutive_losses": state.consecutive_losses,
        "market_open": state.market_open,
    }
    if context:
        payload["context"] = context

    journal.insert_event(
        conn,
        ts=now,
        severity=severity,
        category="risk",
        message=decision.reason or "risk_check_passed",
        context=payload,
    )
    return decision


# ---- SQL helpers (use trades table, take time explicitly) -----------------

def realized_pnl_today(conn: psycopg.Connection, session_date: date) -> Decimal:
    """Sum gross_pnl over trades closed on `session_date`."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(SUM(gross_pnl), 0) "
            "FROM trades "
            "WHERE closed_at::date = %s",
            (session_date,),
        )
        row = cur.fetchone()
    if row is None or row[0] is None:
        return Decimal(0)
    return Decimal(row[0])


def consecutive_loss_count(
    conn: psycopg.Connection,
    *,
    before: datetime,
    lookback_days: int = 30,
) -> int:
    """Count trades from `before` backward where gross_pnl <= 0, stopping at first winner.

    `before` is a wall-clock cutoff so callers can replay history. Walks the
    most-recent-first list and counts losses (gross_pnl <= 0). Breaks on the
    first winner (gross_pnl > 0).
    """
    earliest = before - timedelta(days=lookback_days)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gross_pnl "
            "FROM trades "
            "WHERE closed_at < %s AND closed_at >= %s "
            "ORDER BY closed_at DESC",
            (before, earliest),
        )
        rows = cur.fetchall()
    count = 0
    for (pnl,) in rows:
        if pnl is None:
            break
        if pnl <= 0:
            count += 1
        else:
            break
    return count


# ---- Internal -------------------------------------------------------------

def _first_reason(tripped: frozenset[str]) -> str | None:
    """Pick a deterministic, useful first reason from a set of trips."""
    if not tripped:
        return None
    # Priority order — capital protection first, then connections.
    for name in KILL_SWITCH_NAMES:
        if name in tripped:
            return name
    return next(iter(sorted(tripped)))
