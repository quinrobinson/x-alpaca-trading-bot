"""Integration tests for the /config router."""

from __future__ import annotations

import os
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient

from api.main import create_app
from x_alpaca_trading_bot import db
from x_alpaca_trading_bot.config_store import BotConfigStore


def _db_url() -> str | None:
    load_dotenv(override=True)
    return os.environ.get("DATABASE_URL") or None


@pytest.fixture
def conn() -> Iterator[psycopg.Connection]:
    url = _db_url()
    if not url:
        pytest.skip("DATABASE_URL not set")
    c = db.connect(url)
    db.run_migrations(c, Path(__file__).resolve().parent.parent / "deploy")
    with c.cursor() as cur:
        cur.execute(
            "UPDATE bot_config SET "
            "  max_position_spend_usd = 500.00, "
            "  max_qty_per_position   = 10, "
            "  daily_loss_kill_pct    = 0.03, "
            "  disable_x_stream       = FALSE "
            "WHERE id = 1",
        )
    c.commit()
    yield c
    c.close()


def _client(conn: psycopg.Connection) -> TestClient:
    store = BotConfigStore(conn)
    store.reload()
    app = create_app(conn=conn, config_store=store)
    return TestClient(app)


def test_get_config_returns_current_values(conn: psycopg.Connection) -> None:
    with _client(conn) as client:
        r = client.get("/config")
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "max_position_spend_usd": "500.00",
        "max_qty_per_position": 10,
        "daily_loss_kill_pct": "0.0300",
        "disable_x_stream": False,
    }


def test_patch_config_updates_single_field(conn: psycopg.Connection) -> None:
    with _client(conn) as client:
        r = client.patch("/config", json={"max_position_spend_usd": "750"})
    assert r.status_code == 200
    assert r.json()["max_position_spend_usd"] == "750.00"
    # Untouched fields stay at default.
    assert r.json()["max_qty_per_position"] == 10


def test_patch_config_updates_multiple_fields(conn: psycopg.Connection) -> None:
    with _client(conn) as client:
        r = client.patch(
            "/config",
            json={
                "max_position_spend_usd": "1000",
                "max_qty_per_position": 5,
                "daily_loss_kill_pct": "0.05",
                "disable_x_stream": True,
            },
        )
    assert r.status_code == 200
    body = r.json()
    assert body["max_position_spend_usd"] == "1000.00"
    assert body["max_qty_per_position"] == 5
    assert body["daily_loss_kill_pct"] == "0.0500"
    assert body["disable_x_stream"] is True


def test_patch_config_persists_across_clients(conn: psycopg.Connection) -> None:
    """Two clients (separate stores) reading the same DB see each other's writes."""
    with _client(conn) as client:
        client.patch("/config", json={"disable_x_stream": True})
    # Build a new app pointing at the same DB; the fresh store reads persisted state.
    with _client(conn) as client2:
        r = client2.get("/config")
    assert r.json()["disable_x_stream"] is True


def test_patch_config_rejects_out_of_bounds(conn: psycopg.Connection) -> None:
    with _client(conn) as client:
        r = client.patch("/config", json={"max_position_spend_usd": "999999"})
    assert r.status_code == 422
    assert "max_position_spend_usd" in r.json()["detail"]


def test_patch_config_rejects_unknown_field(conn: psycopg.Connection) -> None:
    with _client(conn) as client:
        r = client.patch("/config", json={"nope": 1})
    assert r.status_code == 422


def test_patch_config_accepts_numeric_decimal(conn: psycopg.Connection) -> None:
    """Dashboard sends floats from a number input; the API should coerce."""
    with _client(conn) as client:
        r = client.patch(
            "/config",
            json={"max_position_spend_usd": 250.5, "daily_loss_kill_pct": 0.025},
        )
    assert r.status_code == 200
    assert r.json()["max_position_spend_usd"] == "250.50"
    assert r.json()["daily_loss_kill_pct"] == "0.0250"


def test_get_config_returns_503_when_store_not_wired() -> None:
    """If create_app is called without config_store= the endpoint surfaces it."""
    app = create_app(conn=None, config_store=None)
    with TestClient(app) as client:
        r = client.get("/config")
    assert r.status_code == 503
