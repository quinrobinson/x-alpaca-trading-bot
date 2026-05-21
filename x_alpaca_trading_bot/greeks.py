"""Black-Scholes option Greeks + implied-volatility solver.

Pure math, no I/O. The bot computes delta/gamma/theta/vega/IV locally
from free real-time Alpaca quotes instead of paying for a Polygon data
tier that gates the snapshot endpoint.

US equity options are American-style; Black-Scholes is the European
model. For the short-dated contracts this bot trades the difference in
the Greeks is small, and these values are recorded for post-trade
analysis only — they gate no entry or exit decision. Black-Scholes is
the standard, well-understood approximation for that purpose.

All inputs are explicit (no wall-clock reads) so this module is fully
unit-testable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Annual risk-free rate — a static stand-in for the 3-month T-bill.
# Greeks are analysis-only, so a fixed rate near the prevailing level is
# plenty precise. Revisit only if rates move materially.
DEFAULT_RISK_FREE_RATE = 0.04

# Theta is conventionally quoted per calendar day.
_DAYS_PER_YEAR = 365.0


@dataclass(frozen=True)
class GreeksResult:
    """All values are plain floats — Greeks are sensitivities, not money."""

    delta: float   # ∂price/∂spot          (call: 0..1, put: -1..0)
    gamma: float   # ∂delta/∂spot
    theta: float   # ∂price/∂time, per calendar day (typically negative)
    vega: float    # ∂price/∂vol, per 1 percentage-point change in IV
    iv: float      # implied volatility, annualized (0.45 == 45%)


# ---- Normal distribution helpers ------------------------------------------

def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _d1_d2(S: float, K: float, T: float, r: float, sigma: float) -> tuple[float, float]:
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return d1, d2


# ---- Pricing + IV ---------------------------------------------------------

def bs_price(S: float, K: float, T: float, r: float, sigma: float, is_call: bool) -> float:
    """Black-Scholes fair value of a European option."""
    d1, d2 = _d1_d2(S, K, T, r, sigma)
    if is_call:
        return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
    return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def _raw_vega(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """∂price/∂sigma in raw (un-scaled) units — drives the IV solver."""
    d1, _ = _d1_d2(S, K, T, r, sigma)
    return S * _norm_pdf(d1) * math.sqrt(T)


def implied_volatility(
    market_price: float,
    S: float,
    K: float,
    T: float,
    r: float,
    is_call: bool,
    *,
    max_iter: int = 100,
    tol: float = 1e-6,
) -> float | None:
    """Newton-Raphson solve for the IV that reprices the option to market.

    Returns None when the inputs can't yield a sane IV — price below
    intrinsic value, price above the underlying, expired, or the
    iteration diverges.
    """
    if T <= 0 or S <= 0 or K <= 0 or market_price <= 0:
        return None
    intrinsic = max(0.0, (S - K) if is_call else (K - S))
    # A market price at/below intrinsic or above the spot has no real IV.
    if market_price <= intrinsic or market_price >= S:
        return None

    sigma = 0.5  # initial guess — 50% vol is a reasonable midpoint
    for _ in range(max_iter):
        price = bs_price(S, K, T, r, sigma, is_call)
        diff = price - market_price
        if abs(diff) < tol:
            return sigma
        v = _raw_vega(S, K, T, r, sigma)
        if v < 1e-8:
            return None  # vega collapsed — can't take another step
        sigma -= diff / v
        if sigma <= 0.0 or sigma > 10.0:
            return None  # diverged out of any plausible range
    return None  # didn't converge inside max_iter


def compute(
    *,
    spot: float,
    strike: float,
    dte_days: float,
    option_price: float,
    is_call: bool,
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
) -> GreeksResult | None:
    """Solve IV from the market option price, then return the full Greek set.

    `dte_days` is calendar days to expiration. Returns None on degenerate
    input (expired, non-positive spot/strike/price) or if the IV solver
    fails to converge.
    """
    S = float(spot)
    K = float(strike)
    T = float(dte_days) / _DAYS_PER_YEAR
    price = float(option_price)
    r = float(risk_free_rate)
    if S <= 0 or K <= 0 or T <= 0 or price <= 0:
        return None

    iv = implied_volatility(price, S, K, T, r, is_call)
    if iv is None:
        return None

    d1, d2 = _d1_d2(S, K, T, r, iv)
    pdf_d1 = _norm_pdf(d1)
    sqrt_t = math.sqrt(T)

    delta = _norm_cdf(d1) if is_call else _norm_cdf(d1) - 1.0
    gamma = pdf_d1 / (S * iv * sqrt_t)
    vega = S * pdf_d1 * sqrt_t / 100.0  # scaled to "per 1 vol point"

    decay = -(S * pdf_d1 * iv) / (2.0 * sqrt_t)
    if is_call:
        theta = (decay - r * K * math.exp(-r * T) * _norm_cdf(d2)) / _DAYS_PER_YEAR
    else:
        theta = (decay + r * K * math.exp(-r * T) * _norm_cdf(-d2)) / _DAYS_PER_YEAR

    return GreeksResult(delta=delta, gamma=gamma, theta=theta, vega=vega, iv=iv)
