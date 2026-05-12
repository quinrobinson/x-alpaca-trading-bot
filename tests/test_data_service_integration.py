"""Integration tests for data_service against real Alpaca + Polygon — Phase 3.

These hit live APIs. They skip cleanly when:
  - DATABASE_URL / Alpaca / Polygon creds are missing or placeholder values
  - Any test fails the credential check on import

The Phase 3 acceptance gate verified here:
  - "Integration test: fetch live quote, Greeks, and indicators for a real
     SPY option contract"
  - "Gate timing: full validation completes in <3 seconds"

Note: market data can change. We do NOT assert specific accept/reject outcomes
for validate() — we assert it returns a well-formed ValidationResult within
budget.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from dotenv import load_dotenv

from x_alpaca_trading_bot.data_service import (
    DataService,
    Indicators,
    MarketContext,
    Quote,
    build_occ_symbol,
)
from x_alpaca_trading_bot.parser import Signal
from x_alpaca_trading_bot.validator import validate


# ---- Skip rules -------------------------------------------------------------

REQUIRED_INTEGRATION_VARS = (
    "ALPACA_API_KEY",
    "ALPACA_SECRET_KEY",
    "ALPACA_BASE_URL",
    "POLYGON_API_KEY",
)


def _load_creds() -> dict[str, str] | None:
    load_dotenv(override=True)
    out: dict[str, str] = {}
    for name in REQUIRED_INTEGRATION_VARS:
        value = os.environ.get(name) or ""
        if not value or "PLACEHOLDER" in value:
            return None
        out[name] = value
    return out


@pytest.fixture(scope="module")
def service() -> Iterator[DataService]:
    creds = _load_creds()
    if creds is None:
        pytest.skip("Integration creds missing or placeholder; skipping live API tests")
    svc = DataService(
        alpaca_api_key=creds["ALPACA_API_KEY"],
        alpaca_secret_key=creds["ALPACA_SECRET_KEY"],
        alpaca_base_url=creds["ALPACA_BASE_URL"],
        polygon_api_key=creds["POLYGON_API_KEY"],
    )
    try:
        yield svc
    finally:
        # The httpx client is the only resource that benefits from closing.
        try:
            svc._http.close()  # type: ignore[attr-defined]
        except Exception:
            pass


# ---- SPY contract helper ----------------------------------------------------

def _next_friday(reference: date) -> date:
    """Find the next Friday >= reference. Standard weekly expiration day."""
    days_ahead = (4 - reference.weekday()) % 7  # Friday is weekday=4
    if days_ahead == 0:
        days_ahead = 7  # use next Friday if today is Friday
    return reference + timedelta(days=days_ahead)


def _build_atm_spy_signal(service: DataService) -> Signal:
    """Build a Signal for a near-ATM SPY call expiring next Friday.

    Approximates ATM with a strike rounded to the nearest $5 of recent close.
    """
    import pandas as pd
    df = service._fetch_alpaca_bars("SPY", datetime.now(timezone.utc), timeframe_minutes=None, lookback_days=5)
    if not isinstance(df, pd.DataFrame) or df.empty:
        pytest.skip("Could not fetch SPY daily bars to build a signal")
    last_close = float(df["close"].iloc[-1])
    strike = Decimal(str(round(last_close / 5.0) * 5))
    expiry = _next_friday(date.today() + timedelta(days=1))
    return Signal(
        ticker="SPY",
        option_type="call",
        strike=strike,
        expiration=expiry,
        posted_price=Decimal("1.00"),  # placeholder; price_deviation may fail and that's OK
        posted_at=datetime.now(timezone.utc) - timedelta(seconds=30),
    )


# ---- Live data smoke tests --------------------------------------------------

def test_is_market_open_returns_bool(service: DataService) -> None:
    """Whatever the answer, the call should complete and be a bool."""
    assert isinstance(service.is_market_open(), bool)


def test_get_option_quote_for_spy(service: DataService) -> None:
    signal = _build_atm_spy_signal(service)
    quote = service.get_option_quote(signal.ticker, signal.expiration, signal.option_type, signal.strike)
    # The contract may not exist on every paper account/data tier — accept None
    # but if it does come back, the shape must be sane.
    if quote is None:
        pytest.skip("No live quote for the chosen SPY contract — common after-hours")
    assert isinstance(quote, Quote)
    assert quote.bid > 0
    assert quote.ask >= quote.bid
    assert quote.mid > 0


def test_get_greeks_for_spy(service: DataService) -> None:
    signal = _build_atm_spy_signal(service)
    occ = build_occ_symbol(signal.ticker, signal.expiration, signal.option_type, signal.strike)
    greeks = service.get_greeks(occ)
    # Greeks may be None when contract is not in Polygon snapshot — just ensure
    # the call returns a Greeks dataclass without raising.
    assert hasattr(greeks, "delta")


def test_get_indicators_for_spy(service: DataService) -> None:
    inds = service.get_indicators("SPY", datetime.now(timezone.utc))
    assert isinstance(inds, Indicators)
    # If market data flowed at all, at least one indicator should be populated.
    assert any(v is not None for v in (
        inds.rsi_14, inds.ema_9, inds.ema_21, inds.vwap, inds.macd, inds.atr_14,
    ))


def test_get_market_context_shape(service: DataService) -> None:
    ctx = service.get_market_context(datetime.now(timezone.utc))
    assert isinstance(ctx, MarketContext)
    # sector_etf_trend is best-effort; may be empty if Alpaca returned nothing
    assert isinstance(ctx.sector_etf_trend, dict)


# ---- Phase 3 latency gate ---------------------------------------------------

def test_full_validation_under_three_seconds(service: DataService) -> None:
    """Phase 3 acceptance: validate() completes in <3 seconds end-to-end."""
    signal = _build_atm_spy_signal(service)
    start = time.perf_counter()
    result = validate(
        signal,
        service,
        datetime.now(timezone.utc),
        signal_stale_seconds=180,
        price_deviation_pct=Decimal("0.10"),
    )
    elapsed = time.perf_counter() - start
    assert elapsed < 3.0, f"validate took {elapsed:.2f}s — exceeds 3s gate"
    # Sanity-check the structure regardless of accept/reject outcome
    assert len(result.gate_results) == 5
    assert isinstance(result.accepted, bool)
