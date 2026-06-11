"""Config loading and the paper-mode startup guard.

All money-related percentages are `Decimal`. The bot refuses to start unless
`ALPACA_BASE_URL` is the documented paper endpoint.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

PAPER_BASE_URL = "https://paper-api.alpaca.markets"

REQUIRED_VARS: tuple[str, ...] = (
    "X_BEARER_TOKEN",
    "X_TARGET_ACCOUNT_ID",
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
    x_target_account_id: str
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

        return cls(
            x_bearer_token=os.environ["X_BEARER_TOKEN"],
            x_target_account_id=os.environ["X_TARGET_ACCOUNT_ID"],
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
            disable_x_stream=os.environ.get("DISABLE_X_STREAM", "").lower() in ("1", "true", "yes"),
        )
