"""Thread-safe, DB-backed runtime config store.

The orchestrator and the API endpoints share one process, so the store
is just an in-memory snapshot guarded by an RLock and persisted on every
write. Reads are O(1) and never block on the DB; writes hit Postgres and
refresh the in-memory copy atomically.

The schema lives in deploy/postgres_setup.sql (table `bot_config`). The
table is single-row (id=1 enforced by CHECK). Schema migrations run at
startup, so the row always exists by the time anyone calls .reload().
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from decimal import Decimal

import psycopg

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BotConfig:
    """Immutable snapshot of the runtime-tunable settings."""

    max_position_spend_usd: Decimal
    max_qty_per_position: int
    daily_loss_kill_pct: Decimal
    disable_x_stream: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "max_position_spend_usd": str(self.max_position_spend_usd),
            "max_qty_per_position": self.max_qty_per_position,
            "daily_loss_kill_pct": str(self.daily_loss_kill_pct),
            "disable_x_stream": self.disable_x_stream,
        }


# Bounds enforced server-side. Mirrored in the UI for client-side hints,
# but the server is authoritative.
BOUNDS = {
    "max_position_spend_usd": (Decimal("1.00"), Decimal("100000.00")),
    "max_qty_per_position": (1, 100),
    "daily_loss_kill_pct": (Decimal("0.001"), Decimal("0.50")),
}


class BotConfigStore:
    """RLock-guarded snapshot of bot_config, refreshed from Postgres on write.

    Usage:
        store = BotConfigStore(conn)
        store.reload()             # call once at startup after migrations
        snap = store.snapshot()    # fast: in-memory read
        store.update(max_position_spend_usd=Decimal("750"))  # writes DB + refreshes
    """

    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn
        self._lock = threading.RLock()
        self._snapshot: BotConfig | None = None

    def reload(self) -> BotConfig:
        """Read the row from Postgres into the in-memory snapshot."""
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT max_position_spend_usd, max_qty_per_position, "
                    "       daily_loss_kill_pct, disable_x_stream "
                    "FROM bot_config WHERE id = 1",
                )
                row = cur.fetchone()
            if row is None:
                raise RuntimeError(
                    "bot_config row missing; ensure deploy/postgres_setup.sql "
                    "has been applied (it INSERTs the default row).",
                )
            self._snapshot = BotConfig(
                max_position_spend_usd=Decimal(row[0]),
                max_qty_per_position=int(row[1]),
                daily_loss_kill_pct=Decimal(row[2]),
                disable_x_stream=bool(row[3]),
            )
            return self._snapshot

    def snapshot(self) -> BotConfig:
        """Return the current in-memory snapshot. Reloads on first call."""
        with self._lock:
            if self._snapshot is None:
                return self.reload()
            return self._snapshot

    def update(
        self,
        *,
        max_position_spend_usd: Decimal | None = None,
        max_qty_per_position: int | None = None,
        daily_loss_kill_pct: Decimal | None = None,
        disable_x_stream: bool | None = None,
    ) -> BotConfig:
        """Validate, write to Postgres, refresh the in-memory snapshot.

        Only fields whose value is not None are touched. Returns the new
        snapshot for caller convenience.

        Raises ValueError on out-of-bounds inputs so the API layer can
        translate into a 422 response.
        """
        updates: dict[str, object] = {}
        if max_position_spend_usd is not None:
            _check_bounds("max_position_spend_usd", max_position_spend_usd)
            updates["max_position_spend_usd"] = max_position_spend_usd
        if max_qty_per_position is not None:
            _check_bounds("max_qty_per_position", max_qty_per_position)
            updates["max_qty_per_position"] = max_qty_per_position
        if daily_loss_kill_pct is not None:
            _check_bounds("daily_loss_kill_pct", daily_loss_kill_pct)
            updates["daily_loss_kill_pct"] = daily_loss_kill_pct
        if disable_x_stream is not None:
            updates["disable_x_stream"] = disable_x_stream

        if not updates:
            return self.snapshot()

        # Build a parameterized SET clause. Column names are restricted to a
        # known whitelist (the if-blocks above), so direct interpolation here
        # is safe.
        set_clause = ", ".join(f"{col} = %s" for col in updates) + ", updated_at = NOW()"
        params = list(updates.values()) + [1]

        with self._lock:
            with self._conn.transaction(), self._conn.cursor() as cur:
                cur.execute(
                    f"UPDATE bot_config SET {set_clause} WHERE id = %s",
                    params,
                )
            logger.info("bot_config updated: %s", updates)
            return self.reload()


def _check_bounds(field: str, value: Decimal | int) -> None:
    lo, hi = BOUNDS[field]
    if value < lo or value > hi:
        raise ValueError(
            f"{field} out of bounds: got {value}, allowed [{lo}, {hi}]",
        )
