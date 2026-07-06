"""Live-connection helper for the read-only API routers.

Why this exists
---------------
The orchestrator and the API used to share a single psycopg connection.
The orchestrator calls ``db.ensure_connection`` at the top of every tick;
when Supabase prunes an idle connection, that helper closes the dead
socket and returns a *new* connection object, which the orchestrator
rebinds to ``self._conn``. But the API's ``app.state.conn`` still pointed
at the now-closed object, so every read endpoint (timeline, scanner,
signals, ...) started raising "connection is closed" and the dashboard
views froze while the bot itself kept trading.

The fix has two parts:

  1. Production gives the API its *own* connection (see
     ``build_production_app``), so the orchestrator's reconnect never
     orphans the API's handle.
  2. This helper re-validates that connection before each request and
     writes any reconnect back to ``app.state.conn`` — so the API is now
     self-healing the same way the orchestrator is.

A lock serializes access because a single psycopg connection is not safe
for concurrent use across the threadpool that runs sync endpoints, and
the reconnect-and-rebind must be atomic.

Test seam: when ``app.state.conn_lock`` is absent (tests / inert mode),
we yield the injected connection unchanged — no url, no reconnect.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from fastapi import Request

from x_alpaca_trading_bot import db


@contextmanager
def resolve_conn(request: Request) -> Iterator:
    """Yield a live DB connection for a read endpoint.

    In production, validates + reconnects ``app.state.conn`` under a lock
    and writes the result back. In tests (no lock configured), yields the
    injected connection as-is.
    """
    lock = getattr(request.app.state, "conn_lock", None)
    url = getattr(request.app.state, "database_url", None)

    if lock is None or url is None:
        # Test / inert mode: use the injected connection unchanged.
        yield request.app.state.conn
        return

    with lock:
        conn = db.ensure_connection(request.app.state.conn, url)
        # Read endpoints must never leave a transaction open — an idle
        # in-transaction connection is exactly what Supabase prunes. A
        # fresh connection defaults to autocommit off, so set it every
        # time (idempotent) rather than only on first connect.
        if not conn.autocommit:
            conn.autocommit = True
        request.app.state.conn = conn
        yield conn
