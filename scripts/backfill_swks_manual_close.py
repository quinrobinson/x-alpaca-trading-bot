#!/usr/bin/env python3
"""One-off backfill for the SWKS trade record corrupted by the manual-close
+ reconcile race bug (signal_id=221, fix shipped in commit after 2e5856d).

The position was actually closed at $0.75 (six contracts, ~$30 profit) per
Alpaca's order record (cid xab-close-88ff3a0548ac4503). The trade row got
written at entry price ($0.70) with exit_reason='external_close' because
reconcile fired in the same tick as the manual close and didn't know
about manual_close_order_id.

Idempotent: re-running just confirms the corrected values are in place.
Safe to delete from the repo after one successful run.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path


def main() -> int:
    from dotenv import load_dotenv

    load_dotenv(Path("/opt/x-alpaca-trading-bot/.env"), override=False)
    load_dotenv(override=False)  # local dev fallback

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("error: DATABASE_URL not set", file=sys.stderr)
        return 1

    import psycopg

    # Ground truth from Alpaca's order record.
    real_exit_price = Decimal("0.75")
    real_closed_at = datetime(2026, 5, 28, 16, 3, 41, 910052, tzinfo=timezone.utc)
    real_reason = "manual_close"

    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, signal_id, entry_price, exit_price, gross_pnl,
                       exit_reason, qty
                FROM trades
                WHERE ticker = 'SWKS'
                  AND exit_reason IN ('external_close', 'manual_close')
                ORDER BY id DESC
                LIMIT 1
                """
            )
            row = cur.fetchone()
            if row is None:
                print("No SWKS trade row found; nothing to backfill.")
                return 0
            trade_id, signal_id, entry_price, exit_price, gross_pnl, reason, qty = row
            print(f"before: trade_id={trade_id} signal_id={signal_id} "
                  f"entry={entry_price} exit={exit_price} "
                  f"gross_pnl={gross_pnl} reason={reason} qty={qty}")

            entry = Decimal(str(entry_price))
            real_gross_pnl = (real_exit_price - entry) * Decimal(qty) * Decimal(100)
            real_pnl_pct = (
                (real_exit_price - entry) / entry if entry > 0 else Decimal(0)
            )

            cur.execute(
                """
                UPDATE trades
                SET exit_price  = %s,
                    exit_reason = %s,
                    closed_at   = %s,
                    gross_pnl   = %s,
                    pnl_pct     = %s
                WHERE id = %s
                """,
                (real_exit_price, real_reason, real_closed_at,
                 real_gross_pnl, real_pnl_pct, trade_id),
            )
        conn.commit()

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, signal_id, entry_price, exit_price, gross_pnl,
                       pnl_pct, exit_reason, closed_at
                FROM trades WHERE id = %s
                """,
                (trade_id,),
            )
            print(f"after:  {cur.fetchone()}")

    print("done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
