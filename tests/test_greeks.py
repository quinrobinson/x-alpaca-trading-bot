"""Unit tests for the Black-Scholes Greeks module. Pure math, no I/O."""

from __future__ import annotations

import math

import pytest

from x_alpaca_trading_bot import greeks


# ---- Pricing --------------------------------------------------------------

def test_bs_price_matches_textbook_atm_call() -> None:
    """Standard reference: S=K=100, T=1y, r=5%, sigma=20% -> call ~= 10.45."""
    price = greeks.bs_price(100.0, 100.0, 1.0, 0.05, 0.20, is_call=True)
    assert price == pytest.approx(10.45, abs=0.02)


def test_bs_price_put_call_parity() -> None:
    """C - P == S - K*exp(-rT) must hold for any inputs."""
    S, K, T, r, sigma = 105.0, 100.0, 0.5, 0.04, 0.30
    call = greeks.bs_price(S, K, T, r, sigma, is_call=True)
    put = greeks.bs_price(S, K, T, r, sigma, is_call=False)
    assert (call - put) == pytest.approx(S - K * math.exp(-r * T), abs=1e-6)


# ---- Implied volatility ---------------------------------------------------

def test_implied_volatility_round_trips() -> None:
    """Price an option at a known sigma, then solve IV back — must recover it."""
    S, K, T, r, true_sigma = 100.0, 105.0, 0.25, 0.04, 0.35
    market = greeks.bs_price(S, K, T, r, true_sigma, is_call=True)
    solved = greeks.implied_volatility(market, S, K, T, r, is_call=True)
    assert solved is not None
    assert solved == pytest.approx(true_sigma, abs=1e-4)


def test_implied_volatility_rejects_price_below_intrinsic() -> None:
    """A call priced under its intrinsic value has no real IV."""
    # S=120, K=100 -> intrinsic 20. A price of 15 is impossible.
    assert greeks.implied_volatility(15.0, 120.0, 100.0, 1.0, 0.04, is_call=True) is None


def test_implied_volatility_rejects_expired() -> None:
    assert greeks.implied_volatility(5.0, 100.0, 100.0, 0.0, 0.04, is_call=True) is None


# ---- Full Greek computation ----------------------------------------------

def test_compute_atm_call_has_sensible_greeks() -> None:
    """An at-the-money call: delta near 0.5, gamma/vega positive, theta negative."""
    # Price an ATM call ourselves so the inputs are internally consistent.
    S, K, dte = 100.0, 100.0, 30.0
    T = dte / 365.0
    market = greeks.bs_price(S, K, T, 0.04, 0.40, is_call=True)
    res = greeks.compute(
        spot=S, strike=K, dte_days=dte, option_price=market, is_call=True,
    )
    assert res is not None
    assert 0.45 < res.delta < 0.62        # ATM call delta sits a bit above 0.5
    assert res.gamma > 0
    assert res.vega > 0
    assert res.theta < 0                  # long option bleeds time value
    assert res.iv == pytest.approx(0.40, abs=1e-3)


def test_compute_deep_itm_call_delta_near_one() -> None:
    S, K, dte = 150.0, 100.0, 30.0
    T = dte / 365.0
    market = greeks.bs_price(S, K, T, 0.04, 0.35, is_call=True)
    res = greeks.compute(spot=S, strike=K, dte_days=dte, option_price=market, is_call=True)
    assert res is not None
    assert res.delta > 0.95


def test_compute_deep_otm_call_delta_near_zero() -> None:
    S, K, dte = 60.0, 100.0, 30.0
    T = dte / 365.0
    market = greeks.bs_price(S, K, T, 0.04, 0.35, is_call=True)
    res = greeks.compute(spot=S, strike=K, dte_days=dte, option_price=market, is_call=True)
    assert res is not None
    assert res.delta < 0.05


def test_compute_put_delta_is_negative() -> None:
    S, K, dte = 100.0, 100.0, 30.0
    T = dte / 365.0
    market = greeks.bs_price(S, K, T, 0.04, 0.40, is_call=False)
    res = greeks.compute(spot=S, strike=K, dte_days=dte, option_price=market, is_call=False)
    assert res is not None
    assert -0.62 < res.delta < -0.40


def test_compute_returns_none_when_expired() -> None:
    assert greeks.compute(
        spot=100.0, strike=100.0, dte_days=0.0, option_price=5.0, is_call=True,
    ) is None


def test_compute_returns_none_on_garbage_price() -> None:
    # Price below intrinsic -> solver can't find an IV.
    assert greeks.compute(
        spot=150.0, strike=100.0, dte_days=30.0, option_price=10.0, is_call=True,
    ) is None
