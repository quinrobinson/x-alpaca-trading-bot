"""Telegram notifier — outbound alerts for trades and kill switches.

All sends are fault-tolerant: a Telegram outage, bad token, or DNS hiccup
must NEVER crash the orchestrator. Every call logs on failure and moves
on.

Messages are plain text (no markdown / no links per operator choice).
Each notification type has its own format helper so the wire format is
easy to eyeball in tests.
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal
from typing import Any, Protocol

import httpx

logger = logging.getLogger(__name__)


class _HttpPost(Protocol):
    """Just enough of httpx.Client for the notifier to send a message.

    Tests pass a stub recording payloads instead of hitting the real API.
    """

    def post(
        self,
        url: str,
        *,
        json: dict[str, Any] | None = ...,
        timeout: float | None = ...,
    ) -> Any: ...


class TelegramNotifier:
    """Thin wrapper around Telegram's bot sendMessage endpoint.

    Construct once at startup; reuse for every alert. Pass `http_client`
    explicitly in tests; production uses a default httpx.Client with a
    short timeout so a stuck request can't stall a trade-entry path.
    """

    BASE_URL = "https://api.telegram.org"
    SEND_TIMEOUT_SECONDS = 5.0

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        *,
        http_client: _HttpPost | None = None,
    ) -> None:
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._http = http_client or httpx.Client(timeout=self.SEND_TIMEOUT_SECONDS)

    # ---- Public notify_* helpers ----

    def notify_trade_entered(
        self,
        *,
        ticker: str,
        option_type: str,
        strike: Decimal,
        expiration: date,
        qty: int,
        entry_price: Decimal,
        post_text: str | None,
    ) -> None:
        self._send(_format_trade_entered(
            ticker=ticker, option_type=option_type, strike=strike,
            expiration=expiration, qty=qty, entry_price=entry_price,
            post_text=post_text,
        ))

    def notify_trade_closed(
        self,
        *,
        ticker: str,
        option_type: str,
        strike: Decimal,
        expiration: date,
        qty: int,
        entry_price: Decimal,
        exit_price: Decimal,
        pnl: Decimal,
        pnl_pct: Decimal,
        exit_reason: str,
        hold_minutes: int,
    ) -> None:
        self._send(_format_trade_closed(
            ticker=ticker, option_type=option_type, strike=strike,
            expiration=expiration, qty=qty, entry_price=entry_price,
            exit_price=exit_price, pnl=pnl, pnl_pct=pnl_pct,
            exit_reason=exit_reason, hold_minutes=hold_minutes,
        ))

    def notify_killswitch_tripped(
        self,
        *,
        newly_tripped: list[str],
        all_tripped: list[str],
        reason: str | None,
    ) -> None:
        self._send(_format_killswitch_tripped(
            newly_tripped=newly_tripped,
            all_tripped=all_tripped,
            reason=reason,
        ))

    # ---- Internal ----

    def _send(self, text: str) -> None:
        url = f"{self.BASE_URL}/bot{self._bot_token}/sendMessage"
        payload = {"chat_id": self._chat_id, "text": text}
        try:
            r = self._http.post(url, json=payload, timeout=self.SEND_TIMEOUT_SECONDS)
            status = getattr(r, "status_code", None)
            if status is not None and status >= 400:
                # Don't log the body — it might echo the bot token.
                logger.warning("telegram send returned %s", status)
        except Exception:  # noqa: BLE001
            logger.exception("telegram send failed; alert dropped")


# ---- Pure formatters (one per notification type) ---------------------------

def _fmt_contract(ticker: str, option_type: str, strike: Decimal, expiration: date) -> str:
    t = option_type[:1].upper()
    exp = f"{expiration.month}/{expiration.day}"
    return f"{ticker} ${strike}{t} {exp}"


def _fmt_money(value: Decimal | int | float) -> str:
    v = Decimal(value) if not isinstance(value, Decimal) else value
    sign = "-" if v < 0 else ""
    return f"{sign}${abs(v):.2f}"


def _fmt_pct(value: Decimal) -> str:
    sign = "+" if value > 0 else ""
    return f"{sign}{value * Decimal(100):.2f}%"


def _format_trade_entered(
    *,
    ticker: str,
    option_type: str,
    strike: Decimal,
    expiration: date,
    qty: int,
    entry_price: Decimal,
    post_text: str | None,
) -> str:
    contract = _fmt_contract(ticker, option_type, strike, expiration)
    lines = [
        f"🟢 ENTERED · {contract}",
        f"qty {qty} @ {_fmt_money(entry_price)} (per share)",
    ]
    if post_text:
        # Trim long tweets so the message stays one screen on mobile.
        trimmed = post_text if len(post_text) <= 200 else post_text[:197] + "…"
        lines.append(f"\n\"{trimmed}\"")
    return "\n".join(lines)


def _format_trade_closed(
    *,
    ticker: str,
    option_type: str,
    strike: Decimal,
    expiration: date,
    qty: int,
    entry_price: Decimal,
    exit_price: Decimal,
    pnl: Decimal,
    pnl_pct: Decimal,
    exit_reason: str,
    hold_minutes: int,
) -> str:
    contract = _fmt_contract(ticker, option_type, strike, expiration)
    icon = "✅" if pnl > 0 else "🔴"
    label = "WIN" if pnl > 0 else "LOSS"
    return (
        f"{icon} CLOSED · {label} · {contract}\n"
        f"{_fmt_money(pnl)} ({_fmt_pct(pnl_pct)}) · {hold_minutes}m hold\n"
        f"entry {_fmt_money(entry_price)} → exit {_fmt_money(exit_price)}\n"
        f"reason: {exit_reason}"
    )


def _format_killswitch_tripped(
    *,
    newly_tripped: list[str],
    all_tripped: list[str],
    reason: str | None,
) -> str:
    head_reason = reason or (newly_tripped[0] if newly_tripped else "unknown")
    body = (
        f"⛔ KILL SWITCH · {head_reason}\n"
        f"newly tripped: {', '.join(newly_tripped) or '(none)'}\n"
        f"all active:    {', '.join(all_tripped) or '(none)'}"
    )
    return body
