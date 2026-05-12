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


def run_migrations(conn: psycopg.Connection, deploy_dir: Path) -> None:
    """Apply the schema file. Idempotent — safe to run on every startup."""
    sql_path = deploy_dir / "postgres_setup.sql"
    if not sql_path.exists():
        raise FileNotFoundError(f"Schema file not found: {sql_path}")

    sql = sql_path.read_text()
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(sql)
    logger.info("Applied schema from %s", sql_path)
