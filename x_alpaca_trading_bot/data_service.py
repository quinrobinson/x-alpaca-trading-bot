"""data_service — Phase 3.

Public surface (per X_ALPACA_OPTIONS_HANDOFF.md §2.2):
    is_market_open() -> bool
    get_option_quote(ticker, expiration, option_type, strike) -> Quote | None
    get_greeks(contract_symbol) -> Greeks
    get_iv_data(contract_symbol) -> IVData
    get_indicators(ticker, now) -> Indicators
    get_market_context(now) -> MarketContext

The DataService class wraps Alpaca (quotes, clock, stock bars) and Polygon
(option Greeks, IV). It implements `MarketDataProvider` so callers — chiefly
validator.py and the future indicator snapshot scheduler — can depend on the
Protocol and use a fake in unit tests.

All money values are `Decimal`. All timestamps are timezone-aware (UTC).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Literal, Protocol

logger = logging.getLogger(__name__)

OptionType = Literal["call", "put"]

# 11 SPDR sector ETFs covered in get_market_context.
SECTOR_ETFS: tuple[str, ...] = (
    "XLK", "XLF", "XLE", "XLY", "XLP", "XLV",
    "XLI", "XLB", "XLU", "XLRE", "XLC",
)


# ---- Data classes ----------------------------------------------------------

@dataclass(frozen=True)
class Quote:
    """Live option quote, all Decimals."""

    bid: Decimal
    ask: Decimal
    mid: Decimal
    spread_pct: Decimal  # (ask - bid) / mid; 1.0 == 100%
    ts: datetime


@dataclass(frozen=True)
class Greeks:
    delta: Decimal | None
    gamma: Decimal | None
    theta: Decimal | None
    vega: Decimal | None


@dataclass(frozen=True)
class IVData:
    iv: Decimal | None
    iv_rank: Decimal | None         # 0-100; None until we have ≥1y of IV history
    iv_percentile: Decimal | None   # 0-100; same


@dataclass(frozen=True)
class Indicators:
    rsi_14: Decimal | None
    macd: Decimal | None
    macd_signal: Decimal | None
    vwap: Decimal | None
    ema_9: Decimal | None
    ema_21: Decimal | None
    atr_14: Decimal | None
    bb_position: Decimal | None     # 0-1; position of close within Bollinger bands


@dataclass(frozen=True)
class MarketContext:
    vix: Decimal | None
    spy_vs_ema21: Literal["above", "below"] | None
    qqq_vs_ema21: Literal["above", "below"] | None
    sector_etf_trend: dict[str, Decimal]   # ticker -> day-change pct (e.g. 0.0123 == +1.23%)


# ---- Protocol so consumers can mock --------------------------------------

class MarketDataProvider(Protocol):
    def is_market_open(self) -> bool: ...
    def get_option_quote(
        self,
        ticker: str,
        expiration: date,
        option_type: OptionType,
        strike: Decimal,
    ) -> Quote | None: ...
    def get_greeks(self, contract_symbol: str) -> Greeks: ...
    def get_iv_data(self, contract_symbol: str) -> IVData: ...
    def get_indicators(self, ticker: str, now: datetime) -> Indicators: ...
    def get_market_context(self, now: datetime) -> MarketContext: ...
    def get_underlying_price(self, ticker: str) -> Decimal | None: ...


# ---- OCC contract symbol -----------------------------------------------------

def build_occ_symbol(
    ticker: str,
    expiration: date,
    option_type: OptionType,
    strike: Decimal,
) -> str:
    """Build an OCC option symbol, e.g. 'AAPL250620C00185000'.

    Layout: {root}{YYMMDD}{C|P}{strike * 1000 zero-padded to 8 digits}.
    """
    yymmdd = expiration.strftime("%y%m%d")
    cp = "C" if option_type == "call" else "P"
    strike_milli = int((strike * Decimal(1000)).to_integral_value())
    return f"{ticker.upper()}{yymmdd}{cp}{strike_milli:08d}"


def polygon_option_ticker(occ_symbol: str) -> str:
    """Polygon's snapshot endpoints expect 'O:AAPL250620C00185000'."""
    return f"O:{occ_symbol}"


# ---- Real DataService implementation ---------------------------------------
# Heavy lifting (Alpaca, Polygon, pandas-ta) is imported lazily inside __init__
# so that this module is cheap to import for unit tests that only need the
# dataclasses and Protocol.

class DataService:
    """Aggregates Alpaca + Polygon. Concrete impl behind the Protocol."""

    def __init__(
        self,
        *,
        alpaca_api_key: str,
        alpaca_secret_key: str,
        alpaca_base_url: str,
        polygon_api_key: str,
        http_timeout_seconds: float = 5.0,
    ) -> None:
        # Lazy imports so tests can patch or skip without paying these costs.
        import httpx
        from alpaca.data.historical.option import OptionHistoricalDataClient
        from alpaca.data.historical.stock import StockHistoricalDataClient
        from alpaca.trading.client import TradingClient

        self._alpaca_options = OptionHistoricalDataClient(
            api_key=alpaca_api_key,
            secret_key=alpaca_secret_key,
        )
        self._alpaca_stocks = StockHistoricalDataClient(
            api_key=alpaca_api_key,
            secret_key=alpaca_secret_key,
        )
        self._alpaca_trading = TradingClient(
            api_key=alpaca_api_key,
            secret_key=alpaca_secret_key,
            paper="paper-api" in alpaca_base_url,
        )
        self._polygon_key = polygon_api_key
        self._http = httpx.Client(
            base_url="https://api.polygon.io",
            timeout=http_timeout_seconds,
            params={"apiKey": polygon_api_key},
        )

    # -- Market clock --

    def is_market_open(self) -> bool:
        try:
            clock = self._alpaca_trading.get_clock()
            return bool(getattr(clock, "is_open", False))
        except Exception as exc:  # noqa: BLE001
            logger.warning("is_market_open failed: %s", exc)
            return False

    # -- Option quote --

    def get_option_quote(
        self,
        ticker: str,
        expiration: date,
        option_type: OptionType,
        strike: Decimal,
    ) -> Quote | None:
        from alpaca.data.requests import OptionLatestQuoteRequest

        symbol = build_occ_symbol(ticker, expiration, option_type, strike)
        try:
            req = OptionLatestQuoteRequest(symbol_or_symbols=symbol)
            resp = self._alpaca_options.get_option_latest_quote(req)
            q = resp.get(symbol) if isinstance(resp, dict) else None
            if q is None:
                logger.info("No quote for %s", symbol)
                return None
            bid = Decimal(str(q.bid_price))
            ask = Decimal(str(q.ask_price))
            if bid <= 0 or ask <= 0 or ask < bid:
                logger.info("Stale/invalid quote for %s: bid=%s ask=%s", symbol, bid, ask)
                return None
            mid = (bid + ask) / Decimal(2)
            spread_pct = (ask - bid) / mid
            ts = getattr(q, "timestamp", None) or datetime.now(timezone.utc)
            return Quote(bid=bid, ask=ask, mid=mid, spread_pct=spread_pct, ts=ts)
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_option_quote(%s) failed: %s", symbol, exc)
            return None

    # -- Greeks + IV --

    def get_greeks(self, contract_symbol: str) -> Greeks:
        snap = self._polygon_option_snapshot(contract_symbol)
        if snap is None:
            return Greeks(delta=None, gamma=None, theta=None, vega=None)
        greeks = snap.get("greeks") or {}
        return Greeks(
            delta=_to_decimal(greeks.get("delta")),
            gamma=_to_decimal(greeks.get("gamma")),
            theta=_to_decimal(greeks.get("theta")),
            vega=_to_decimal(greeks.get("vega")),
        )

    def get_iv_data(self, contract_symbol: str) -> IVData:
        snap = self._polygon_option_snapshot(contract_symbol)
        if snap is None:
            return IVData(iv=None, iv_rank=None, iv_percentile=None)
        return IVData(
            iv=_to_decimal(snap.get("implied_volatility")),
            iv_rank=None,        # populated once we build a 252-day IV history table
            iv_percentile=None,  # ditto
        )

    def _polygon_option_snapshot(self, contract_symbol: str) -> dict[str, Any] | None:
        underlying = _underlying_from_occ(contract_symbol)
        polygon_ticker = polygon_option_ticker(contract_symbol)
        try:
            r = self._http.get(f"/v3/snapshot/options/{underlying}/{polygon_ticker}")
            r.raise_for_status()
            body = r.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("polygon snapshot %s failed: %s", contract_symbol, exc)
            return None
        return body.get("results") or None

    # -- Indicators --

    def get_indicators(self, ticker: str, now: datetime) -> Indicators:
        intraday = self._fetch_alpaca_bars(ticker, now, timeframe_minutes=5, lookback_minutes=60 * 7)
        daily = self._fetch_alpaca_bars(ticker, now, timeframe_minutes=None, lookback_days=60)
        return _compute_indicators(intraday, daily)

    # -- Market context --

    def get_market_context(self, now: datetime) -> MarketContext:
        vix = self._fetch_vix(now)
        sectors = self._fetch_sector_changes(now)
        spy = self._trend_vs_ema21("SPY", now)
        qqq = self._trend_vs_ema21("QQQ", now)
        return MarketContext(
            vix=vix,
            spy_vs_ema21=spy,
            qqq_vs_ema21=qqq,
            sector_etf_trend=sectors,
        )

    # -- Internal helpers --

    def _fetch_alpaca_bars(
        self,
        ticker: str,
        now: datetime,
        *,
        timeframe_minutes: int | None,
        lookback_minutes: int | None = None,
        lookback_days: int | None = None,
    ) -> "Any":
        """Returns a pandas DataFrame indexed by timestamp with OHLCV columns,
        or an empty DataFrame on failure.

        Uses the IEX feed by default since the SIP feed requires a paid Alpaca
        market-data subscription. IEX data is real-time and free but covers a
        smaller share of total volume — fine for indicator math.
        """
        import pandas as pd
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

        if timeframe_minutes is None:
            tf = TimeFrame.Day
            start = now - timedelta(days=lookback_days or 60)
        else:
            tf = TimeFrame(timeframe_minutes, TimeFrameUnit.Minute)
            start = now - timedelta(minutes=lookback_minutes or 60 * 7)

        try:
            req = StockBarsRequest(
                symbol_or_symbols=ticker,
                timeframe=tf,
                start=start,
                end=now,
                feed=DataFeed.IEX,
            )
            resp = self._alpaca_stocks.get_stock_bars(req)
            df = resp.df  # MultiIndex (symbol, timestamp)
            if df is None or df.empty:
                return pd.DataFrame()
            # Drop the symbol level so callers see a single-ticker frame.
            return df.xs(ticker, level=0) if ticker in df.index.get_level_values(0) else df
        except Exception as exc:  # noqa: BLE001
            logger.warning("alpaca bars %s failed: %s", ticker, exc)
            return pd.DataFrame()

    def get_underlying_price(self, ticker: str) -> Decimal | None:
        """Latest mid-quote for the underlying stock. None on failure."""
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockLatestQuoteRequest
        try:
            req = StockLatestQuoteRequest(symbol_or_symbols=ticker, feed=DataFeed.IEX)
            resp = self._alpaca_stocks.get_stock_latest_quote(req)
            q = resp.get(ticker) if isinstance(resp, dict) else None
            if q is None:
                return None
            bid = Decimal(str(q.bid_price)) if getattr(q, "bid_price", 0) else None
            ask = Decimal(str(q.ask_price)) if getattr(q, "ask_price", 0) else None
            if bid and ask:
                return (bid + ask) / Decimal(2)
            return bid or ask
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_underlying_price(%s) failed: %s", ticker, exc)
            return None

    def _fetch_vix(self, now: datetime) -> Decimal | None:
        """Best-effort VIX fetch from Polygon. Returns None on failure."""
        try:
            r = self._http.get("/v2/aggs/ticker/I:VIX/prev")
            r.raise_for_status()
            results = r.json().get("results") or []
            if not results:
                return None
            return _to_decimal(results[0].get("c"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("VIX fetch failed: %s", exc)
            return None

    def _fetch_sector_changes(self, now: datetime) -> dict[str, Decimal]:
        """Fetch today's % change for every SPDR sector ETF in one batched call."""
        import pandas as pd
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        out: dict[str, Decimal] = {}
        start = now - timedelta(days=4)  # weekend cushion
        try:
            req = StockBarsRequest(
                symbol_or_symbols=list(SECTOR_ETFS),
                timeframe=TimeFrame.Day,
                start=start,
                end=now,
                feed=DataFeed.IEX,
            )
            resp = self._alpaca_stocks.get_stock_bars(req)
            df = resp.df
            if df is None or df.empty:
                return out
            for sym in SECTOR_ETFS:
                if sym not in df.index.get_level_values(0):
                    continue
                rows = df.xs(sym, level=0)
                if len(rows) < 2:
                    continue
                today_close = Decimal(str(rows["close"].iloc[-1]))
                prev_close = Decimal(str(rows["close"].iloc[-2]))
                if prev_close > 0:
                    out[sym] = (today_close - prev_close) / prev_close
        except Exception as exc:  # noqa: BLE001
            logger.warning("sector change fetch failed: %s", exc)
        return out

    def _trend_vs_ema21(self, ticker: str, now: datetime) -> Literal["above", "below"] | None:
        df = self._fetch_alpaca_bars(ticker, now, timeframe_minutes=None, lookback_days=60)
        if df is None or len(df) < 21:
            return None
        import pandas_ta_classic as ta
        ema21 = ta.ema(df["close"], length=21)
        if ema21 is None or ema21.isna().all():
            return None
        last_close = float(df["close"].iloc[-1])
        last_ema = float(ema21.dropna().iloc[-1])
        return "above" if last_close >= last_ema else "below"


# ---- Pure helpers (no I/O) -------------------------------------------------

def _to_decimal(x: Any) -> Decimal | None:
    if x is None:
        return None
    try:
        return Decimal(str(x))
    except Exception:  # noqa: BLE001
        return None


def _underlying_from_occ(occ_symbol: str) -> str:
    """Strip the date+type+strike tail off an OCC symbol to get the underlying.

    Assumes a 6-digit date + 1-char type + 8-digit strike = 15 trailing chars.
    """
    if len(occ_symbol) < 16:
        return occ_symbol
    return occ_symbol[:-15]


def _compute_indicators(intraday_df: "Any", daily_df: "Any") -> Indicators:
    """Compute the indicator suite from OHLCV bars using pandas-ta.

    Returns Indicators with None fields when input is insufficient.
    """
    import pandas as pd
    import pandas_ta_classic as ta

    def last_decimal(series) -> Decimal | None:
        try:
            if series is None:
                return None
            series = series.dropna()
            if series.empty:
                return None
            return Decimal(str(series.iloc[-1]))
        except Exception:  # noqa: BLE001
            return None

    rsi = macd_line = macd_signal = vwap = ema9 = ema21 = bb_pos = None
    atr_14 = None

    if isinstance(intraday_df, pd.DataFrame) and not intraday_df.empty:
        close = intraday_df["close"]
        rsi = last_decimal(ta.rsi(close, length=14))
        macd_df = ta.macd(close)
        if macd_df is not None and not macd_df.empty:
            macd_line = last_decimal(macd_df.filter(regex="^MACD_").iloc[:, 0])
            macd_signal = last_decimal(macd_df.filter(regex="^MACDs_").iloc[:, 0])
        ema9 = last_decimal(ta.ema(close, length=9))
        ema21 = last_decimal(ta.ema(close, length=21))

        if all(c in intraday_df.columns for c in ("high", "low", "close", "volume")):
            try:
                vwap = last_decimal(
                    ta.vwap(
                        high=intraday_df["high"],
                        low=intraday_df["low"],
                        close=intraday_df["close"],
                        volume=intraday_df["volume"],
                    )
                )
            except Exception:  # noqa: BLE001
                vwap = None

        bb = ta.bbands(close, length=20, std=2)
        if bb is not None and not bb.empty:
            try:
                lower = bb.filter(regex="^BBL_").iloc[:, 0].dropna()
                upper = bb.filter(regex="^BBU_").iloc[:, 0].dropna()
                if not lower.empty and not upper.empty:
                    last_close = float(close.iloc[-1])
                    l = float(lower.iloc[-1])
                    u = float(upper.iloc[-1])
                    if u > l:
                        pos = (last_close - l) / (u - l)
                        bb_pos = Decimal(str(round(max(0.0, min(1.0, pos)), 4)))
            except Exception:  # noqa: BLE001
                bb_pos = None

    if isinstance(daily_df, pd.DataFrame) and not daily_df.empty:
        if all(c in daily_df.columns for c in ("high", "low", "close")):
            atr_14 = last_decimal(
                ta.atr(
                    high=daily_df["high"],
                    low=daily_df["low"],
                    close=daily_df["close"],
                    length=14,
                )
            )

    return Indicators(
        rsi_14=rsi,
        macd=macd_line,
        macd_signal=macd_signal,
        vwap=vwap,
        ema_9=ema9,
        ema_21=ema21,
        atr_14=atr_14,
        bb_position=bb_pos,
    )
