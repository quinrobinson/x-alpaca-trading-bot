"""Integration tests for journal.insert_raw_post — Phase 2 gate 2.d.

Requires a live local Postgres at DATABASE_URL with migrations applied. If
DATABASE_URL is missing or unreachable, every test in this module is skipped
so unit-test runs in environments without a DB still pass.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from datetime import date, datetime, timezone
from decimal import Decimal

import psycopg
import pytest

from x_alpaca_trading_bot import db, journal
from x_alpaca_trading_bot.parser import (
    PARSE_PROMPT_VERSION,
    ParseResult,
    Signal,
    parse_result_to_journal_dict,
)

# Phase 2 gate 2.d threshold
WRITE_LATENCY_BUDGET_SECONDS = 1.0


def _database_url() -> str | None:
    # Honor a local .env so devs don't have to export DATABASE_URL into the shell.
    from dotenv import load_dotenv
    load_dotenv(override=True)
    return os.environ.get("DATABASE_URL") or None


@pytest.fixture
def conn() -> Iterator[psycopg.Connection]:
    url = _database_url()
    if not url:
        pytest.skip("DATABASE_URL not set; skipping journal integration tests")
    try:
        c = db.connect(url)
    except psycopg.OperationalError as exc:
        pytest.skip(f"Cannot connect to {url}: {exc}")

    # Ensure migrations are applied. Idempotent.
    from pathlib import Path
    deploy_dir = Path(__file__).resolve().parent.parent / "deploy"
    db.run_migrations(c, deploy_dir)

    # Clean x_posts before each test for deterministic state.
    with c.cursor() as cur:
        cur.execute("DELETE FROM x_posts")
    c.commit()

    try:
        yield c
    finally:
        c.close()


def test_insert_round_trip_under_one_second(conn: psycopg.Connection) -> None:
    """Phase 2 gate 2.d: from call to commit must be ≤ 1 second."""
    posted_at = datetime(2026, 5, 12, 14, 30, tzinfo=timezone.utc)
    received_at = datetime(2026, 5, 12, 14, 30, 1, tzinfo=timezone.utc)

    start = time.perf_counter()
    new_id = journal.insert_raw_post(
        conn,
        post_id="gate-2d-1",
        post_text="$AAPL 6/20 $185c @ 2.50",
        posted_at=posted_at,
        received_at=received_at,
        parse_result=None,
        actionable=False,
    )
    elapsed = time.perf_counter() - start

    assert new_id > 0
    assert elapsed < WRITE_LATENCY_BUDGET_SECONDS, (
        f"Insert took {elapsed * 1000:.1f}ms — exceeds {WRITE_LATENCY_BUDGET_SECONDS}s gate"
    )


def test_insert_actionable_with_signal_payload(conn: psycopg.Connection) -> None:
    """Full signal payload should round-trip through JSONB correctly."""
    signal = Signal(
        ticker="AAPL",
        option_type="call",
        strike=Decimal("185.00"),
        expiration=date(2026, 6, 20),
        posted_price=Decimal("2.50"),
        posted_at=datetime(2026, 5, 12, 14, 30, tzinfo=timezone.utc),
    )
    result = ParseResult(
        signal=signal,
        parse_version=PARSE_PROMPT_VERSION,
        model="claude-x",
        raw_response="(omitted)",
        latency_ms=120,
        error=None,
    )

    new_id = journal.insert_raw_post(
        conn,
        post_id="gate-2d-2",
        post_text="$AAPL 6/20 $185c @ 2.50",
        posted_at=signal.posted_at,
        received_at=datetime.now(timezone.utc),
        parse_result=parse_result_to_journal_dict(result),
        actionable=True,
    )

    with conn.cursor() as cur:
        cur.execute(
            "SELECT post_id, actionable, parse_result FROM x_posts WHERE id = %s",
            (new_id,),
        )
        row = cur.fetchone()
    assert row is not None
    post_id, actionable, payload = row
    assert post_id == "gate-2d-2"
    assert actionable is True
    assert payload["signal"]["ticker"] == "AAPL"
    assert payload["signal"]["strike"] == "185.00"
    assert payload["parse_version"] == PARSE_PROMPT_VERSION
    assert payload["model"] == "claude-x"
    assert payload["error"] is None
    assert "raw_response" not in payload  # we strip it for storage


def test_duplicate_post_id_upserts_preserves_id(conn: psycopg.Connection) -> None:
    """Stream re-delivery on reconnect must not crash; row id is stable."""
    posted_at = datetime(2026, 5, 12, 14, 30, tzinfo=timezone.utc)
    received_at = datetime(2026, 5, 12, 14, 30, 1, tzinfo=timezone.utc)

    first_id = journal.insert_raw_post(
        conn,
        post_id="dup-1",
        post_text="initial",
        posted_at=posted_at,
        received_at=received_at,
        parse_result=None,
        actionable=False,
    )
    second_id = journal.insert_raw_post(
        conn,
        post_id="dup-1",
        post_text="initial",
        posted_at=posted_at,
        received_at=received_at,
        parse_result={"updated": True},
        actionable=True,
    )
    assert first_id == second_id

    with conn.cursor() as cur:
        cur.execute("SELECT count(*), max(actionable::int) FROM x_posts WHERE post_id = %s", ("dup-1",))
        count, max_actionable = cur.fetchone()
    assert count == 1  # upsert, not duplicate insert
    assert max_actionable == 1  # actionable updated to True on re-deliver
