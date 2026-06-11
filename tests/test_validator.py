"""Tests for validator — Phase 3 acceptance gate.

Every validation gate has at least one pass and one fail case. A fake
MarketDataProvider lets us script the data layer deterministically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from x_alpaca_trading_bot.data_service import (
    Greeks,
    Indicators,
    IVData,
    MarketContext,
    Quote,
    build_occ_symbol,
)
from x_alpaca_trading_bot.parser import Signal
from x_alpaca_trading_bot.validator import (
    DEFAULT_MAX_SPREAD_PCT,
    ValidationResult,
    gate_results_to_dict,
    validate,
)

NOW = datetime(2026, 5, 12, 14, 30, tzinfo=timezone.utc)
POSTED_AT = NOW - timedelta(seconds=30)


def _signal(
    *,
    ticker: str = "AAPL",
    option_type: str = "call",
    strike: Decimal = Decimal("185.00"),
    expiration: date = date(2026, 6, 20),
    posted_price: Decimal = Decimal("2.50"),
    posted_at: datetime = POSTED_AT,
) -> Signal:
    return Signal(
        ticker=ticker,
        option_type=option_type,  # type: ignore[arg-type]
        strike=strike,
        expiration=expiration,
        posted_price=posted_price,
        posted_at=posted_at,
    )


@dataclass
class FakeProvider:
    """Configurable stand-in for the real DataService."""

    market_open: bool = True
    quote: Quote | None = field(default=None)

    def is_market_open(self) -> bool:
        return self.market_open

    def get_option_quote(self, ticker, expiration, option_type, strike) -> Quote | None:
        return self.quote

    # The remaining methods aren't exercised by validator but must satisfy
    # the Protocol for callers in other tests.
    def get_greeks(self, contract_symbol: str) -> Greeks:
        return Greeks(delta=None, gamma=None, theta=None, vega=None)

    def get_iv_data(self, contract_symbol: str) -> IVData:
        return IVData(iv=None, iv_rank=None, iv_percentile=None)

    def get_indicators(self, ticker, now) -> Indicators:
        return Indicators(
            rsi_14=None, macd=None, macd_signal=None, vwap=None,
            ema_9=None, ema_21=None, atr_14=None, bb_position=None,
        )

    def get_market_context(self, now) -> MarketContext:
        return MarketContext(
            vix=None, spy_vs_ema21=None, qqq_vs_ema21=None, sector_etf_trend={},
        )

    def get_underlying_price(self, ticker: str) -> Decimal | None:
        return None


def _good_quote(*, ask: Decimal = Decimal("2.55"), bid: Decimal = Decimal("2.45")) -> Quote:
    mid = (ask + bid) / Decimal(2)
    return Quote(
        bid=bid,
        ask=ask,
        mid=mid,
        spread_pct=(ask - bid) / mid,
        ts=NOW,
    )


# ---- Happy path ----

def test_all_gates_pass() -> None:
    provider = FakeProvider(market_open=True, quote=_good_quote())
    result = validate(
        _signal(),
        provider,
        NOW,
        signal_stale_seconds=180,
        price_deviation_pct=Decimal("0.10"),
    )
    assert isinstance(result, ValidationResult)
    assert result.accepted is True
    assert result.rejection_reason is None
    assert [g.name for g in result.gate_results] == [
        "time_age", "market_open", "contract_exists", "spread", "price_deviation",
    ]
    assert all(g.passed for g in result.gate_results)
    assert result.live_ask == Decimal("2.55")


# ---- time_age ----

def test_time_age_stale_post_rejected() -> None:
    stale_signal = _signal(posted_at=NOW - timedelta(seconds=500))
    provider = FakeProvider(market_open=True, quote=_good_quote())
    result = validate(stale_signal, provider, NOW, signal_stale_seconds=180, price_deviation_pct=Decimal("0.10"))
    assert result.accepted is False
    assert result.rejection_reason == "time_age"
    time_gate = next(g for g in result.gate_results if g.name == "time_age")
    assert time_gate.passed is False
    assert "age" in (time_gate.reason or "")


def test_time_age_at_boundary_passes() -> None:
    signal_at_boundary = _signal(posted_at=NOW - timedelta(seconds=180))
    provider = FakeProvider(market_open=True, quote=_good_quote())
    result = validate(signal_at_boundary, provider, NOW, signal_stale_seconds=180, price_deviation_pct=Decimal("0.10"))
    time_gate = next(g for g in result.gate_results if g.name == "time_age")
    assert time_gate.passed is True


# ---- market_open ----

def test_market_closed_rejects() -> None:
    provider = FakeProvider(market_open=False, quote=_good_quote())
    result = validate(_signal(), provider, NOW, signal_stale_seconds=180, price_deviation_pct=Decimal("0.10"))
    assert result.accepted is False
    market_gate = next(g for g in result.gate_results if g.name == "market_open")
    assert market_gate.passed is False


# ---- contract_exists + cascading ----

def test_no_quote_marks_contract_spread_and_price_failed_with_reason() -> None:
    provider = FakeProvider(market_open=True, quote=None)
    result = validate(_signal(), provider, NOW, signal_stale_seconds=180, price_deviation_pct=Decimal("0.10"))
    assert result.accepted is False
    contract_gate = next(g for g in result.gate_results if g.name == "contract_exists")
    spread_gate = next(g for g in result.gate_results if g.name == "spread")
    price_gate = next(g for g in result.gate_results if g.name == "price_deviation")
    assert contract_gate.passed is False
    assert spread_gate.passed is False and spread_gate.reason == "no_quote"
    assert price_gate.passed is False and price_gate.reason == "no_quote"
    assert result.live_ask is None


# ---- spread ----

def test_wide_spread_rejected() -> None:
    # spread 20% > 10% default
    wide = _good_quote(bid=Decimal("2.25"), ask=Decimal("2.75"))
    assert wide.spread_pct > DEFAULT_MAX_SPREAD_PCT
    provider = FakeProvider(market_open=True, quote=wide)
    result = validate(_signal(), provider, NOW, signal_stale_seconds=180, price_deviation_pct=Decimal("0.10"))
    spread_gate = next(g for g in result.gate_results if g.name == "spread")
    assert spread_gate.passed is False
    assert "spread" in (spread_gate.reason or "")


def test_tight_spread_passes() -> None:
    tight = _good_quote(bid=Decimal("2.48"), ask=Decimal("2.52"))  # ~1.6%
    provider = FakeProvider(market_open=True, quote=tight)
    result = validate(_signal(), provider, NOW, signal_stale_seconds=180, price_deviation_pct=Decimal("0.10"))
    spread_gate = next(g for g in result.gate_results if g.name == "spread")
    assert spread_gate.passed is True


# ---- price_deviation ----

def test_price_deviation_too_wide_rejected() -> None:
    # posted 2.50, live ask 3.00 = 20% above
    quote = _good_quote(bid=Decimal("2.95"), ask=Decimal("3.00"))
    provider = FakeProvider(market_open=True, quote=quote)
    result = validate(_signal(posted_price=Decimal("2.50")), provider, NOW, signal_stale_seconds=180, price_deviation_pct=Decimal("0.10"))
    pd_gate = next(g for g in result.gate_results if g.name == "price_deviation")
    assert pd_gate.passed is False


def test_price_deviation_within_tolerance_passes() -> None:
    quote = _good_quote(bid=Decimal("2.55"), ask=Decimal("2.60"))  # 4% above 2.50
    provider = FakeProvider(market_open=True, quote=quote)
    result = validate(_signal(posted_price=Decimal("2.50")), provider, NOW, signal_stale_seconds=180, price_deviation_pct=Decimal("0.10"))
    pd_gate = next(g for g in result.gate_results if g.name == "price_deviation")
    assert pd_gate.passed is True


# ---- ValidationResult metadata ----

def test_rejection_reason_is_first_failing_gate() -> None:
    # market closed AND stale — time_age comes first in order
    stale = _signal(posted_at=NOW - timedelta(seconds=500))
    provider = FakeProvider(market_open=False, quote=_good_quote())
    result = validate(stale, provider, NOW, signal_stale_seconds=180, price_deviation_pct=Decimal("0.10"))
    assert result.rejection_reason == "time_age"


def test_elapsed_seconds_recorded() -> None:
    provider = FakeProvider(market_open=True, quote=_good_quote())
    result = validate(_signal(), provider, NOW, signal_stale_seconds=180, price_deviation_pct=Decimal("0.10"))
    assert result.elapsed_seconds >= 0
    # Fake provider is in-process; validation must be near-instant.
    assert result.elapsed_seconds < 0.1


# ---- Serialization ----

def test_gate_results_to_dict_round_trip_friendly() -> None:
    provider = FakeProvider(market_open=True, quote=_good_quote())
    result = validate(_signal(), provider, NOW, signal_stale_seconds=180, price_deviation_pct=Decimal("0.10"))
    payload = gate_results_to_dict(result)
    assert payload["accepted"] is True
    assert payload["live_ask"] == "2.55"
    assert len(payload["gates"]) == 5
    # Every Decimal measurement is a string in the serialized payload
    for gate in payload["gates"]:
        if gate["measured"] is not None and gate["name"] in ("spread", "price_deviation"):
            assert isinstance(gate["measured"], str)


# ---- entry_iv gate ----

@dataclass
class _ProviderWithSpot(FakeProvider):
    """Same as FakeProvider but returns a configurable underlying spot.

    Needed because the entry_iv gate calls provider.get_underlying_price to
    back-solve IV via Black-Scholes.
    """

    spot: Decimal | None = None

    def get_underlying_price(self, ticker: str) -> Decimal | None:
        return self.spot


def test_entry_iv_gate_absent_when_threshold_unset() -> None:
    """Default behaviour — five gates, no entry_iv."""
    provider = FakeProvider(market_open=True, quote=_good_quote())
    result = validate(
        _signal(),
        provider,
        NOW,
        signal_stale_seconds=180,
        price_deviation_pct=Decimal("0.10"),
    )
    assert len(result.gate_results) == 5
    assert all(g.name != "entry_iv" for g in result.gate_results)


def test_entry_iv_gate_present_when_threshold_set() -> None:
    """Gate appears as the 6th entry once max_entry_iv is configured."""
    provider = _ProviderWithSpot(
        market_open=True, quote=_good_quote(), spot=Decimal("185.00")
    )
    result = validate(
        _signal(),
        provider,
        NOW,
        signal_stale_seconds=180,
        price_deviation_pct=Decimal("0.10"),
        max_entry_iv=Decimal("1.50"),
    )
    assert len(result.gate_results) == 6
    assert result.gate_results[-1].name == "entry_iv"


def test_entry_iv_gate_rejects_when_iv_exceeds_threshold() -> None:
    """An ATM option priced at ~30% of spot implies very high IV — well above
    a 1.50 ceiling — so the gate must reject."""
    quote = Quote(
        bid=Decimal("55.00"),
        ask=Decimal("57.00"),
        mid=Decimal("56.00"),
        spread_pct=Decimal("0.036"),
        ts=NOW,
    )
    provider = _ProviderWithSpot(
        market_open=True, quote=quote, spot=Decimal("185.00")
    )
    result = validate(
        _signal(posted_price=Decimal("56.00")),
        provider,
        NOW,
        signal_stale_seconds=180,
        price_deviation_pct=Decimal("1.00"),  # generous so price_dev passes
        max_entry_iv=Decimal("1.50"),
    )
    iv_gate = next(g for g in result.gate_results if g.name == "entry_iv")
    assert iv_gate.passed is False
    assert result.rejection_reason == "entry_iv"


def test_entry_iv_gate_passes_when_iv_within_threshold() -> None:
    """A reasonably-priced ATM option (~3% of spot) implies a moderate IV
    that should sit well under a 1.50 ceiling."""
    provider = _ProviderWithSpot(
        market_open=True, quote=_good_quote(), spot=Decimal("185.00")
    )
    result = validate(
        _signal(),
        provider,
        NOW,
        signal_stale_seconds=180,
        price_deviation_pct=Decimal("0.10"),
        max_entry_iv=Decimal("1.50"),
    )
    iv_gate = next(g for g in result.gate_results if g.name == "entry_iv")
    assert iv_gate.passed is True
    assert result.accepted is True


def test_entry_iv_gate_fails_open_when_spot_unavailable() -> None:
    """If the underlying spot fetch returns None we can't solve IV — pass
    with reason 'iv_unavailable' rather than silently reject the signal."""
    # FakeProvider's get_underlying_price returns None, so the IV solver
    # returns None.
    provider = FakeProvider(market_open=True, quote=_good_quote())
    result = validate(
        _signal(),
        provider,
        NOW,
        signal_stale_seconds=180,
        price_deviation_pct=Decimal("0.10"),
        max_entry_iv=Decimal("1.50"),
    )
    iv_gate = next(g for g in result.gate_results if g.name == "entry_iv")
    assert iv_gate.passed is True
    assert iv_gate.reason == "iv_unavailable"


def test_entry_iv_gate_records_no_quote_when_contract_missing() -> None:
    """contract_exists fails -> entry_iv also flagged with reason='no_quote'
    so the gate_results sequence stays uniform for downstream analysis."""
    provider = _ProviderWithSpot(market_open=True, quote=None, spot=Decimal("185.00"))
    result = validate(
        _signal(),
        provider,
        NOW,
        signal_stale_seconds=180,
        price_deviation_pct=Decimal("0.10"),
        max_entry_iv=Decimal("1.50"),
    )
    iv_gate = next(g for g in result.gate_results if g.name == "entry_iv")
    assert iv_gate.passed is False
    assert iv_gate.reason == "no_quote"


# ---- OCC symbol helper (lives in data_service but exercised here too) ----

def test_build_occ_symbol_format() -> None:
    sym = build_occ_symbol("AAPL", date(2026, 6, 20), "call", Decimal("185.00"))
    assert sym == "AAPL260620C00185000"


def test_build_occ_symbol_put() -> None:
    sym = build_occ_symbol("TSLA", date(2026, 7, 18), "put", Decimal("230.50"))
    assert sym == "TSLA260718P00230500"
