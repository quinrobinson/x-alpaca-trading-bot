"""Postgres connection and migration runner.

Phase 1 uses a single `deploy/postgres_setup.sql` with `IF NOT EXISTS`
everywhere so re-running is a no-op. A versioned migrations table can be
introduced later when schema starts evolving incrementally.
"""

from __future__ import annotations

import logging
from pathlib import Path

import psycopg

logger = logging.getLogger(__name__)


def connect(database_url: str) -> psycopg.Connection:
    """Open a synchronous psycopg connection. Caller closes via context manager."""
    return psycopg.connect(database_url)


def ensure_connection(
    conn: psycopg.Connection | None,
    database_url: str,
) -> psycopg.Connection:
    """Return a live connection — reconnect if the current one is dead.

    Background: Supabase prunes idle connections and pauses free-tier
    projects on inactivity. When that happens the next cursor() call on
    the cached connection raises OperationalError("the connection is
    closed"). The orchestrator was crashing on every tick until a manual
    restart cleared the state.

    This helper does a cheap `SELECT 1` round-trip. If the connection is
    alive, returns it unchanged. If it's dead (OperationalError or
    InterfaceError), closes it best-effort and opens a fresh one.

    Callers reassign the returned connection back to their stored
    reference: `self._conn = db.ensure_connection(self._conn, url)`.
    Subsequent DB operations through that reference will then use the
    live connection.

    Failure mode: if reconnect itself raises, the exception propagates
    to the caller. The orchestrator already wraps each tick stage in
    try/except so a failed reconnect just skips that stage instead of
    crashing the tick loop.
    """
    if conn is not None:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
            return conn
        except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
            logger.warning(
                "DB connection check failed (%s); reconnecting",
                exc.__class__.__name__,
            )
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
    return psycopg.connect(database_url)


def run_migrations(conn: psycopg.Connection, deploy_dir: Path) -> None:
    """Apply the schema file. Idempotent — safe to run on every startup."""
    sql_path = deploy_dir / "postgres_setup.sql"
    if not sql_path.exists():
        raise FileNotFoundError(f"Schema file not found: {sql_path}")

    sql = sql_path.read_text()
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(sql)
    logger.info("Applied schema from %s", sql_path)
