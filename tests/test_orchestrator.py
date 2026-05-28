"""Integration tests for the Orchestrator.

Drives Orchestrator.tick() synchronously with mocked Alpaca + Anthropic so
the full pipeline (post → parse → validate → risk → executor → fill → stop
→ snapshot) runs end-to-end against the real local Postgres + journal +
scheduler.

Skips cleanly when DATABASE_URL is unset.
"""

from __future__ import annotations

import os
import queue
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
import pytest
from dotenv import load_dotenv

from x_alpaca_trading_bot import db, journal
from x_alpaca_trading_bot.config import Config, PAPER_BASE_URL
from x_alpaca_trading_bot.data_service import (
    Greeks,
    Indicators,
    IVData,
    MarketContext,
    Quote,
)
from x_alpaca_trading_bot.executor import Executor
from x_alpaca_trading_bot.main import (
    Orchestrator,
    OrchestratorState,
    PositionRecord,
    _StreamEvent,
)
from x_alpaca_trading_bot.snapshot import SnapshotScheduler


ET = ZoneInfo("America/New_York")
NOW_UTC = datetime(2026, 5, 13, 13, 30, tzinfo=timezone.utc)  # well before 15:55 ET


# ---- Mocks --------------------------------------------------------------

@dataclass
class HappyQuoteProvider:
    """Returns a sensible Quote / Greeks / Indicators / etc for every call."""

    ask: Decimal = Decimal("2.55")
    bid: Decimal = Decimal("2.50")

    def is_market_open(self) -> bool:
        return True

    def get_option_quote(self, ticker, expiration, option_type, strike) -> Quote:
        mid = (self.ask + self.bid) / Decimal(2)
        return Quote(
            bid=self.bid, ask=self.ask, mid=mid,
            spread_pct=(self.ask - self.bid) / mid,
            ts=NOW_UTC,
        )

    def get_greeks(self, contract_symbol: str) -> Greeks:
        return Greeks(delta=Decimal("0.55"), gamma=Decimal("0.03"),
                      theta=Decimal("-0.07"), vega=Decimal("0.18"))

    def get_iv_data(self, contract_symbol: str) -> IVData:
        return IVData(iv=Decimal("0.32"), iv_rank=None, iv_percentile=None)

    def get_indicators(self, ticker: str, now: datetime) -> Indicators:
        return Indicators(
            rsi_14=Decimal("58"), macd=Decimal("0.1"), macd_signal=Decimal("0.08"),
            vwap=Decimal("185"), ema_9=Decimal("184.5"), ema_21=Decimal("184"),
            atr_14=Decimal("2"), bb_position=Decimal("0.6"),
        )

    def get_market_context(self, now: datetime) -> MarketContext:
        return MarketContext(
            vix=Decimal("17.5"), spy_vs_ema21="above", qqq_vs_ema21="above",
            sector_etf_trend={"XLK": Decimal("0.01")},
        )

    def get_underlying_price(self, ticker: str) -> Decimal:
        return Decimal("185.10")


@dataclass
class FakeFillBlob:
    fill_price: Decimal
    qty: int
    filled_at: datetime


@dataclass
class FakeOrderBlob:
    id: str
    client_order_id: str
    symbol: str
    side: str
    type: str = "limit"
    qty: int = 1
    status: str = "filled"
    limit_price: float | None = None
    stop_price: float | None = None
    filled_avg_price: float | None = None
    filled_at: datetime | None = None
    submitted_at: datetime = field(default_factory=lambda: NOW_UTC)


@dataclass
class FakeClock:
    is_open: bool = True


class FakeAlpacaClient:
    """Just enough of alpaca-py's TradingClient for the Executor to work."""

    def __init__(self) -> None:
        self.submitted: list[FakeOrderBlob] = []
        self.cancellations: list[str] = []
        self.orders_by_id: dict[str, FakeOrderBlob] = {}
        self.positions: list[Any] = []
        self.next_fill: Decimal | None = None  # if set, next submit gets pre-filled
        self.close_all_calls = 0
        self.clock = FakeClock(is_open=True)

    def submit_order(self, req: Any) -> FakeOrderBlob:
        symbol = req.symbol
        side = _enum_str(getattr(req, "side", "buy")).lower()
        type_str = _infer_type(req)
        cid = getattr(req, "client_order_id", "")
        ob = FakeOrderBlob(
            id=f"fake-{uuid.uuid4().hex[:8]}",
            client_order_id=cid, symbol=symbol, side=side, type=type_str,
            qty=int(req.qty),
            limit_price=getattr(req, "limit_price", None),
            stop_price=getattr(req, "stop_price", None),
        )
        if self.next_fill is not None:
            ob.status = "filled"
            ob.filled_avg_price = float(self.next_fill)
            ob.filled_at = NOW_UTC
            self.next_fill = None
        else:
            ob.status = "new"
        self.submitted.append(ob)
        self.orders_by_id[ob.id] = ob
        return ob

    def get_order_by_id(self, oid: str) -> FakeOrderBlob:
        return self.orders_by_id[oid]

    def cancel_order_by_id(self, oid: str) -> None:
        self.cancellations.append(oid)
        if oid in self.orders_by_id:
            self.orders_by_id[oid].status = "canceled"

    def get_orders(self, *, filter: Any = None) -> list[FakeOrderBlob]:  # noqa: A002
        return [o for o in self.orders_by_id.values()
                if o.status in ("new", "accepted", "partially_filled")]

    def get_all_positions(self) -> list[Any]:
        return list(self.positions)

    def close_all_positions(self, *, cancel_orders: bool = True) -> list[FakeOrderBlob]:
        self.close_all_calls += 1
        return []

    def get_clock(self) -> FakeClock:
        return self.clock


def _enum_str(v: Any) -> str:
    if v is None:
        return ""
    if hasattr(v, "value"):
        return str(v.value)
    return str(v)


def _infer_type(req: Any) -> str:
    name = req.__class__.__name__.lower()
    if "limit" in name:
        return "limit"
    if "stop" in name:
        return "stop"
    return "market"


class FakeAnthropic:
    """Returns canned parser JSON per call."""

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.messages = self  # parse_post does client.messages.create

    def create(self, **kwargs: Any) -> Any:
        if not self.responses:
            raise RuntimeError("No more canned anthropic responses")
        text = self.responses.pop(0)

        class _Block:
            def __init__(self, t: str) -> None:
                self.text = t

        class _Resp:
            def __init__(self, t: str) -> None:
                self.content = [_Block(t)]

        return _Resp(text)


# ---- Fixtures -----------------------------------------------------------

def _db_url() -> str | None:
    load_dotenv(override=True)
    return os.environ.get("DATABASE_URL") or None


@pytest.fixture
def conn() -> Iterator[psycopg.Connection]:
    url = _db_url()
    if not url:
        pytest.skip("DATABASE_URL not set")
    c = db.connect(url)
    db.run_migrations(c, Path(__file__).resolve().parent.parent / "deploy")
    with c.cursor() as cur:
        cur.execute("DELETE FROM signal_price_tracks")
        cur.execute("DELETE FROM indicator_snapshots")
        cur.execute("DELETE FROM trades")
        cur.execute("DELETE FROM fills")
        cur.execute("DELETE FROM orders")
        cur.execute("DELETE FROM events")
        cur.execute("DELETE FROM signals")
        cur.execute("DELETE FROM x_posts")
    c.commit()
    yield c
    c.close()


def _config() -> Config:
    return Config(
        x_bearer_token="fake-x",
        x_target_account_id="123",
        anthropic_api_key="fake-anthropic",
        alpaca_api_key="fake-alpaca",
        alpaca_secret_key="fake-alpaca-secret",
        alpaca_base_url=PAPER_BASE_URL,
        polygon_api_key="fake-polygon",
        supabase_url="x", supabase_key="x", database_url="x",
        telegram_bot_token="x", telegram_chat_id="x",
        stop_loss_pct=Decimal("0.20"),
        daily_loss_kill_pct=Decimal("0.03"),
        max_consecutive_losses=4,
        max_fill_wait_seconds=5,
        signal_stale_seconds=180,
        price_deviation_pct=Decimal("0.10"),
    )


def _orch(
    *,
    conn: psycopg.Connection,
    ds: Any | None = None,
    alpaca: FakeAlpacaClient | None = None,
    anthropic_responses: list[str] | None = None,
    seed_heartbeats: bool = True,
    config_store: Any | None = None,
    notifier: Any | None = None,
) -> tuple[Orchestrator, FakeAlpacaClient, list[tuple[str, dict]]]:
    fake_alpaca = alpaca or FakeAlpacaClient()
    ex = Executor(trading_client=fake_alpaca)
    sched = SnapshotScheduler()
    broadcasts: list[tuple[str, dict]] = []

    def record(event: str, payload: dict) -> None:
        broadcasts.append((event, payload))

    orch = Orchestrator(
        config=_config(),
        conn=conn,
        data_service=ds or HappyQuoteProvider(),
        executor=ex,
        scheduler=sched,
        anthropic_client=FakeAnthropic(anthropic_responses or []),
        broadcast=record,
        tick_seconds=1.0,
        config_store=config_store,
        notifier=notifier,
    )
    if seed_heartbeats:
        # Mimic _reconcile_on_startup + an initial stream heartbeat so the
        # connection kill switches don't trip immediately in tests.
        fresh = NOW_UTC - timedelta(seconds=5)
        orch._state.last_alpaca_ok_at = fresh
        orch._state.last_x_received_at = fresh
    return orch, fake_alpaca, broadcasts


# ---- The signal we'll use ------------------------------------------------

VALID_PARSE_JSON = (
    '{"ticker":"AAPL","option_type":"call","strike":"185","expiration":"2026-06-20","entry_price":"2.50"}'
)


def _stream_event(*, posted_at: datetime, post_id: str | None = None) -> _StreamEvent:
    return _StreamEvent(
        post_id=post_id or f"post-{uuid.uuid4().hex[:8]}",
        post_text="$AAPL 6/20 $185c @ 2.50",
        posted_at=posted_at,
        received_at=posted_at + timedelta(seconds=1),
    )


# ---- Tests --------------------------------------------------------------

def test_tick_with_no_state_is_a_no_op(conn: psycopg.Connection) -> None:
    orch, alpaca, _ = _orch(conn=conn, anthropic_responses=[])
    orch.tick(NOW_UTC)
    assert alpaca.submitted == []
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM x_posts")
        assert cur.fetchone()[0] == 0


def test_unactionable_post_only_journals_x_posts(conn: psycopg.Connection) -> None:
    """Pure commentary → parser returns null → x_posts row, no signal row."""
    orch, alpaca, _ = _orch(conn=conn, anthropic_responses=["null"])
    orch._post_queue.put(_stream_event(posted_at=NOW_UTC - timedelta(seconds=30)))
    orch.tick(NOW_UTC)

    with conn.cursor() as cur:
        cur.execute("SELECT actionable FROM x_posts")
        rows = cur.fetchall()
        assert rows == [(False,)]
        cur.execute("SELECT count(*) FROM signals")
        assert cur.fetchone()[0] == 0
    assert alpaca.submitted == []


def test_full_entry_flow_writes_all_journal_rows(conn: psycopg.Connection) -> None:
    """End-to-end: post → parse → validate → risk → entry fill → stop → snapshot."""
    alpaca = FakeAlpacaClient()
    alpaca.next_fill = Decimal("2.55")  # the entry fills immediately

    orch, _, broadcasts = _orch(
        conn=conn, alpaca=alpaca, anthropic_responses=[VALID_PARSE_JSON],
    )

    orch._post_queue.put(_stream_event(posted_at=NOW_UTC - timedelta(seconds=30)))
    orch.tick(NOW_UTC)

    # x_posts row (actionable=True now that parse succeeded)
    with conn.cursor() as cur:
        cur.execute("SELECT actionable FROM x_posts")
        assert cur.fetchone() == (True,)
        cur.execute("SELECT ticker, taken, rejection_reason FROM signals")
        row = cur.fetchone()
        assert row is not None
        ticker, taken, reason = row
        assert ticker == "AAPL"
        assert taken is True
        assert reason is None
        # Two orders submitted: limit buy + stop sell
        cur.execute("SELECT order_type FROM orders ORDER BY id")
        types = [r[0] for r in cur.fetchall()]
        assert types == ["limit", "stop"]
        # Fill row written
        cur.execute("SELECT fill_price FROM fills")
        assert cur.fetchone()[0] == Decimal("2.5500")
        # Entry snapshot written
        cur.execute("SELECT snapshot_type FROM indicator_snapshots")
        types = [r[0] for r in cur.fetchall()]
        assert types == ["entry"]

    # Position registered with the scheduler
    assert len(orch._open_positions) == 1
    assert len(orch._scheduler) == 1

    # WebSocket broadcasts fired
    event_names = [name for name, _ in broadcasts]
    assert "signal.received" in event_names
    assert "signal.validated" in event_names
    assert "trade.entered" in event_names


def test_validation_failure_writes_signal_but_no_order(conn: psycopg.Connection) -> None:
    """Stale post → validator rejects on time_age. Signal row written, no orders."""
    alpaca = FakeAlpacaClient()
    orch, _, _ = _orch(conn=conn, alpaca=alpaca, anthropic_responses=[VALID_PARSE_JSON])
    # Post is older than signal_stale_seconds (180s)
    stale_event = _stream_event(posted_at=NOW_UTC - timedelta(seconds=400))
    orch._post_queue.put(stale_event)
    orch.tick(NOW_UTC)

    with conn.cursor() as cur:
        cur.execute("SELECT taken, rejection_reason FROM signals")
        taken, reason = cur.fetchone()
        assert taken is False
        assert reason == "time_age"
        cur.execute("SELECT count(*) FROM orders")
        assert cur.fetchone()[0] == 0


def test_advance_position_with_stop_loss_closes_trade(conn: psycopg.Connection) -> None:
    """Open a position, then a tick with price below stop → close path runs."""
    alpaca = FakeAlpacaClient()
    alpaca.next_fill = Decimal("2.55")  # entry fills

    orch, _, _ = _orch(conn=conn, alpaca=alpaca, anthropic_responses=[VALID_PARSE_JSON])
    orch._post_queue.put(_stream_event(posted_at=NOW_UTC - timedelta(seconds=30)))
    orch.tick(NOW_UTC)
    assert len(orch._open_positions) == 1

    # Next tick: quote drops below the -20% stop. Both market sell and the
    # close path should run. We pre-arrange the close-fill response on alpaca.
    sub_orch_state_before = dict(orch._open_positions)
    record = next(iter(sub_orch_state_before.values()))
    # Initial stop is 2.55 * 0.80 = 2.04. Push quote down so strategy stops.
    crashing_ds = HappyQuoteProvider(bid=Decimal("1.95"), ask=Decimal("2.00"))
    orch._ds = crashing_ds
    alpaca.next_fill = Decimal("1.97")  # market sell fills here
    later = NOW_UTC + timedelta(minutes=20)
    orch.tick(later)

    # Position should have been closed
    assert orch._open_positions == {}
    with conn.cursor() as cur:
        cur.execute("SELECT exit_reason, exit_price FROM trades")
        rows = cur.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "stop_loss"
        # The exit price should be the close-fill price
        assert rows[0][1] == Decimal("1.9700")
        # Exit snapshot landed
        cur.execute("SELECT snapshot_type FROM indicator_snapshots ORDER BY ts")
        types = [r[0] for r in cur.fetchall()]
        assert types == ["entry", "exit"]


def test_monitor_snapshot_taken_when_interval_elapses(conn: psycopg.Connection) -> None:
    """Open a position, advance time by 15+ minutes → monitor snapshot row appears."""
    alpaca = FakeAlpacaClient()
    alpaca.next_fill = Decimal("2.55")

    orch, _, _ = _orch(conn=conn, alpaca=alpaca, anthropic_responses=[VALID_PARSE_JSON])
    orch._post_queue.put(_stream_event(posted_at=NOW_UTC - timedelta(seconds=30)))
    orch.tick(NOW_UTC)

    # Advance 16 minutes — scheduler should mark the position due.
    later = NOW_UTC + timedelta(minutes=16)
    orch.tick(later)

    with conn.cursor() as cur:
        cur.execute("SELECT snapshot_type FROM indicator_snapshots ORDER BY ts")
        types = [r[0] for r in cur.fetchall()]
        assert types == ["entry", "monitor"]


def test_tick_after_1555_et_flattens(conn: psycopg.Connection) -> None:
    """At 15:55 ET, open positions get closed with reason=time_stop_1555."""
    alpaca = FakeAlpacaClient()
    alpaca.next_fill = Decimal("2.55")

    orch, _, _ = _orch(conn=conn, alpaca=alpaca, anthropic_responses=[VALID_PARSE_JSON])
    # Open a position at a pre-close ET time
    pre_close = datetime(2026, 5, 13, 14, 30, tzinfo=ET).astimezone(timezone.utc)
    orch._post_queue.put(_stream_event(posted_at=pre_close - timedelta(seconds=30)))
    orch.tick(pre_close)
    assert len(orch._open_positions) == 1

    # Now tick at 15:55 ET — flatten should run
    at_close = datetime(2026, 5, 13, 15, 55, tzinfo=ET).astimezone(timezone.utc)
    alpaca.next_fill = Decimal("2.60")
    orch.tick(at_close)

    assert orch._open_positions == {}
    with conn.cursor() as cur:
        cur.execute("SELECT exit_reason FROM trades")
        rows = cur.fetchall()
        assert rows == [("time_stop_1555",)]


def test_orchestrator_state_tracks_x_heartbeat_via_callback(conn: psycopg.Connection) -> None:
    """Calling _on_x_post directly (simulating the stream thread) bumps the heartbeat."""
    orch, _, _ = _orch(conn=conn, seed_heartbeats=False)
    assert orch._state.last_x_received_at is None
    posted = NOW_UTC - timedelta(seconds=30)
    orch._on_x_post("post-A", "ignored body", posted)
    assert orch._state.last_x_received_at is not None
    # And the event was queued for the tick
    assert orch._post_queue.qsize() == 1


def test_orchestrator_on_stream_connected_bumps_heartbeat(conn: psycopg.Connection) -> None:
    """Tweepy reconnects must freshen the kill-switch heartbeat so the
    x_stream_disconnected switch doesn't trip when a low-volume target
    account hasn't tweeted in the stall window."""
    orch, _, _ = _orch(conn=conn, seed_heartbeats=False)
    assert orch._state.last_x_received_at is None
    orch._on_stream_connected()
    assert orch._state.last_x_received_at is not None
    # No queued post — this is a connection-state heartbeat, not a tweet.
    assert orch._post_queue.qsize() == 0


class _StubNotifier:
    """Records each notify_* call so tests can assert the right one fired."""

    def __init__(self) -> None:
        self.entered: list[dict] = []
        self.closed: list[dict] = []
        self.killswitch: list[dict] = []

    def notify_trade_entered(self, **kw) -> None:
        self.entered.append(kw)

    def notify_trade_closed(self, **kw) -> None:
        self.closed.append(kw)

    def notify_killswitch_tripped(self, **kw) -> None:
        self.killswitch.append(kw)


def test_notifier_called_on_trade_entered(conn: psycopg.Connection) -> None:
    alpaca = FakeAlpacaClient()
    alpaca.next_fill = Decimal("2.55")
    notifier = _StubNotifier()
    orch, _, _ = _orch(
        conn=conn, alpaca=alpaca, anthropic_responses=[VALID_PARSE_JSON],
        notifier=notifier,
    )
    orch._post_queue.put(_stream_event(posted_at=NOW_UTC - timedelta(seconds=30)))
    orch.tick(NOW_UTC)

    assert len(notifier.entered) == 1
    call = notifier.entered[0]
    assert call["ticker"] == "AAPL"
    assert call["qty"] >= 1
    assert call["entry_price"] == Decimal("2.55")


def test_advance_position_detects_filled_stop_and_records_trade(conn: psycopg.Connection) -> None:
    """If Alpaca's stop order has already filled, the bot must record a
    trade and unregister the position rather than continuing to manage a
    ghost. Without this check, `_open_positions` accumulates stale entries
    that never clean up."""
    alpaca = FakeAlpacaClient()
    alpaca.next_fill = Decimal("2.55")  # entry fills immediately
    orch, _, _ = _orch(conn=conn, alpaca=alpaca, anthropic_responses=[VALID_PARSE_JSON])

    # Open a position via the normal entry flow so all the records exist.
    orch._post_queue.put(_stream_event(posted_at=NOW_UTC - timedelta(seconds=30)))
    orch.tick(NOW_UTC)
    assert len(orch._open_positions) == 1
    signal_id, record = next(iter(orch._open_positions.items()))

    # Now simulate Alpaca's stop filling at 2.00 (a stop-loss exit).
    stop_blob = alpaca.orders_by_id[record.stop_order_id]
    stop_blob.status = "filled"
    stop_blob.filled_avg_price = 2.00
    stop_blob.filled_at = NOW_UTC + timedelta(minutes=5)

    # Next tick should detect the filled stop and clean up.
    orch.tick(NOW_UTC + timedelta(minutes=5, seconds=10))

    assert len(orch._open_positions) == 0
    with conn.cursor() as cur:
        cur.execute("SELECT exit_reason, exit_price FROM trades WHERE signal_id = %s", (signal_id,))
        row = cur.fetchone()
    assert row is not None
    assert row[0] == "stop_loss"
    assert Decimal(row[1]) == Decimal("2.0000")


def test_reconcile_does_not_duplicate_existing_trade(conn: psycopg.Connection) -> None:
    """If _close_position already wrote a trade but failed to pop the
    position from _open_positions (exactly the bug we saw with CTSH/MA),
    the periodic reconciliation must clean up state WITHOUT writing a
    second trade row for the same signal_id."""
    alpaca = FakeAlpacaClient()
    alpaca.next_fill = Decimal("2.55")
    orch, _, broadcasts = _orch(
        conn=conn, alpaca=alpaca, anthropic_responses=[VALID_PARSE_JSON],
    )
    orch._post_queue.put(_stream_event(posted_at=NOW_UTC - timedelta(seconds=30)))
    orch.tick(NOW_UTC)
    signal_id, record = next(iter(orch._open_positions.items()))

    # Simulate _close_position having written the trade but never popping.
    journal.insert_trade(
        conn,
        signal_id=signal_id,
        opened_at=record.opened_at,
        closed_at=NOW_UTC + timedelta(minutes=10),
        ticker=record.ticker,
        option_type=record.option_type,
        strike=record.strike,
        expiration=record.expiration,
        entry_price=record.entry_price,
        exit_price=record.entry_price,
        qty=record.qty,
        exit_reason="stop_loss",
    )
    pre_count = _count_trades(conn, signal_id)
    pre_broadcast_count = sum(1 for e, _ in broadcasts if e == "trade.exited")

    # Drive enough ticks to fire reconciliation (Alpaca already has no
    # position for this contract — FakeAlpacaClient.positions is empty).
    for i in range(1, 7):
        orch.tick(NOW_UTC + timedelta(minutes=10, seconds=i * 5))

    # Position is gone from in-memory state, trade NOT duplicated, no
    # second WS broadcast for the same close.
    assert signal_id not in orch._open_positions
    assert _count_trades(conn, signal_id) == pre_count
    post_broadcast_count = sum(1 for e, _ in broadcasts if e == "trade.exited")
    assert post_broadcast_count == pre_broadcast_count


def _count_trades(conn: psycopg.Connection, signal_id: int) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM trades WHERE signal_id = %s", (signal_id,))
        return int(cur.fetchone()[0])


def test_reconcile_clears_ghost_positions(conn: psycopg.Connection) -> None:
    """Belt-and-suspenders: even if the per-tick stop check misses (e.g.
    the stop order was canceled and the close happened via a market sell
    we didn't initiate), the periodic full-position reconciliation should
    catch the divergence within ~30 seconds."""
    alpaca = FakeAlpacaClient()
    alpaca.next_fill = Decimal("2.55")
    orch, _, _ = _orch(conn=conn, alpaca=alpaca, anthropic_responses=[VALID_PARSE_JSON])
    orch._post_queue.put(_stream_event(posted_at=NOW_UTC - timedelta(seconds=30)))
    orch.tick(NOW_UTC)
    assert len(orch._open_positions) == 1
    signal_id, record = next(iter(orch._open_positions.items()))

    # Simulate the position vanishing from Alpaca with no stop fill record
    # (e.g. a manual close via Alpaca's UI). FakeAlpacaClient.positions is
    # already empty, so we just need to drive enough ticks to fire the
    # periodic reconcile (every 6 ticks).
    for i in range(1, 7):
        orch.tick(NOW_UTC + timedelta(seconds=i * 5))

    # Ghost should be cleared.
    assert signal_id not in orch._open_positions
    with conn.cursor() as cur:
        cur.execute("SELECT exit_reason FROM trades WHERE signal_id = %s", (signal_id,))
        row = cur.fetchone()
    assert row is not None
    # No fill data to recover → falls back to "external_close" at entry price.
    assert row[0] == "external_close"


def test_tick_survives_per_position_exception(conn: psycopg.Connection) -> None:
    """A crash in one position's _advance_position must not kill the tick
    or the rest of the loop. Before the fix, an Alpaca 422 (stop above
    market) would propagate all the way out of run() and zombify the bot.
    """
    orch, _, _ = _orch(conn=conn, seed_heartbeats=True)
    # Inject a fake position whose strategy_position is None — that will
    # raise an AttributeError when _advance_position tries to read it.
    class _Broken:
        signal_id = 9999
        ticker = "FAKE"
        expiration = NOW_UTC.date()
        option_type = "call"
        strike = Decimal("100")

        def __getattr__(self, _name):
            raise RuntimeError("simulated downstream failure")

    orch._open_positions[9999] = _Broken()  # type: ignore[assignment]

    # tick() must NOT raise.
    orch.tick(NOW_UTC)

    # And it should have stamped last_tick_at — proving the loop reached
    # the end despite the failure.
    assert orch._state.last_tick_at == NOW_UTC


def test_listener_thread_alive_keeps_heartbeat_fresh(conn: psycopg.Connection) -> None:
    """When tweepy's listener thread is alive, the orchestrator must
    treat the X stream as healthy regardless of whether on_keep_alive
    has fired recently. This is the fix for the recurring
    x_stream_disconnected trips on low-volume target accounts."""
    orch, _, _ = _orch(conn=conn, seed_heartbeats=False)
    # Simulate a stale heartbeat — last tweet was 5 minutes ago.
    orch._state.last_x_received_at = NOW_UTC - timedelta(minutes=5)

    # Inject a fake listener whose `running` property returns True.
    class _AliveListener:
        running = True

    orch._stream_listener = _AliveListener()

    state = orch._build_session_state(NOW_UTC)
    # The heartbeat handed to risk_manager.evaluate should be fresh
    # (==NOW_UTC), not the 5-minute-old original.
    assert state.last_x_received_at == NOW_UTC


def test_listener_thread_dead_lets_kill_switch_trip(conn: psycopg.Connection) -> None:
    """If the listener thread is dead (genuine disconnect), DON'T paper
    over it — let the heartbeat go stale so the kill switch trips."""
    orch, _, _ = _orch(conn=conn, seed_heartbeats=False)
    orch._state.last_x_received_at = NOW_UTC - timedelta(minutes=5)

    class _DeadListener:
        running = False

    orch._stream_listener = _DeadListener()

    state = orch._build_session_state(NOW_UTC)
    # Heartbeat is the original stale value, not auto-bumped.
    assert state.last_x_received_at == NOW_UTC - timedelta(minutes=5)


def test_x_stream_disconnected_auto_clears_when_heartbeat_recovers(conn: psycopg.Connection) -> None:
    """Once x_stream_disconnected is in active_switches, a fresh heartbeat
    must clear it on the next risk pulse. Previously the switch persisted
    because risk_manager.evaluate unions newly-tripped with active_switches,
    so the connection switches need to be stripped before the call."""
    orch, _, _ = _orch(conn=conn, seed_heartbeats=True)
    # Simulate the switch being tripped earlier.
    orch._state.active_switches = frozenset({"x_stream_disconnected"})
    # Heartbeat is fresh (seed_heartbeats=True set it to NOW_UTC-5s).
    state = orch._build_session_state(NOW_UTC)
    # Connection switches are dropped from the state passed to evaluate.
    assert "x_stream_disconnected" not in state.active_switches
    # And on the orchestrator's own state too.
    assert "x_stream_disconnected" not in orch._state.active_switches


# ---- Spend-cap sizing + runtime config ---------------------------------

class _StubConfigStore:
    """Minimal in-memory stand-in for BotConfigStore in unit tests."""

    def __init__(self, snap: Any) -> None:
        self._snap = snap

    def snapshot(self) -> Any:
        return self._snap


def _snapshot(
    *,
    max_position_spend_usd: Decimal = Decimal("500.00"),
    max_qty_per_position: int = 10,
    daily_loss_kill_pct: Decimal = Decimal("0.03"),
    disable_x_stream: bool = False,
):
    from x_alpaca_trading_bot.config_store import BotConfig
    return BotConfig(
        max_position_spend_usd=max_position_spend_usd,
        max_qty_per_position=max_qty_per_position,
        daily_loss_kill_pct=daily_loss_kill_pct,
        disable_x_stream=disable_x_stream,
    )


def test_entry_qty_derived_from_spend_cap(conn: psycopg.Connection) -> None:
    """A $1000 cap at $2.55/share fills floor(1000 / 255) = 3 contracts."""
    alpaca = FakeAlpacaClient()
    alpaca.next_fill = Decimal("2.55")
    store = _StubConfigStore(_snapshot(max_position_spend_usd=Decimal("1000.00")))
    orch, _, _ = _orch(
        conn=conn, alpaca=alpaca, anthropic_responses=[VALID_PARSE_JSON],
        config_store=store,
    )
    orch._post_queue.put(_stream_event(posted_at=NOW_UTC - timedelta(seconds=30)))
    orch.tick(NOW_UTC)

    with conn.cursor() as cur:
        cur.execute("SELECT qty FROM orders WHERE order_type = 'limit'")
        row = cur.fetchone()
    assert row is not None
    assert row[0] == 3


def test_entry_qty_clamped_to_max_qty_per_position(conn: psycopg.Connection) -> None:
    """Even with plenty of cap, qty never exceeds max_qty_per_position."""
    alpaca = FakeAlpacaClient()
    alpaca.next_fill = Decimal("2.55")
    store = _StubConfigStore(_snapshot(
        max_position_spend_usd=Decimal("10000.00"),
        max_qty_per_position=2,
    ))
    orch, _, _ = _orch(
        conn=conn, alpaca=alpaca, anthropic_responses=[VALID_PARSE_JSON],
        config_store=store,
    )
    orch._post_queue.put(_stream_event(posted_at=NOW_UTC - timedelta(seconds=30)))
    orch.tick(NOW_UTC)

    with conn.cursor() as cur:
        cur.execute("SELECT qty FROM orders WHERE order_type = 'limit'")
        row = cur.fetchone()
    assert row is not None
    assert row[0] == 2


def test_signal_rejected_when_contract_exceeds_spend_cap(conn: psycopg.Connection) -> None:
    """Contract costs $255 but cap is $200 → too_expensive, no order submitted."""
    alpaca = FakeAlpacaClient()
    store = _StubConfigStore(_snapshot(max_position_spend_usd=Decimal("200.00")))
    orch, _, _ = _orch(
        conn=conn, alpaca=alpaca, anthropic_responses=[VALID_PARSE_JSON],
        config_store=store,
    )
    orch._post_queue.put(_stream_event(posted_at=NOW_UTC - timedelta(seconds=30)))
    orch.tick(NOW_UTC)

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM orders")
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT message FROM events WHERE category = 'strategy'")
        rows = [r[0] for r in cur.fetchall()]
    assert "too_expensive" in rows


def test_disable_x_stream_drops_incoming_posts(conn: psycopg.Connection) -> None:
    """When the dashboard pauses entries, posts never get parsed or journaled."""
    alpaca = FakeAlpacaClient()
    store = _StubConfigStore(_snapshot(disable_x_stream=True))
    orch, _, _ = _orch(
        conn=conn, alpaca=alpaca, anthropic_responses=[VALID_PARSE_JSON],
        config_store=store,
    )
    orch._post_queue.put(_stream_event(posted_at=NOW_UTC - timedelta(seconds=30)))
    orch.tick(NOW_UTC)

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM x_posts")
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM orders")
        assert cur.fetchone()[0] == 0


# ---- Post-signal price tracking ----------------------------------------

def test_capture_due_price_tracks_records_mids(conn: psycopg.Connection) -> None:
    """A signal received 6 min ago gets price-track rows for the +1m and
    +5m offsets; +15m and +30m aren't due yet."""
    orch, _, _ = _orch(conn=conn)
    xid = journal.insert_raw_post(
        conn, post_id="pt-1", post_text="$AAPL 6/20 185c @ 2.50",
        posted_at=NOW_UTC - timedelta(minutes=6),
        received_at=NOW_UTC - timedelta(minutes=6),
        parse_result=None, actionable=True,
    )
    sid = journal.insert_signal(
        conn, x_post_id=xid, parsed_at=NOW_UTC - timedelta(minutes=6),
        ticker="AAPL", option_type="call", strike=Decimal("185"),
        expiration=date(2026, 6, 20), posted_price=Decimal("2.50"),
        live_ask=Decimal("2.55"), taken=False, rejection_reason="spread",
        gate_results={},
    )
    orch._capture_due_price_tracks(NOW_UTC)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT offset_minutes, option_mid FROM signal_price_tracks "
            "WHERE signal_id = %s ORDER BY offset_minutes",
            (sid,),
        )
        rows = cur.fetchall()
    assert [r[0] for r in rows] == [1, 5]
    # HappyQuoteProvider: mid = (2.55 + 2.50) / 2 = 2.525
    assert all(r[1] == Decimal("2.5250") for r in rows)


def test_capture_due_price_tracks_skips_past_grace_window(conn: psycopg.Connection) -> None:
    """Offsets whose [target, target+grace] window has already closed are
    skipped — a late catch-up must not record a stale price as on-time."""
    orch, _, _ = _orch(conn=conn)
    xid = journal.insert_raw_post(
        conn, post_id="pt-2", post_text="$AAPL 6/20 185c @ 2.50",
        posted_at=NOW_UTC - timedelta(minutes=20),
        received_at=NOW_UTC - timedelta(minutes=20),
        parse_result=None, actionable=True,
    )
    sid = journal.insert_signal(
        conn, x_post_id=xid, parsed_at=NOW_UTC - timedelta(minutes=20),
        ticker="AAPL", option_type="call", strike=Decimal("185"),
        expiration=date(2026, 6, 20), posted_price=Decimal("2.50"),
        live_ask=Decimal("2.55"), taken=False, rejection_reason="spread",
        gate_results={},
    )
    orch._capture_due_price_tracks(NOW_UTC)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT offset_minutes FROM signal_price_tracks "
            "WHERE signal_id = %s ORDER BY offset_minutes",
            (sid,),
        )
        rows = [r[0] for r in cur.fetchall()]
    # parsed 20 min ago, grace 10 min:
    #   +1m  window [1,11]  closed -> skip
    #   +5m  window [5,15]  closed -> skip
    #   +15m window [15,25] open   -> capture
    #   +30m not due yet
    assert rows == [15]


def test_capture_due_price_tracks_is_idempotent(conn: psycopg.Connection) -> None:
    """Running the capture twice must not double-insert a (signal,offset)."""
    orch, _, _ = _orch(conn=conn)
    xid = journal.insert_raw_post(
        conn, post_id="pt-3", post_text="$AAPL 6/20 185c @ 2.50",
        posted_at=NOW_UTC - timedelta(minutes=6),
        received_at=NOW_UTC - timedelta(minutes=6),
        parse_result=None, actionable=True,
    )
    sid = journal.insert_signal(
        conn, x_post_id=xid, parsed_at=NOW_UTC - timedelta(minutes=6),
        ticker="AAPL", option_type="call", strike=Decimal("185"),
        expiration=date(2026, 6, 20), posted_price=Decimal("2.50"),
        live_ask=Decimal("2.55"), taken=False, rejection_reason="spread",
        gate_results={},
    )
    orch._capture_due_price_tracks(NOW_UTC)
    orch._capture_due_price_tracks(NOW_UTC)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM signal_price_tracks WHERE signal_id = %s",
            (sid,),
        )
        assert cur.fetchone()[0] == 2  # offsets 1 and 5, captured once each


# ---- Startup reconciliation (adopt orphaned positions) -----------------

def test_adopt_position_rebuilds_record_from_alpaca(conn: psycopg.Connection) -> None:
    """A position open on Alpaca that the bot lost across a restart gets
    re-adopted into _open_positions and re-registered for snapshots."""
    from x_alpaca_trading_bot.executor import OpenPosition
    orch, _, _ = _orch(conn=conn)
    assert len(orch._open_positions) == 0

    xid = journal.insert_raw_post(
        conn, post_id="adopt-1", post_text="$AAPL 6/20 185c @ 2.50",
        posted_at=NOW_UTC - timedelta(minutes=20),
        received_at=NOW_UTC - timedelta(minutes=20),
        parse_result=None, actionable=True,
    )
    sid = journal.insert_signal(
        conn, x_post_id=xid, parsed_at=NOW_UTC - timedelta(minutes=20),
        ticker="AAPL", option_type="call", strike=Decimal("185"),
        expiration=date(2026, 6, 20), posted_price=Decimal("2.50"),
        live_ask=Decimal("2.55"), taken=True, rejection_reason=None,
        gate_results={},
    )
    pos = OpenPosition(
        symbol="AAPL260620C00185000", qty=3,
        avg_entry_price=Decimal("2.55"), market_value=None, current_price=None,
    )
    assert orch._adopt_position(pos, stops_by_symbol={}) is True

    rec = orch._open_positions.get(sid)
    assert rec is not None
    assert rec.ticker == "AAPL"
    assert rec.qty == 3
    assert rec.entry_price == Decimal("2.55")
    # No stop on Alpaca -> computed initial: 2.55 * (1 - 0.20) = 2.04
    assert rec.strategy_position.stop_price == Decimal("2.04")
    assert orch._scheduler.get(sid) is not None


def test_adopt_position_skips_when_no_matching_signal(conn: psycopg.Connection) -> None:
    """An Alpaca position with no corresponding taken signal is left
    orphaned (and logged), not adopted."""
    from x_alpaca_trading_bot.executor import OpenPosition
    orch, _, _ = _orch(conn=conn)
    pos = OpenPosition(
        symbol="NVDA260620C00900000", qty=2,
        avg_entry_price=Decimal("3.00"), market_value=None, current_price=None,
    )
    assert orch._adopt_position(pos, stops_by_symbol={}) is False
    assert len(orch._open_positions) == 0


# ---- Manual close ("Sell now" button) -----------------------------------

def _open_one_position(
    conn: psycopg.Connection,
) -> tuple[Orchestrator, FakeAlpacaClient, list[tuple[str, dict]], int, PositionRecord]:
    """Helper — drive the normal entry flow so we have one open position."""
    alpaca = FakeAlpacaClient()
    alpaca.next_fill = Decimal("2.55")
    orch, _, broadcasts = _orch(
        conn=conn, alpaca=alpaca, anthropic_responses=[VALID_PARSE_JSON],
    )
    orch._post_queue.put(_stream_event(posted_at=NOW_UTC - timedelta(seconds=30)))
    orch.tick(NOW_UTC)
    assert len(orch._open_positions) == 1
    signal_id, record = next(iter(orch._open_positions.items()))
    return orch, alpaca, broadcasts, signal_id, record


def test_request_manual_close_returns_not_open_for_unknown_signal(
    conn: psycopg.Connection,
) -> None:
    orch, _, _ = _orch(conn=conn)
    result = orch.request_manual_close(signal_id=999_999)
    assert result == {"ok": False, "reason": "not_open", "signal_id": 999_999}


def test_request_manual_close_is_idempotent_on_double_tap(
    conn: psycopg.Connection,
) -> None:
    """A second tap on Sell now while the first is still in flight must
    NOT enqueue a second close — otherwise the queue drains it next tick,
    cancels a stop that's already gone, and submits a duplicate sell."""
    orch, _alpaca, _bc, signal_id, _record = _open_one_position(conn)

    first = orch.request_manual_close(signal_id)
    second = orch.request_manual_close(signal_id)

    assert first["ok"] is True
    assert first.get("reason") != "already_closing"
    assert second["ok"] is True
    assert second["reason"] == "already_closing"
    # Only one request on the queue.
    assert orch._manual_close_queue.qsize() == 1


def test_manual_close_cancels_stop_and_submits_market_sell(
    conn: psycopg.Connection,
) -> None:
    """After Sell now + one tick: the stop is canceled, a market sell
    is on Alpaca, and the position is flagged closing_in_progress."""
    orch, alpaca, broadcasts, signal_id, record = _open_one_position(conn)
    original_stop_id = record.stop_order_id
    assert original_stop_id is not None

    orch.request_manual_close(signal_id)
    orch.tick(NOW_UTC + timedelta(seconds=5))

    # Stop was canceled.
    assert original_stop_id in alpaca.cancellations

    # A market sell was submitted on the same contract.
    sells = [
        o for o in alpaca.submitted
        if o.side == "sell" and o.type == "market"
        and o.symbol == record.contract_symbol
    ]
    assert len(sells) == 1
    assert sells[0].qty == record.qty

    # Position is still tracked, but flagged + linked to the sell order.
    rec = orch._open_positions[signal_id]
    assert rec.closing_in_progress is True
    assert rec.manual_close_order_id == sells[0].id
    assert rec.stop_order_id is None  # cleared after cancel

    # WebSocket got the "closing" event.
    assert any(e == "position.closing" for e, _ in broadcasts)


def test_manual_close_books_trade_with_manual_close_reason_on_fill(
    conn: psycopg.Connection,
) -> None:
    """Once the market sell fills, the next tick must record the trade
    with exit_reason='manual_close' and remove the position."""
    orch, alpaca, broadcasts, signal_id, record = _open_one_position(conn)
    orch.request_manual_close(signal_id)
    orch.tick(NOW_UTC + timedelta(seconds=5))

    sell_order = alpaca.orders_by_id[record.manual_close_order_id]
    sell_order.status = "filled"
    sell_order.filled_avg_price = 2.75  # user got out at a profit
    sell_order.filled_at = NOW_UTC + timedelta(seconds=8)

    orch.tick(NOW_UTC + timedelta(seconds=10))

    # Position cleared, trade booked, reason captured.
    assert signal_id not in orch._open_positions
    with conn.cursor() as cur:
        cur.execute(
            "SELECT exit_reason, exit_price FROM trades WHERE signal_id = %s",
            (signal_id,),
        )
        row = cur.fetchone()
    assert row is not None
    assert row[0] == "manual_close"
    assert Decimal(row[1]) == Decimal("2.7500")

    # And the dashboard gets the trade.exited event.
    assert any(e == "trade.exited" for e, _ in broadcasts)


def test_advance_position_skips_when_closing_in_progress(
    conn: psycopg.Connection,
) -> None:
    """While a manual close is in flight, the autonomous ratchet/exit
    logic must NOT touch the position — otherwise we race the market
    sell with a stop modify and risk a wash-trade rejection."""
    orch, alpaca, _bc, signal_id, record = _open_one_position(conn)
    record.closing_in_progress = True

    cancellations_before = list(alpaca.cancellations)
    submitted_before = list(alpaca.submitted)
    orch._advance_position(record, NOW_UTC + timedelta(seconds=5))
    # No order activity from advance_position.
    assert alpaca.cancellations == cancellations_before
    assert alpaca.submitted == submitted_before
