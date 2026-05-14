"""Unit tests for x_alpaca_trading_bot.config_store.BotConfigStore.

Requires DATABASE_URL pointed at a Postgres with the bot_config table
created via deploy/postgres_setup.sql.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
from dotenv import load_dotenv

from x_alpaca_trading_bot import db
from x_alpaca_trading_bot.config_store import BotConfig, BotConfigStore


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
    # Reset the single bot_config row to defaults before each test.
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


def test_reload_returns_defaults(conn: psycopg.Connection) -> None:
    store = BotConfigStore(conn)
    snap = store.reload()
    assert snap == BotConfig(
        max_position_spend_usd=Decimal("500.00"),
        max_qty_per_position=10,
        daily_loss_kill_pct=Decimal("0.0300"),
        disable_x_stream=False,
    )


def test_snapshot_lazily_loads(conn: psycopg.Connection) -> None:
    store = BotConfigStore(conn)
    # First call hits the DB; second returns the cached snapshot.
    s1 = store.snapshot()
    s2 = store.snapshot()
    assert s1 is s2


def test_update_persists_and_refreshes(conn: psycopg.Connection) -> None:
    store = BotConfigStore(conn)
    store.reload()
    new = store.update(
        max_position_spend_usd=Decimal("750.50"),
        max_qty_per_position=5,
        daily_loss_kill_pct=Decimal("0.05"),
        disable_x_stream=True,
    )
    assert new.max_position_spend_usd == Decimal("750.50")
    assert new.max_qty_per_position == 5
    assert new.daily_loss_kill_pct == Decimal("0.0500")
    assert new.disable_x_stream is True

    # A fresh store reading the same conn must see the same persisted state.
    other = BotConfigStore(conn)
    persisted = other.reload()
    assert persisted == new


def test_update_partial_leaves_other_fields_intact(conn: psycopg.Connection) -> None:
    store = BotConfigStore(conn)
    store.reload()
    new = store.update(disable_x_stream=True)
    assert new.disable_x_stream is True
    # Untouched fields keep their defaults.
    assert new.max_position_spend_usd == Decimal("500.00")
    assert new.max_qty_per_position == 10
    assert new.daily_loss_kill_pct == Decimal("0.0300")


def test_update_rejects_spend_below_minimum(conn: psycopg.Connection) -> None:
    store = BotConfigStore(conn)
    store.reload()
    with pytest.raises(ValueError, match="max_position_spend_usd"):
        store.update(max_position_spend_usd=Decimal("0.50"))


def test_update_rejects_spend_above_maximum(conn: psycopg.Connection) -> None:
    store = BotConfigStore(conn)
    store.reload()
    with pytest.raises(ValueError, match="max_position_spend_usd"):
        store.update(max_position_spend_usd=Decimal("999999"))


def test_update_rejects_qty_out_of_range(conn: psycopg.Connection) -> None:
    store = BotConfigStore(conn)
    store.reload()
    with pytest.raises(ValueError, match="max_qty_per_position"):
        store.update(max_qty_per_position=0)
    with pytest.raises(ValueError, match="max_qty_per_position"):
        store.update(max_qty_per_position=101)


def test_update_rejects_kill_pct_out_of_range(conn: psycopg.Connection) -> None:
    store = BotConfigStore(conn)
    store.reload()
    with pytest.raises(ValueError, match="daily_loss_kill_pct"):
        store.update(daily_loss_kill_pct=Decimal("0.0001"))
    with pytest.raises(ValueError, match="daily_loss_kill_pct"):
        store.update(daily_loss_kill_pct=Decimal("0.99"))


def test_update_with_no_fields_is_noop(conn: psycopg.Connection) -> None:
    store = BotConfigStore(conn)
    before = store.reload()
    after = store.update()
    assert before == after
