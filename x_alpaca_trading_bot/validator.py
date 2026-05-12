"""validator — Phase 3.

Pure gate logic. Takes a `Signal`, a market-data provider, and tunables; runs
all gates; returns a `ValidationResult` listing each gate's outcome plus an
overall `accepted` boolean.

Gates (in order):
    1. time_age        — post age <= signal_stale_seconds
    2. market_open     — Alpaca clock reports open
    3. contract_exists — option quote returned valid bid/ask
    4. spread          — (ask - bid) / mid < max_spread_pct
    5. price_deviation — |live_ask - posted_price| / posted_price < price_deviation_pct

If `contract_exists` fails, `spread` and `price_deviation` are recorded as
not passed with reason="no_quote" — we still produce a complete gate_results
list so journaling and post-hoc analysis are uniform.

No I/O here. The provider is a Protocol; tests inject a fake.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from x_alpaca_trading_bot.data_service import MarketDataProvider, Quote
from x_alpaca_trading_bot.parser import Signal

logger = logging.getLogger(__name__)

DEFAULT_MAX_SPREAD_PCT = Decimal("0.10")


@dataclass(frozen=True)
class GateResult:
    name: str
    passed: bool
    reason: str | None = None
    measured: Any | None = None  # numeric measurement, when relevant


@dataclass(frozen=True)
class ValidationResult:
    signal: Signal
    accepted: bool
    gate_results: list[GateResult]
    live_ask: Decimal | None
    elapsed_seconds: float
    rejection_reason: str | None  # name of the first failing gate, or None


def validate(
    signal: Signal,
    provider: MarketDataProvider,
    now: datetime,
    *,
    signal_stale_seconds: int,
    price_deviation_pct: Decimal,
    max_spread_pct: Decimal = DEFAULT_MAX_SPREAD_PCT,
) -> ValidationResult:
    """Run the 5 market validation gates against a parsed Signal.

    `now` is injected (no `datetime.now()` here per spec §2.3 rule 3).
    """
    started = time.perf_counter()
    gates: list[GateResult] = []

    # ---- 1. time_age ----
    age = (now - signal.posted_at).total_seconds()
    gates.append(
        GateResult(
            name="time_age",
            passed=age <= signal_stale_seconds,
            measured=round(age, 2),
            reason=None if age <= signal_stale_seconds else f"age {age:.1f}s > {signal_stale_seconds}s",
        )
    )

    # ---- 2. market_open ----
    market_open = provider.is_market_open()
    gates.append(
        GateResult(
            name="market_open",
            passed=market_open,
            reason=None if market_open else "market is closed",
        )
    )

    # ---- 3. contract_exists ----
    quote: Quote | None = provider.get_option_quote(
        signal.ticker,
        signal.expiration,
        signal.option_type,
        signal.strike,
    )
    contract_ok = quote is not None
    gates.append(
        GateResult(
            name="contract_exists",
            passed=contract_ok,
            reason=None if contract_ok else "no_quote",
        )
    )

    # ---- 4. spread + 5. price_deviation ----
    live_ask: Decimal | None = None
    if quote is None:
        gates.append(GateResult(name="spread", passed=False, reason="no_quote"))
        gates.append(GateResult(name="price_deviation", passed=False, reason="no_quote"))
    else:
        live_ask = quote.ask
        gates.append(
            GateResult(
                name="spread",
                passed=quote.spread_pct < max_spread_pct,
                measured=quote.spread_pct,
                reason=(
                    None
                    if quote.spread_pct < max_spread_pct
                    else f"spread {quote.spread_pct:.2%} >= {max_spread_pct:.2%}"
                ),
            )
        )
        if signal.posted_price > 0:
            deviation = abs(quote.ask - signal.posted_price) / signal.posted_price
        else:
            deviation = Decimal("Infinity")
        gates.append(
            GateResult(
                name="price_deviation",
                passed=deviation < price_deviation_pct,
                measured=deviation,
                reason=(
                    None
                    if deviation < price_deviation_pct
                    else f"deviation {deviation:.2%} >= {price_deviation_pct:.2%}"
                ),
            )
        )

    accepted = all(g.passed for g in gates)
    rejection_reason = next((g.name for g in gates if not g.passed), None)

    elapsed = time.perf_counter() - started
    return ValidationResult(
        signal=signal,
        accepted=accepted,
        gate_results=gates,
        live_ask=live_ask,
        elapsed_seconds=elapsed,
        rejection_reason=rejection_reason,
    )


def gate_results_to_dict(result: ValidationResult) -> dict[str, Any]:
    """Serialize gate results for the signals.gate_results JSONB column."""
    return {
        "accepted": result.accepted,
        "rejection_reason": result.rejection_reason,
        "elapsed_seconds": round(result.elapsed_seconds, 4),
        "live_ask": str(result.live_ask) if result.live_ask is not None else None,
        "gates": [
            {
                "name": g.name,
                "passed": g.passed,
                "reason": g.reason,
                "measured": _coerce_measured(g.measured),
            }
            for g in result.gate_results
        ],
    }


def _coerce_measured(x: Any) -> Any:
    """JSON can't store Decimal; coerce gate measurements to str where needed."""
    if isinstance(x, Decimal):
        return str(x)
    return x
