"""Unit tests for x_alpaca_trading_bot.alerts.TelegramNotifier.

No real HTTP. A stub captures the URL + payload so we can assert the
wire format AND that failures stay swallowed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from x_alpaca_trading_bot.alerts import TelegramNotifier


@dataclass
class _StubResponse:
    status_code: int = 200


@dataclass
class _StubClient:
    """Captures requests for assertion. Pass `raises` to simulate a failure."""

    posts: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    raises: Exception | None = None
    status_code: int = 200

    def post(self, url: str, *, json: dict[str, Any] | None = None, timeout: float | None = None) -> Any:
        if self.raises is not None:
            raise self.raises
        self.posts.append((url, json or {}))
        return _StubResponse(status_code=self.status_code)


def _notifier(client: _StubClient | None = None) -> tuple[TelegramNotifier, _StubClient]:
    c = client or _StubClient()
    return TelegramNotifier(bot_token="TKN", chat_id="42", http_client=c), c


def test_notify_trade_entered_posts_expected_payload() -> None:
    n, c = _notifier()
    n.notify_trade_entered(
        ticker="AAPL",
        option_type="call",
        strike=Decimal("185"),
        expiration=date(2026, 6, 20),
        qty=2,
        entry_price=Decimal("2.50"),
        post_text="$AAPL 6/20 $185c @ 2.50",
    )
    assert len(c.posts) == 1
    url, body = c.posts[0]
    assert url == "https://api.telegram.org/botTKN/sendMessage"
    assert body["chat_id"] == "42"
    text = body["text"]
    assert "ENTERED" in text
    assert "AAPL $185C 6/20" in text
    assert "qty 2 @ $2.50" in text
    assert "$AAPL 6/20 $185c @ 2.50" in text


def test_notify_trade_closed_win_shows_check_icon_and_pct() -> None:
    n, c = _notifier()
    n.notify_trade_closed(
        ticker="AAPL", option_type="call", strike=Decimal("185"),
        expiration=date(2026, 6, 20),
        qty=2, entry_price=Decimal("2.50"), exit_price=Decimal("3.25"),
        pnl=Decimal("150"), pnl_pct=Decimal("0.30"),
        exit_reason="profit_target", hold_minutes=42,
    )
    text = c.posts[0][1]["text"]
    assert "CLOSED" in text
    assert "WIN" in text
    assert "$150.00" in text
    assert "+30.00%" in text
    assert "42m hold" in text
    assert "profit_target" in text


def test_notify_trade_closed_loss_shows_red_label() -> None:
    n, c = _notifier()
    n.notify_trade_closed(
        ticker="AAPL", option_type="put", strike=Decimal("180"),
        expiration=date(2026, 6, 20),
        qty=1, entry_price=Decimal("2.50"), exit_price=Decimal("2.00"),
        pnl=Decimal("-50"), pnl_pct=Decimal("-0.20"),
        exit_reason="stop_loss", hold_minutes=8,
    )
    text = c.posts[0][1]["text"]
    assert "LOSS" in text
    assert "-$50.00" in text
    assert "-20.00%" in text


def test_notify_killswitch_payload() -> None:
    n, c = _notifier()
    n.notify_killswitch_tripped(
        newly_tripped=["daily_loss"],
        all_tripped=["daily_loss", "consecutive_losses"],
        reason="daily_loss",
    )
    text = c.posts[0][1]["text"]
    assert "KILL SWITCH" in text
    assert "daily_loss" in text
    assert "consecutive_losses" in text


def test_send_swallows_http_exception() -> None:
    """A Telegram outage must not propagate into the orchestrator."""
    n, c = _notifier(_StubClient(raises=RuntimeError("network down")))
    # Should NOT raise.
    n.notify_trade_entered(
        ticker="AAPL", option_type="call", strike=Decimal("185"),
        expiration=date(2026, 6, 20),
        qty=1, entry_price=Decimal("2.50"), post_text=None,
    )


def test_send_logs_on_400_response() -> None:
    """A 4xx response is logged but not raised."""
    client = _StubClient(status_code=401)
    n = TelegramNotifier(bot_token="TKN", chat_id="42", http_client=client)
    n.notify_killswitch_tripped(
        newly_tripped=["daily_loss"],
        all_tripped=["daily_loss"],
        reason="daily_loss",
    )
    # Still made the POST, no crash.
    assert len(client.posts) == 1
