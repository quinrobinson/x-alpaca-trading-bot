"""Phase 2 — parse raw X posts into structured options-trade signals via Claude.

Contract:
    parse_post(post_text, posted_at, client) -> ParseResult

Returns `Signal` if all required fields parse cleanly; returns None for any
non-signal post, missing field, ambiguous content, or model-side error. The
caller is responsible for journaling the full ParseResult (signal + metadata)
to the x_posts.parse_result JSONB column.

The prompt is versioned. Bump PARSE_PROMPT_VERSION whenever SYSTEM_PROMPT or
parse logic changes so journal entries are comparable over time.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, Protocol

logger = logging.getLogger(__name__)

PARSE_PROMPT_VERSION = "v1"
DEFAULT_MODEL = "claude-sonnet-4-20250514"  # from X_ALPACA_OPTIONS_HANDOFF.md §2.2
MAX_TOKENS = 512

OptionType = Literal["call", "put"]


@dataclass(frozen=True)
class Signal:
    """A structured options trade signal parsed from a single X post."""

    ticker: str
    option_type: OptionType
    strike: Decimal
    expiration: date
    posted_price: Decimal
    posted_at: datetime


@dataclass(frozen=True)
class ParseResult:
    """Result of one parse attempt — signal (if any) plus metadata for journaling."""

    signal: Signal | None
    parse_version: str
    model: str
    raw_response: str
    latency_ms: int
    error: str | None = None  # populated if parse failed for a non-content reason


# Minimal protocol so tests can inject a fake client.
class _AnthropicMessages(Protocol):
    def create(self, **kwargs: Any) -> Any: ...


class AnthropicClient(Protocol):
    messages: _AnthropicMessages


SYSTEM_PROMPT = """You parse options-trade signals from X (Twitter) posts.

Return ONLY a single JSON object on one line. No prose, no markdown fences.

Schema:
{
  "ticker": "AAPL",           // uppercase, no $ sign
  "option_type": "call"|"put",
  "strike": "185.00",         // string-formatted decimal
  "expiration": "2026-06-20", // ISO date YYYY-MM-DD
  "entry_price": "2.50"       // string-formatted decimal, the posted target option price
}

If the post is NOT a trade signal, OR any required field is missing, ambiguous,
or contradicted by the post, return exactly:
null

Rules:
- Ticker must be explicit ($AAPL, AAPL, TSLA). Slang names (e.g. "the spider" for SPY) → null.
- option_type must be unambiguously "call" or "put". "c"/"call"/"calls" → call. "p"/"put"/"puts" → put.
- Strike is the numeric option strike. Reject if absent or a range.
- Expiration must resolve to a single calendar date. Phrases like "weekly", "monthly",
  "0DTE", "Friday" without a clear date → null (ambiguous).
- entry_price is the OPTION premium target the poster is calling out, not the underlying price.
- Multiple distinct signals in one post → null.
- Pure commentary, news, polls, replies, retweets without a new signal → null.

Examples:
Input: "$AAPL 6/20 $185c @ 2.50"
Output: {"ticker":"AAPL","option_type":"call","strike":"185","expiration":"2026-06-20","entry_price":"2.50"}

Input: "Looking at TSLA puts, 230 strike, June 6 expiry, entering at 4.20"
Output: {"ticker":"TSLA","option_type":"put","strike":"230","expiration":"2026-06-06","entry_price":"4.20"}

Input: "SPY 450c looking good"
Output: null

Input: "Market is wild today"
Output: null
"""


def parse_post(
    post_text: str,
    posted_at: datetime,
    client: AnthropicClient,
    *,
    model: str = DEFAULT_MODEL,
) -> ParseResult:
    """Parse one X post into a Signal (or None) plus journaling metadata.

    Never raises for content-shape errors — non-signals, malformed JSON, missing
    fields, and API errors all return a ParseResult with signal=None and an
    optional error message. The caller logs the full result.
    """
    started = time.perf_counter()
    raw = ""
    error: str | None = None

    try:
        response = client.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": post_text}],
        )
        raw = _extract_text(response)
    except Exception as exc:  # noqa: BLE001 — never raise from parser
        latency_ms = int((time.perf_counter() - started) * 1000)
        logger.warning("parser API error: %s", exc)
        return ParseResult(
            signal=None,
            parse_version=PARSE_PROMPT_VERSION,
            model=model,
            raw_response="",
            latency_ms=latency_ms,
            error=f"api_error: {exc!r}",
        )

    latency_ms = int((time.perf_counter() - started) * 1000)
    signal, parse_error = _decode_signal(raw, posted_at)
    if parse_error:
        error = parse_error
        logger.info("parser rejected post: %s (raw=%r)", parse_error, raw[:200])

    return ParseResult(
        signal=signal,
        parse_version=PARSE_PROMPT_VERSION,
        model=model,
        raw_response=raw,
        latency_ms=latency_ms,
        error=error,
    )


def _extract_text(response: Any) -> str:
    """Pull the text block out of an Anthropic SDK response. Tolerant of shape."""
    content = getattr(response, "content", None)
    if not content:
        return ""
    first = content[0]
    return getattr(first, "text", "") or ""


def _decode_signal(raw: str, posted_at: datetime) -> tuple[Signal | None, str | None]:
    """Parse Claude's JSON-or-null response. Returns (signal, error_reason)."""
    text = raw.strip()
    if not text:
        return None, "empty_response"

    # Strip accidental markdown fences if the model misbehaves.
    if text.startswith("```"):
        text = text.strip("`")
        # tolerate ```json prefix
        if "\n" in text:
            text = text.split("\n", 1)[1]
        text = text.strip("`").strip()

    if text == "null":
        return None, None  # legitimate non-signal — not an error

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"malformed_json: {exc.msg}"

    if payload is None:
        return None, None

    if not isinstance(payload, dict):
        return None, "not_object"

    required = {"ticker", "option_type", "strike", "expiration", "entry_price"}
    missing = required - payload.keys()
    if missing:
        return None, f"missing_fields: {sorted(missing)}"

    # Field validation
    ticker = str(payload["ticker"]).strip().upper().lstrip("$")
    if not ticker or not ticker.replace(".", "").isalnum():
        return None, "invalid_ticker"

    option_type = str(payload["option_type"]).strip().lower()
    if option_type not in ("call", "put"):
        return None, "invalid_option_type"

    try:
        strike = Decimal(str(payload["strike"]))
        entry_price = Decimal(str(payload["entry_price"]))
    except (InvalidOperation, TypeError, ValueError) as exc:
        return None, f"invalid_decimal: {exc}"

    if strike <= 0 or entry_price <= 0:
        return None, "non_positive_price"

    try:
        expiration = date.fromisoformat(str(payload["expiration"]))
    except (TypeError, ValueError) as exc:
        return None, f"invalid_expiration: {exc}"

    return (
        Signal(
            ticker=ticker,
            option_type=option_type,  # type: ignore[arg-type]
            strike=strike,
            expiration=expiration,
            posted_price=entry_price,
            posted_at=posted_at,
        ),
        None,
    )


def signal_to_dict(signal: Signal) -> dict[str, Any]:
    """Serialize Signal for JSONB storage. Decimals → strings, dates → ISO."""
    data = asdict(signal)
    data["strike"] = str(signal.strike)
    data["posted_price"] = str(signal.posted_price)
    data["expiration"] = signal.expiration.isoformat()
    data["posted_at"] = signal.posted_at.isoformat()
    return data
