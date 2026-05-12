"""Tests for parser — Phase 2 acceptance gate.

We mock the Anthropic SDK responses, so these tests cover the parser's
handling of Claude's output, not Claude's classification itself. End-to-end
parse accuracy against real posts is a separate manual verification step
once X_BEARER_TOKEN + ANTHROPIC_API_KEY are wired up.

Acceptance threshold: ≥ 90% of synthetic cases must produce the correct
signal-or-None classification.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

import pytest

from x_alpaca_trading_bot.parser import (
    PARSE_PROMPT_VERSION,
    ParseResult,
    Signal,
    parse_post,
    signal_to_dict,
)

POSTED_AT = datetime(2026, 5, 12, 14, 30, tzinfo=timezone.utc)


# ---- Fake Anthropic client ----

@dataclass
class _FakeContent:
    text: str


@dataclass
class _FakeResponse:
    content: list[_FakeContent]


class _FakeMessages:
    def __init__(self, response_text: str = "", raises: Exception | None = None):
        self._response_text = response_text
        self._raises = raises
        self.last_kwargs: dict[str, Any] | None = None

    def create(self, **kwargs: Any) -> _FakeResponse:
        self.last_kwargs = kwargs
        if self._raises:
            raise self._raises
        return _FakeResponse(content=[_FakeContent(text=self._response_text)])


class _FakeClient:
    def __init__(self, response_text: str = "", raises: Exception | None = None):
        self.messages = _FakeMessages(response_text, raises)


def _client(text: str) -> _FakeClient:
    return _FakeClient(text)


# ---- Happy paths ----

def test_parses_complete_call_signal() -> None:
    raw = '{"ticker":"AAPL","option_type":"call","strike":"185.00","expiration":"2026-06-20","entry_price":"2.50"}'
    result = parse_post("$AAPL 6/20 $185c @ 2.50", POSTED_AT, _client(raw))

    assert isinstance(result, ParseResult)
    assert result.signal is not None
    assert result.signal.ticker == "AAPL"
    assert result.signal.option_type == "call"
    assert result.signal.strike == Decimal("185.00")
    assert result.signal.expiration == date(2026, 6, 20)
    assert result.signal.posted_price == Decimal("2.50")
    assert result.signal.posted_at == POSTED_AT
    assert result.parse_version == PARSE_PROMPT_VERSION
    assert result.error is None
    assert isinstance(result.signal.strike, Decimal)
    assert isinstance(result.signal.posted_price, Decimal)


def test_parses_put_signal_with_different_format() -> None:
    raw = '{"ticker":"TSLA","option_type":"put","strike":"230","expiration":"2026-06-06","entry_price":"4.20"}'
    result = parse_post("Looking at TSLA puts, 230 strike, June 6, in at 4.20", POSTED_AT, _client(raw))

    assert result.signal is not None
    assert result.signal.option_type == "put"
    assert result.signal.ticker == "TSLA"
    assert result.signal.strike == Decimal("230")


def test_strips_dollar_sign_from_ticker() -> None:
    raw = '{"ticker":"$SPY","option_type":"call","strike":"450","expiration":"2026-05-20","entry_price":"1.85"}'
    result = parse_post("$SPY $450c 5/20 in @ 1.85", POSTED_AT, _client(raw))
    assert result.signal is not None
    assert result.signal.ticker == "SPY"


def test_handles_markdown_fence_response() -> None:
    raw = '```json\n{"ticker":"NVDA","option_type":"call","strike":"130","expiration":"2026-06-27","entry_price":"3.10"}\n```'
    result = parse_post("$NVDA 6/27 $130c @ 3.10", POSTED_AT, _client(raw))
    assert result.signal is not None
    assert result.signal.ticker == "NVDA"


# ---- Legitimate non-signal returns ----

def test_null_response_returns_none_without_error() -> None:
    result = parse_post("Market is wild today", POSTED_AT, _client("null"))
    assert result.signal is None
    assert result.error is None  # null is a clean classification, not a failure


def test_null_with_whitespace() -> None:
    result = parse_post("anything", POSTED_AT, _client("  null  "))
    assert result.signal is None
    assert result.error is None


# ---- Parse failure modes (signal None + error set) ----

def test_missing_field_returns_none_with_error() -> None:
    # Missing expiration
    raw = '{"ticker":"AAPL","option_type":"call","strike":"185","entry_price":"2.50"}'
    result = parse_post("incomplete post", POSTED_AT, _client(raw))
    assert result.signal is None
    assert result.error is not None
    assert "expiration" in result.error


def test_invalid_option_type_returns_none() -> None:
    raw = '{"ticker":"AAPL","option_type":"straddle","strike":"185","expiration":"2026-06-20","entry_price":"2.50"}'
    result = parse_post("$AAPL straddle", POSTED_AT, _client(raw))
    assert result.signal is None
    assert result.error == "invalid_option_type"


def test_non_positive_strike_returns_none() -> None:
    raw = '{"ticker":"AAPL","option_type":"call","strike":"0","expiration":"2026-06-20","entry_price":"2.50"}'
    result = parse_post("bad", POSTED_AT, _client(raw))
    assert result.signal is None
    assert result.error == "non_positive_price"


def test_invalid_decimal_returns_none() -> None:
    raw = '{"ticker":"AAPL","option_type":"call","strike":"abc","expiration":"2026-06-20","entry_price":"2.50"}'
    result = parse_post("bad", POSTED_AT, _client(raw))
    assert result.signal is None
    assert result.error is not None
    assert "invalid_decimal" in result.error


def test_invalid_expiration_returns_none() -> None:
    raw = '{"ticker":"AAPL","option_type":"call","strike":"185","expiration":"next-friday","entry_price":"2.50"}'
    result = parse_post("bad", POSTED_AT, _client(raw))
    assert result.signal is None
    assert result.error is not None
    assert "invalid_expiration" in result.error


def test_malformed_json_returns_none() -> None:
    result = parse_post("post", POSTED_AT, _client("not json {{{"))
    assert result.signal is None
    assert result.error is not None
    assert "malformed_json" in result.error


def test_empty_response_returns_none() -> None:
    result = parse_post("post", POSTED_AT, _client(""))
    assert result.signal is None
    assert result.error == "empty_response"


def test_non_object_returns_none() -> None:
    result = parse_post("post", POSTED_AT, _client('"just a string"'))
    assert result.signal is None
    assert result.error == "not_object"


def test_invalid_ticker_returns_none() -> None:
    raw = '{"ticker":"","option_type":"call","strike":"185","expiration":"2026-06-20","entry_price":"2.50"}'
    result = parse_post("post", POSTED_AT, _client(raw))
    assert result.signal is None
    assert result.error == "invalid_ticker"


# ---- API-level error handling ----

def test_api_error_returns_none_with_error_message() -> None:
    client = _FakeClient(raises=RuntimeError("connection refused"))
    result = parse_post("post", POSTED_AT, client)
    assert result.signal is None
    assert result.error is not None
    assert "api_error" in result.error
    assert result.parse_version == PARSE_PROMPT_VERSION  # metadata still populated


# ---- Metadata integrity ----

def test_metadata_populated_on_success() -> None:
    raw = '{"ticker":"AAPL","option_type":"call","strike":"185","expiration":"2026-06-20","entry_price":"2.50"}'
    client = _FakeClient(raw)
    result = parse_post("$AAPL 6/20 $185c @ 2.50", POSTED_AT, client, model="claude-x")
    assert result.model == "claude-x"
    assert result.raw_response == raw
    assert result.latency_ms >= 0
    # Client received the right kwargs
    assert client.messages.last_kwargs is not None
    assert client.messages.last_kwargs["model"] == "claude-x"
    assert client.messages.last_kwargs["max_tokens"] > 0
    assert "system" in client.messages.last_kwargs


# ---- Signal serialization ----

def test_signal_to_dict_serializes_decimals_and_dates() -> None:
    signal = Signal(
        ticker="AAPL",
        option_type="call",
        strike=Decimal("185.00"),
        expiration=date(2026, 6, 20),
        posted_price=Decimal("2.50"),
        posted_at=POSTED_AT,
    )
    out = signal_to_dict(signal)
    assert out["ticker"] == "AAPL"
    assert out["option_type"] == "call"
    assert out["strike"] == "185.00"
    assert out["posted_price"] == "2.50"
    assert out["expiration"] == "2026-06-20"
    assert out["posted_at"] == POSTED_AT.isoformat()


# ---- Accuracy meta-test: ≥90% on the synthetic suite above ----

def test_overall_accuracy_meets_threshold() -> None:
    """Sanity check that the synthetic suite would clear the 90% bar.

    Counts every parametric case above as one trial. This isn't a real
    accuracy measurement against live posts — it's a smoke gate to ensure
    the suite stays comprehensive over time.
    """
    # Re-run a representative subset programmatically so a regression in
    # parser logic shows up here even if someone refactors named tests.
    cases: list[tuple[str, bool]] = [
        # (raw_response, expect_signal)
        ('{"ticker":"AAPL","option_type":"call","strike":"185","expiration":"2026-06-20","entry_price":"2.50"}', True),
        ('{"ticker":"TSLA","option_type":"put","strike":"230","expiration":"2026-06-06","entry_price":"4.20"}', True),
        ('{"ticker":"$SPY","option_type":"call","strike":"450","expiration":"2026-05-20","entry_price":"1.85"}', True),
        ("null", False),
        ('{"ticker":"AAPL","option_type":"call","expiration":"2026-06-20","entry_price":"2.50"}', False),  # missing strike
        ('{"ticker":"AAPL","option_type":"straddle","strike":"185","expiration":"2026-06-20","entry_price":"2.50"}', False),
        ('{"ticker":"AAPL","option_type":"call","strike":"0","expiration":"2026-06-20","entry_price":"2.50"}', False),
        ('not json', False),
        ('', False),
        ('{"ticker":"AAPL","option_type":"call","strike":"185","expiration":"next-friday","entry_price":"2.50"}', False),
    ]

    correct = 0
    for raw, expect_signal in cases:
        result = parse_post("post", POSTED_AT, _client(raw))
        if (result.signal is not None) == expect_signal:
            correct += 1

    accuracy = correct / len(cases)
    assert accuracy >= 0.90, f"parser accuracy {accuracy:.0%} below 90% threshold"
