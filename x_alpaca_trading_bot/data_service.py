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


@dataclass(frozen=True)
class OHLCBar:
    """One OHLCV bar for an underlying ticker. Decimal prices, int volume."""

    ts: datetime          # timezone-aware (UTC)
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


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


def parse_occ_symbol(occ_symbol: str) -> tuple[str, date, OptionType, Decimal]:
    """Inverse of build_occ_symbol.

    'AAPL260620C00185000' -> ('AAPL', date(2026, 6, 20), 'call', Decimal('185')).
    Trailing layout: 6-digit YYMMDD + 1-char C|P + 8-digit strike×1000.
    """
    from datetime import datetime as _dt

    strike_milli = int(occ_symbol[-8:])
    cp = occ_symbol[-9]
    yymmdd = occ_symbol[-15:-9]
    ticker = occ_symbol[:-15]
    expiration = _dt.strptime(yymmdd, "%y%m%d").date()
    option_type: OptionType = "call" if cp.upper() == "C" else "put"
    strike = Decimal(strike_milli) / Decimal(1000)
    return ticker, expiration, option_type, strike


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
        """Latest NBBO quote for an option contract.

        Alpaca with the OPRA feed is the source of truth — it returns
        real consolidated NBBO under the Algo Trader Plus subscription.
        Polygon stays as a backup for the unlikely case where Alpaca's
        options endpoint is unavailable; on the free Polygon tier it
        will simply 403 and we move on. Order is Alpaca first because
        Polygon's free tier doesn't return last_quote on the snapshot
        endpoint, so the call would always fall through anyway.
        """
        symbol = build_occ_symbol(ticker, expiration, option_type, strike)
        alpaca_quote = self._alpaca_option_quote(symbol)
        if alpaca_quote is not None:
            return alpaca_quote
        return self._polygon_option_quote(symbol)

    def _polygon_option_quote(self, contract_symbol: str) -> Quote | None:
        snapshot = self._polygon_option_snapshot(contract_symbol)
        if not snapshot:
            return None
        last_quote = snapshot.get("last_quote") or {}
        bid_raw = last_quote.get("bid")
        ask_raw = last_quote.get("ask")
        if bid_raw is None or ask_raw is None:
            return None
        try:
            bid = Decimal(str(bid_raw))
            ask = Decimal(str(ask_raw))
        except Exception:  # noqa: BLE001
            return None
        if bid <= 0 or ask <= 0 or ask < bid:
            logger.info("Stale/invalid polygon quote for %s: bid=%s ask=%s", contract_symbol, bid, ask)
            return None
        mid = (bid + ask) / Decimal(2)
        spread_pct = (ask - bid) / mid
        last_updated_ns = last_quote.get("last_updated")
        if isinstance(last_updated_ns, (int, float)) and last_updated_ns > 0:
            ts = datetime.fromtimestamp(last_updated_ns / 1_000_000_000, tz=timezone.utc)
        else:
            ts = datetime.now(timezone.utc)
        return Quote(bid=bid, ask=ask, mid=mid, spread_pct=spread_pct, ts=ts)

    def _alpaca_option_quote(self, symbol: str) -> Quote | None:
        from alpaca.data.enums import OptionsFeed
        from alpaca.data.requests import OptionLatestQuoteRequest

        try:
            # OPRA is the real consolidated NBBO; without this argument
            # Alpaca defaults to the "indicative" feed, which prints
            # spreads 3-5x wider than the actual NBBO and was rejecting
            # most signals through the spread gate.
            req = OptionLatestQuoteRequest(
                symbol_or_symbols=symbol,
                feed=OptionsFeed.OPRA,
            )
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
        """Delta/gamma/theta/vega, computed locally via Black-Scholes.

        Replaces the Polygon snapshot (which this account's plan tier
        403s). Inputs — option mid, underlying spot, strike, DTE — all
        come from free real-time feeds. See greeks.py for the math.
        """
        result = self._local_greeks(contract_symbol)
        if result is None:
            return Greeks(delta=None, gamma=None, theta=None, vega=None)
        return Greeks(
            delta=_to_decimal(result.delta),
            gamma=_to_decimal(result.gamma),
            theta=_to_decimal(result.theta),
            vega=_to_decimal(result.vega),
        )

    def get_iv_data(self, contract_symbol: str) -> IVData:
        """Implied volatility, back-solved from the market option price.

        iv_rank / iv_percentile stay None until we maintain a rolling IV
        history table to rank against.
        """
        result = self._local_greeks(contract_symbol)
        if result is None:
            return IVData(iv=None, iv_rank=None, iv_percentile=None)
        return IVData(
            iv=_to_decimal(result.iv),
            iv_rank=None,
            iv_percentile=None,
        )

    def _local_greeks(self, contract_symbol: str) -> "Any":
        """Fetch the live inputs and run the Black-Scholes solver.

        Returns a greeks.GreeksResult, or None if any input is missing
        or the solver can't converge. Each snapshot calls this twice
        (greeks + iv) — fine at a 15-minute cadence.
        """
        from x_alpaca_trading_bot import greeks as greeks_mod

        try:
            ticker, expiration, option_type, strike = parse_occ_symbol(contract_symbol)
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not parse OCC symbol %s: %s", contract_symbol, exc)
            return None

        quote = self.get_option_quote(ticker, expiration, option_type, strike)
        spot = self.get_underlying_price(ticker)
        if quote is None or spot is None:
            return None

        dte_days = (expiration - datetime.now(timezone.utc).date()).days
        return greeks_mod.compute(
            spot=float(spot),
            strike=float(strike),
            dte_days=float(dte_days),
            option_price=float(quote.mid),
            is_call=(option_type == "call"),
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

    def get_underlying_bars(
        self,
        ticker: str,
        now: datetime,
        *,
        timeframe_minutes: int,
        limit: int = 60,
    ) -> list[OHLCBar]:
        """Recent OHLC bars for one underlying ticker — feeds the chart on
        the dashboard's open position card.

        Fetches with a generous lookback window so weekend/holiday gaps
        don't shrink the result below `limit`. Returns at most `limit`
        bars, oldest first. IEX feed, real-time but lower volume than SIP.
        """
        # 3x the nominal window — gives us enough cushion to clear an
        # overnight or weekend gap and still return `limit` bars during
        # a normal trading session.
        lookback = max(timeframe_minutes * limit * 3, 60 * 24)
        df = self._fetch_alpaca_bars(
            ticker, now,
            timeframe_minutes=timeframe_minutes,
            lookback_minutes=lookback,
        )
        if df is None or df.empty:
            return []
        tail = df.tail(limit)
        out: list[OHLCBar] = []
        for ts, row in tail.iterrows():
            # Alpaca returns tz-aware Timestamps; normalize to a plain
            # datetime so consumers don't have to know about pandas.
            py_ts = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
            out.append(OHLCBar(
                ts=py_ts,
                open=Decimal(str(row["open"])),
                high=Decimal(str(row["high"])),
                low=Decimal(str(row["low"])),
                close=Decimal(str(row["close"])),
                volume=int(row.get("volume", 0)),
            ))
        return out

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
        """Volatility proxy — VIXY ETF mid-quote from Alpaca.

        The real VIX index requires a Polygon indices plan this account
        doesn't have (the endpoint 403s). VIXY tracks VIX short-term
        futures: its absolute level is NOT the VIX number, but it rises
        and falls with market volatility, which is all this analysis-only
        field needs. Stored in the `vix` column; the dashboard labels it
        VIXY so the number isn't mistaken for the index.
        """
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockLatestQuoteRequest
        try:
            req = StockLatestQuoteRequest(symbol_or_symbols="VIXY", feed=DataFeed.IEX)
            resp = self._alpaca_stocks.get_stock_latest_quote(req)
            q = resp.get("VIXY") if isinstance(resp, dict) else None
            if q is None:
                return None
            bid = Decimal(str(q.bid_price)) if getattr(q, "bid_price", 0) else None
            ask = Decimal(str(q.ask_price)) if getattr(q, "ask_price", 0) else None
            if bid and ask:
                return (bid + ask) / Decimal(2)
            return bid or ask
        except Exception as exc:  # noqa: BLE001
            logger.warning("VIXY fetch failed: %s", exc)
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
