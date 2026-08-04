"""Config loading and the paper-mode startup guard.

All money-related percentages are `Decimal`. The bot refuses to start unless
`ALPACA_BASE_URL` is the documented paper endpoint.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import time
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

PAPER_BASE_URL = "https://paper-api.alpaca.markets"

REQUIRED_VARS: tuple[str, ...] = (
    "X_BEARER_TOKEN",
    # X target account IDs validated separately — see load() below — so the
    # singular X_TARGET_ACCOUNT_ID stays as a backwards-compat fallback for
    # the multi-account X_TARGET_ACCOUNT_IDS list.
    "ANTHROPIC_API_KEY",
    "ALPACA_API_KEY",
    "ALPACA_SECRET_KEY",
    "ALPACA_BASE_URL",
    "POLYGON_API_KEY",
    "SUPABASE_URL",
    "SUPABASE_KEY",
    "DATABASE_URL",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
)


def _parse_cutoff(raw: str) -> time | None:
    """Parse SCANNER_BREAKOUT_CUTOFF ("HH:MM", ET wall clock) or None.

    A malformed value raises rather than silently reverting to the
    default — a mis-set cutoff would quietly change which events get
    logged, which is exactly the kind of drift Phase A exists to avoid.
    """
    raw = raw.strip()
    if not raw:
        return None
    try:
        hh, mm = raw.split(":")
        return time(int(hh), int(mm))
    except (ValueError, TypeError) as exc:
        raise RuntimeError(
            f"SCANNER_BREAKOUT_CUTOFF must be HH:MM (got {raw!r})"
        ) from exc


def assert_paper_mode(base_url: str) -> None:
    """Hard-fail unless the configured Alpaca base URL is the paper endpoint."""
    if base_url != PAPER_BASE_URL:
        raise RuntimeError(
            f"Refusing to start: ALPACA_BASE_URL must be {PAPER_BASE_URL!r}, "
            f"got {base_url!r}. This bot is paper-only."
        )


@dataclass(frozen=True)
class Config:
    # Required credentials
    x_bearer_token: str
    x_target_account_ids: tuple[str, ...]
    anthropic_api_key: str
    alpaca_api_key: str
    alpaca_secret_key: str
    alpaca_base_url: str
    polygon_api_key: str
    supabase_url: str
    supabase_key: str
    database_url: str
    telegram_bot_token: str
    telegram_chat_id: str

    # Tunables (defaults in classmethod load)
    stop_loss_pct: Decimal
    daily_loss_kill_pct: Decimal
    max_consecutive_losses: int
    max_fill_wait_seconds: int
    signal_stale_seconds: int
    price_deviation_pct: Decimal

    # Optional IV ceiling at entry. None disables the gate. Driven by early
    # pattern analysis showing losers had ~15-point higher median IV than
    # winners — re-evaluate at N=50+ closed trades before promoting from
    # operator-toggled to a hard default.
    max_entry_iv: Decimal | None = None

    # Cap on simultaneously open positions. New entries that would push the
    # bot above this number are skipped at the orchestrator level (the
    # existing X-stream + scanner pipelines share this budget). Defaults to
    # a generous ceiling so single-account setups behave as before — tighten
    # when adding a second signal source so they don't dogpile capital.
    max_concurrent_positions: int = 50

    # Scanner (independent signal source). Default OFF so behavior is
    # unchanged until the operator opts in. When enabled, the orchestrator
    # spawns a background thread that runs the failed-breakout scanner
    # every scanner_interval_seconds during RTH. Phase A is log-only —
    # detected events land in scanner_events but do NOT enter the trade
    # queue.
    scanner_enabled: bool = False
    scanner_interval_seconds: int = 300
    # None means "use scanners.failed_breakout.DEFAULT_UNIVERSE". A comma-
    # separated string overrides — same shape as X_TARGET_ACCOUNT_IDS.
    scanner_universe: tuple[str, ...] | None = None
    # Latest ET wall-clock time for a breakout to qualify. None means "use
    # scanners.failed_breakout.DEFAULT_BREAKOUT_CUTOFF" (10:30 ET as of
    # 2026-07-30; late-morning failures showed no edge in the live sample).
    # Env format: SCANNER_BREAKOUT_CUTOFF=HH:MM.
    scanner_breakout_cutoff: time | None = None
    # Which lab hypotheses run (SCANNER_PROGRAM.md Phase S1). None means
    # all registered in scanners.lab.HYPOTHESES. Comma-separated names;
    # unknown names fail startup (validated in main via resolve_hypotheses).
    scanner_hypotheses: tuple[str, ...] | None = None

    # Phase S2 — shares-based execution of the validated failed-breakout
    # slice. Ships DISARMED (owner decision 2026-08-04): the arm switch
    # stays false until ~4 weeks of fresh volume_ratio data confirm the
    # slice out-of-sample. Entry filter, sizing, and caps are
    # owner-confirmed in SCANNER_PROGRAM.md.
    scanner_trading_enabled: bool = False
    scanner_trade_notional: Decimal = Decimal("1000")
    scanner_max_concurrent: int = 3
    scanner_min_volume_ratio: Decimal = Decimal("1.0")

    # Operator switches
    disable_x_stream: bool = False        # skip X stream connect + suppress x_stream kill switch

    @classmethod
    def load(cls, env_file: Path | str | None = None) -> "Config":
        """Read environment (and optional .env file) into a frozen Config.

        Pass an explicit `env_file` to scope tests to a known file; otherwise
        the default search behavior of python-dotenv is used.
        """
        # override=True so .env wins over inherited (often empty) shell vars.
        # In production there is no .env file, so this is a no-op there.
        if env_file is not None:
            load_dotenv(env_file, override=True)
        else:
            load_dotenv(override=True)

        missing = [v for v in REQUIRED_VARS if not os.environ.get(v)]
        if missing:
            raise RuntimeError(
                f"Missing required environment variables: {', '.join(missing)}. "
                f"See .env.example."
            )

        # X target accounts — prefer multi-account list; fall back to the
        # singular var for backwards compatibility. One of the two must be
        # set or the bot has nothing to listen to.
        ids_raw = os.environ.get("X_TARGET_ACCOUNT_IDS", "").strip()
        if ids_raw:
            x_target_account_ids = tuple(
                aid.strip() for aid in ids_raw.split(",") if aid.strip()
            )
        elif os.environ.get("X_TARGET_ACCOUNT_ID", "").strip():
            x_target_account_ids = (os.environ["X_TARGET_ACCOUNT_ID"].strip(),)
        else:
            raise RuntimeError(
                "Missing X target account configuration: set "
                "X_TARGET_ACCOUNT_IDS (comma-separated) or X_TARGET_ACCOUNT_ID."
            )

        return cls(
            x_bearer_token=os.environ["X_BEARER_TOKEN"],
            x_target_account_ids=x_target_account_ids,
            anthropic_api_key=os.environ["ANTHROPIC_API_KEY"],
            alpaca_api_key=os.environ["ALPACA_API_KEY"],
            alpaca_secret_key=os.environ["ALPACA_SECRET_KEY"],
            alpaca_base_url=os.environ["ALPACA_BASE_URL"],
            polygon_api_key=os.environ["POLYGON_API_KEY"],
            supabase_url=os.environ["SUPABASE_URL"],
            supabase_key=os.environ["SUPABASE_KEY"],
            database_url=os.environ["DATABASE_URL"],
            telegram_bot_token=os.environ["TELEGRAM_BOT_TOKEN"],
            telegram_chat_id=os.environ["TELEGRAM_CHAT_ID"],
            stop_loss_pct=Decimal(os.environ.get("STOP_LOSS_PCT", "0.20")),
            daily_loss_kill_pct=Decimal(os.environ.get("DAILY_LOSS_KILL_PCT", "0.03")),
            max_consecutive_losses=int(os.environ.get("MAX_CONSECUTIVE_LOSSES", "4")),
            max_fill_wait_seconds=int(os.environ.get("MAX_FILL_WAIT_SECONDS", "60")),
            signal_stale_seconds=int(os.environ.get("SIGNAL_STALE_SECONDS", "180")),
            price_deviation_pct=Decimal(os.environ.get("PRICE_DEVIATION_PCT", "0.10")),
            max_entry_iv=(
                Decimal(os.environ["MAX_ENTRY_IV"])
                if os.environ.get("MAX_ENTRY_IV", "").strip()
                else None
            ),
            max_concurrent_positions=int(
                os.environ.get("MAX_CONCURRENT_POSITIONS", "50")
            ),
            scanner_enabled=(
                os.environ.get("SCANNER_ENABLED", "").lower() in ("1", "true", "yes")
            ),
            scanner_interval_seconds=int(
                os.environ.get("SCANNER_INTERVAL_SECONDS", "300")
            ),
            scanner_universe=(
                tuple(
                    t.strip() for t in os.environ["SCANNER_UNIVERSE"].split(",")
                    if t.strip()
                )
                if os.environ.get("SCANNER_UNIVERSE", "").strip()
                else None
            ),
            scanner_breakout_cutoff=_parse_cutoff(
                os.environ.get("SCANNER_BREAKOUT_CUTOFF", "")
            ),
            scanner_hypotheses=(
                tuple(
                    h.strip() for h in os.environ["SCANNER_HYPOTHESES"].split(",")
                    if h.strip()
                )
                if os.environ.get("SCANNER_HYPOTHESES", "").strip()
                else None
            ),
            scanner_trading_enabled=(
                os.environ.get("SCANNER_TRADING_ENABLED", "").lower()
                in ("1", "true", "yes")
            ),
            scanner_trade_notional=Decimal(
                os.environ.get("SCANNER_TRADE_NOTIONAL", "1000")
            ),
            scanner_max_concurrent=int(
                os.environ.get("SCANNER_MAX_CONCURRENT", "3")
            ),
            scanner_min_volume_ratio=Decimal(
                os.environ.get("SCANNER_MIN_VOLUME_RATIO", "1.0")
            ),
            disable_x_stream=os.environ.get("DISABLE_X_STREAM", "").lower() in ("1", "true", "yes"),
        )
