"""Tests for config — paper-mode guard and env loading (Phase 1 acceptance gate)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from x_alpaca_trading_bot.config import (
    PAPER_BASE_URL,
    Config,
    assert_paper_mode,
)


# ---- Paper-mode guard ----

def test_paper_url_passes() -> None:
    assert_paper_mode(PAPER_BASE_URL)  # must not raise


def test_live_url_rejected() -> None:
    with pytest.raises(RuntimeError, match="paper-only"):
        assert_paper_mode("https://api.alpaca.markets")


def test_empty_url_rejected() -> None:
    with pytest.raises(RuntimeError, match="paper-only"):
        assert_paper_mode("")


# ---- Config loading ----

_MINIMAL_ENV = {
    "X_BEARER_TOKEN": "x-bearer",
    "X_TARGET_ACCOUNT_ID": "123",
    "ANTHROPIC_API_KEY": "anthropic-key",
    "ALPACA_API_KEY": "alpaca-key",
    "ALPACA_SECRET_KEY": "alpaca-secret",
    "ALPACA_BASE_URL": PAPER_BASE_URL,
    "POLYGON_API_KEY": "polygon-key",
    "SUPABASE_URL": "https://example.supabase.co",
    "SUPABASE_KEY": "supabase-key",
    "DATABASE_URL": "postgresql://u:p@localhost:5432/db",
    "TELEGRAM_BOT_TOKEN": "tg-token",
    "TELEGRAM_CHAT_ID": "tg-chat",
}

_TUNABLE_VARS = (
    "STOP_LOSS_PCT",
    "DAILY_LOSS_KILL_PCT",
    "MAX_CONSECUTIVE_LOSSES",
    "MAX_FILL_WAIT_SECONDS",
    "SIGNAL_STALE_SECONDS",
    "PRICE_DEVIATION_PCT",
)


def _apply_env(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)


def test_config_loads_tunables_with_defaults(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    for key in _TUNABLE_VARS:
        monkeypatch.delenv(key, raising=False)
    _apply_env(monkeypatch, _MINIMAL_ENV)

    empty_env = tmp_path / "empty.env"
    empty_env.write_text("")
    cfg = Config.load(empty_env)

    assert cfg.alpaca_base_url == PAPER_BASE_URL
    assert cfg.stop_loss_pct == Decimal("0.20")
    assert isinstance(cfg.stop_loss_pct, Decimal)
    assert cfg.daily_loss_kill_pct == Decimal("0.03")
    assert isinstance(cfg.daily_loss_kill_pct, Decimal)
    assert cfg.max_consecutive_losses == 4
    assert cfg.max_fill_wait_seconds == 60
    assert cfg.signal_stale_seconds == 180
    assert cfg.price_deviation_pct == Decimal("0.10")
    assert isinstance(cfg.price_deviation_pct, Decimal)


def test_missing_required_var_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _apply_env(monkeypatch, _MINIMAL_ENV)
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)

    empty_env = tmp_path / "empty.env"
    empty_env.write_text("")
    with pytest.raises(RuntimeError, match="ALPACA_API_KEY"):
        Config.load(empty_env)


# ---- X target account loading ----

def test_legacy_singular_x_target_loads(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """X_TARGET_ACCOUNT_ID (legacy) still produces a one-element tuple so
    existing single-account deploys keep working."""
    _apply_env(monkeypatch, _MINIMAL_ENV)
    monkeypatch.delenv("X_TARGET_ACCOUNT_IDS", raising=False)
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("")
    cfg = Config.load(empty_env)
    assert cfg.x_target_account_ids == ("123",)


def test_multi_account_csv_loads(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """X_TARGET_ACCOUNT_IDS overrides the legacy singular when both are set."""
    _apply_env(monkeypatch, _MINIMAL_ENV)
    monkeypatch.setenv("X_TARGET_ACCOUNT_IDS", "111, 222 ,333")
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("")
    cfg = Config.load(empty_env)
    assert cfg.x_target_account_ids == ("111", "222", "333")


def test_missing_both_x_targets_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """At least one of X_TARGET_ACCOUNT_IDS or X_TARGET_ACCOUNT_ID must be set."""
    _apply_env(monkeypatch, _MINIMAL_ENV)
    monkeypatch.delenv("X_TARGET_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("X_TARGET_ACCOUNT_IDS", raising=False)
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("")
    with pytest.raises(RuntimeError, match="X target account"):
        Config.load(empty_env)


def test_max_concurrent_positions_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Defaults to 50 when MAX_CONCURRENT_POSITIONS is unset (effectively
    no cap for single-account setups)."""
    _apply_env(monkeypatch, _MINIMAL_ENV)
    monkeypatch.delenv("MAX_CONCURRENT_POSITIONS", raising=False)
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("")
    cfg = Config.load(empty_env)
    assert cfg.max_concurrent_positions == 50


def test_max_concurrent_positions_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _apply_env(monkeypatch, _MINIMAL_ENV)
    monkeypatch.setenv("MAX_CONCURRENT_POSITIONS", "3")
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("")
    cfg = Config.load(empty_env)
    assert cfg.max_concurrent_positions == 3
