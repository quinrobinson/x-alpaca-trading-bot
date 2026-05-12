"""journal — DB writes for the bot. Phase 2 partial: x_posts only.

This module owns every INSERT/UPDATE against the database. Later phases extend
it with `insert_signal`, `insert_order`, `insert_fill`, `write_snapshot`, etc.
Telegram alerts also live here per spec §2.2.

Phase 2 implements only the x_posts path so Phase 2 gate 2.d ("posts written
to DB within 1 second of receipt") can be verified.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

import psycopg

logger = logging.getLogger(__name__)


def insert_raw_post(
    conn: psycopg.Connection,
    *,
    post_id: str,
    post_text: str,
    posted_at: datetime,
    received_at: datetime,
    parse_result: dict[str, Any] | None,
    actionable: bool,
) -> int:
    """Insert (or upsert) a row into x_posts; return the row id.

    Upsert semantics: stream re-deliveries on reconnect must not crash. The
    post_id column is UNIQUE; on conflict we update parse_result and
    actionable in case the parse rerun produced a better classification.

    parse_result must already be JSON-serializable (e.g. via
    parser.parse_result_to_journal_dict). Pass None for a non-parsed write.
    """
    payload = json.dumps(parse_result) if parse_result is not None else None
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO x_posts
                (posted_at, received_at, post_id, post_text, parse_result, actionable)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (post_id) DO UPDATE
              SET parse_result = EXCLUDED.parse_result,
                  actionable   = EXCLUDED.actionable
            RETURNING id
            """,
            (posted_at, received_at, post_id, post_text, payload, actionable),
        )
        row = cur.fetchone()
    conn.commit()
    assert row is not None
    return int(row[0])
