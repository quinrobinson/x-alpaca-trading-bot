"""Tests for db.ensure_connection — the auto-reconnect band-aid.

The reason this exists: Supabase prunes idle connections and pauses
free-tier projects on inactivity. When that happens the orchestrator's
long-lived psycopg connection goes "closed" and every cursor() call
raises OperationalError forever. ensure_connection does a cheap SELECT 1
before each tick so the tick can fall back to a fresh connection instead
of crash-looping until someone manually restarts the service.

These tests use fakes so they don't need a real DB — the real DB code
path is covered by the integration tests under
test_risk_manager_integration.py etc.
"""

from __future__ import annotations

from unittest.mock import patch

import psycopg
import pytest

from x_alpaca_trading_bot import db


class _FakeCursor:
    """Stands in for psycopg's cursor context manager."""

    def __init__(self, conn: "_FakeLiveConnection") -> None:
        self._conn = conn

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *a) -> None:
        pass

    def execute(self, sql: str) -> None:
        self._conn.executed.append(sql)


class _FakeLiveConnection:
    """A connection that succeeds — SELECT 1 returns cleanly."""

    def __init__(self) -> None:
        self.closed_calls = 0
        self.executed: list[str] = []

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def close(self) -> None:
        self.closed_calls += 1


class _FakeDeadConnection:
    """A connection whose cursor() raises OperationalError, simulating
    Supabase pruning the underlying socket."""

    def __init__(self) -> None:
        self.closed_calls = 0

    def cursor(self) -> None:
        raise psycopg.OperationalError("the connection is closed")

    def close(self) -> None:
        self.closed_calls += 1


# ---- Happy path -----------------------------------------------------------

def test_ensure_connection_returns_live_connection_unchanged() -> None:
    """A connection that ping-succeeds is returned as-is — no reconnect."""
    live = _FakeLiveConnection()
    fresh = _FakeLiveConnection()

    with patch("x_alpaca_trading_bot.db.psycopg.connect", return_value=fresh):
        result = db.ensure_connection(live, "postgresql://test")

    assert result is live
    assert live.closed_calls == 0
    assert live.executed == ["SELECT 1"]


# ---- Reconnect on dead connection ----------------------------------------

def test_ensure_connection_reopens_when_cursor_raises_operationalerror() -> None:
    """Dead connection → closed best-effort + fresh connection returned."""
    dead = _FakeDeadConnection()
    fresh = _FakeLiveConnection()

    with patch("x_alpaca_trading_bot.db.psycopg.connect", return_value=fresh):
        result = db.ensure_connection(dead, "postgresql://test")

    assert result is fresh
    assert dead.closed_calls == 1


def test_ensure_connection_reopens_on_interface_error() -> None:
    """InterfaceError is the other psycopg flavor we want to catch."""
    class _InterfaceDead:
        def __init__(self) -> None:
            self.closed_calls = 0

        def cursor(self) -> None:
            raise psycopg.InterfaceError("connection already closed")

        def close(self) -> None:
            self.closed_calls += 1

    dead = _InterfaceDead()
    fresh = _FakeLiveConnection()

    with patch("x_alpaca_trading_bot.db.psycopg.connect", return_value=fresh):
        result = db.ensure_connection(dead, "postgresql://test")

    assert result is fresh
    assert dead.closed_calls == 1


def test_ensure_connection_tolerates_close_raising() -> None:
    """If close() raises on the dead handle, we still return a fresh
    connection rather than re-raising. The original conn is dead anyway."""
    class _CloseRaises:
        def cursor(self) -> None:
            raise psycopg.OperationalError("closed")

        def close(self) -> None:
            raise RuntimeError("close already failed")

    fresh = _FakeLiveConnection()

    with patch("x_alpaca_trading_bot.db.psycopg.connect", return_value=fresh):
        result = db.ensure_connection(_CloseRaises(), "postgresql://test")

    assert result is fresh


# ---- Bootstrap path -------------------------------------------------------

def test_ensure_connection_opens_fresh_when_conn_is_none() -> None:
    """Startup-style call where no prior connection exists — just open one."""
    fresh = _FakeLiveConnection()

    with patch("x_alpaca_trading_bot.db.psycopg.connect", return_value=fresh):
        result = db.ensure_connection(None, "postgresql://test")

    assert result is fresh


# ---- Failure surface ------------------------------------------------------

def test_ensure_connection_raises_when_reconnect_itself_fails() -> None:
    """If the reconnect attempt raises, the exception propagates. The
    orchestrator wraps the call in try/except so the tick survives, but
    this function does not swallow connect() failures itself — that
    would mask Supabase outages from the caller."""
    dead = _FakeDeadConnection()

    def _explode(_url: str) -> None:
        raise psycopg.OperationalError("supabase project is paused")

    with patch("x_alpaca_trading_bot.db.psycopg.connect", side_effect=_explode):
        with pytest.raises(psycopg.OperationalError):
            db.ensure_connection(dead, "postgresql://test")

    assert dead.closed_calls == 1
