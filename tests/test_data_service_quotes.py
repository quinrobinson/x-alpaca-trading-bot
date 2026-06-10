"""Unit tests for the option quote source-of-truth.

Covers `get_option_quote` and its two underlying helpers:
    _polygon_option_quote — primary, NBBO via Polygon snapshot
    _alpaca_option_quote  — fallback when Polygon is unreachable

Built with stubbed providers — no live API calls. Skips Alpaca's
real `OptionHistoricalDataClient` so DataService instances don't
need real credentials.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal

from x_alpaca_trading_bot.data_service import DataService, Quote


# ---- helpers ---------------------------------------------------------------


def _bare_service() -> DataService:
    """Construct a DataService without running __init__.

    __init__ imports alpaca-py and instantiates clients that hit the
    network on auth. These tests stub the methods we care about, so the
    real clients are never used.
    """
    return DataService.__new__(DataService)


def _signal_args() -> tuple[str, date, str, Decimal]:
    return ("AAPL", date(2026, 6, 20), "call", Decimal("185"))


# ---- _polygon_option_quote -------------------------------------------------


def test_polygon_quote_parses_last_quote_into_decimal_quote() -> None:
    svc = _bare_service()
    svc._polygon_option_snapshot = lambda sym: {
        "last_quote": {
            "bid": 1.35,
            "ask": 1.40,
            "last_updated": 1_700_000_000_000_000_000,
        }
    }

    quote = svc._polygon_option_quote("AAPL260620C00185000")

    assert isinstance(quote, Quote)
    assert quote.bid == Decimal("1.35")
    assert quote.ask == Decimal("1.40")
    assert quote.mid == Decimal("1.375")
    # 0.05 / 1.375 ≈ 0.0364
    assert quote.spread_pct < Decimal("0.04")
    assert quote.ts == datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc)


def test_polygon_quote_returns_none_when_snapshot_empty() -> None:
    svc = _bare_service()
    svc._polygon_option_snapshot = lambda sym: None

    assert svc._polygon_option_quote("AAPL260620C00185000") is None


def test_polygon_quote_returns_none_when_bid_or_ask_missing() -> None:
    svc = _bare_service()
    svc._polygon_option_snapshot = lambda sym: {"last_quote": {"ask": 1.40}}

    assert svc._polygon_option_quote("AAPL260620C00185000") is None


def test_polygon_quote_returns_none_when_bid_exceeds_ask() -> None:
    svc = _bare_service()
    svc._polygon_option_snapshot = lambda sym: {
        "last_quote": {"bid": 1.50, "ask": 1.40, "last_updated": 0}
    }

    assert svc._polygon_option_quote("AAPL260620C00185000") is None


def test_polygon_quote_returns_none_on_zero_prices() -> None:
    svc = _bare_service()
    svc._polygon_option_snapshot = lambda sym: {
        "last_quote": {"bid": 0, "ask": 1.40, "last_updated": 0}
    }

    assert svc._polygon_option_quote("AAPL260620C00185000") is None


def test_polygon_quote_falls_back_to_now_when_timestamp_missing() -> None:
    svc = _bare_service()
    svc._polygon_option_snapshot = lambda sym: {
        "last_quote": {"bid": 1.35, "ask": 1.40}
    }
    before = datetime.now(timezone.utc)

    quote = svc._polygon_option_quote("AAPL260620C00185000")

    assert quote is not None
    assert quote.ts >= before


# ---- get_option_quote routing ---------------------------------------------


def test_get_option_quote_prefers_polygon_when_available() -> None:
    svc = _bare_service()
    polygon_quote = Quote(
        bid=Decimal("1.35"),
        ask=Decimal("1.40"),
        mid=Decimal("1.375"),
        spread_pct=Decimal("0.0364"),
        ts=datetime.now(timezone.utc),
    )
    svc._polygon_option_quote = lambda sym: polygon_quote

    alpaca_called = False

    def _alpaca_should_not_run(sym: str) -> Quote | None:
        nonlocal alpaca_called
        alpaca_called = True
        return None

    svc._alpaca_option_quote = _alpaca_should_not_run

    result = svc.get_option_quote(*_signal_args())

    assert result is polygon_quote
    assert alpaca_called is False


def test_get_option_quote_falls_back_to_alpaca_when_polygon_returns_none() -> None:
    svc = _bare_service()
    alpaca_quote = Quote(
        bid=Decimal("1.20"),
        ask=Decimal("1.60"),
        mid=Decimal("1.40"),
        spread_pct=Decimal("0.2857"),
        ts=datetime.now(timezone.utc),
    )
    svc._polygon_option_quote = lambda sym: None
    svc._alpaca_option_quote = lambda sym: alpaca_quote

    result = svc.get_option_quote(*_signal_args())

    assert result is alpaca_quote


def test_get_option_quote_returns_none_when_both_sources_fail() -> None:
    svc = _bare_service()
    svc._polygon_option_quote = lambda sym: None
    svc._alpaca_option_quote = lambda sym: None

    assert svc.get_option_quote(*_signal_args()) is None


# ---- _alpaca_option_quote (stale/invalid handling) ------------------------


@dataclass
class _FakeAlpacaQuote:
    bid_price: float
    ask_price: float
    timestamp: datetime | None = None


class _FakeAlpacaOptionsClient:
    def __init__(self, quote_by_symbol: dict[str, _FakeAlpacaQuote | None]) -> None:
        self._quotes = quote_by_symbol

    def get_option_latest_quote(self, req):
        sym = req.symbol_or_symbols
        if sym not in self._quotes:
            return {}
        return {sym: self._quotes[sym]}


def test_alpaca_quote_returns_none_when_response_missing_symbol() -> None:
    svc = _bare_service()
    svc._alpaca_options = _FakeAlpacaOptionsClient({})

    assert svc._alpaca_option_quote("AAPL260620C00185000") is None


def test_alpaca_quote_returns_none_when_bid_or_ask_invalid() -> None:
    svc = _bare_service()
    svc._alpaca_options = _FakeAlpacaOptionsClient(
        {"AAPL260620C00185000": _FakeAlpacaQuote(bid_price=0.0, ask_price=1.4)}
    )

    assert svc._alpaca_option_quote("AAPL260620C00185000") is None


def test_alpaca_quote_builds_quote_from_valid_fake() -> None:
    svc = _bare_service()
    ts = datetime(2026, 6, 10, 14, 30, tzinfo=timezone.utc)
    svc._alpaca_options = _FakeAlpacaOptionsClient(
        {
            "AAPL260620C00185000": _FakeAlpacaQuote(
                bid_price=1.35, ask_price=1.40, timestamp=ts
            )
        }
    )

    quote = svc._alpaca_option_quote("AAPL260620C00185000")

    assert quote is not None
    assert quote.bid == Decimal("1.35")
    assert quote.ask == Decimal("1.40")
    assert quote.ts == ts
