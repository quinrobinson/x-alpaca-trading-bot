"""Phase 7.5 — orchestration.

Wires every existing module into one process that actually runs the bot:

    X stream  →  parser  →  validator  →  risk_manager
                                              ↓
        strategy (every tick) ←──── executor (entry / stop / exit)
                ↓                                ↓
            scheduler.update_extremes        journal (orders, fills,
                ↓                              snapshots, trades, events)
       capture_snapshot every 15min
                ↓
            close_trade on exit

Public entry point is `main()`. Tests drive the loop synchronously via
`Orchestrator.tick(now)`.

Design constraints:
  - One main thread + one queue from the X stream callback. Serializes DB
    access; no connection pool needed.
  - `strategy` and `risk_manager` are pure — the orchestrator passes `now`
    in explicitly. The orchestrator itself is allowed to read wall time.
  - All money in Decimal; tz-aware datetimes everywhere.
"""

from __future__ import annotations

import logging
import os
import queue
import signal
import sys
import threading
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from x_alpaca_trading_bot import db, executor as exec_mod, journal, snapshot
from x_alpaca_trading_bot.alerts import TelegramNotifier
from x_alpaca_trading_bot.config import Config, assert_paper_mode
from x_alpaca_trading_bot.config_store import BotConfig, BotConfigStore
from x_alpaca_trading_bot.data_service import (
    DataService,
    MarketDataProvider,
    build_occ_symbol,
    parse_occ_symbol,
)
from x_alpaca_trading_bot.parser import (
    ParseResult,
    parse_post,
    parse_result_to_journal_dict,
)
from x_alpaca_trading_bot import risk_manager, strategy, validator
from x_alpaca_trading_bot.snapshot import (
    SnapshotContext,
    SnapshotScheduler,
    capture_snapshot,
    close_trade,
)

logger = logging.getLogger(__name__)

# Tick cadence — every 5 seconds we drain the queue, evaluate positions,
# take any due snapshots, and check the time stop. Configurable via env
# for tests / smoke runs.
DEFAULT_TICK_SECONDS = float(os.environ.get("ORCHESTRATOR_TICK_SECONDS", "5"))

# Per-contract multiplier — options are priced per share but traded in 100-share
# contracts. So a $1.20 ask on AAPL230120C100 costs $120 per contract.
OPTION_CONTRACT_MULTIPLIER = Decimal("100")

# Rejection reason emitted when no whole-contract size fits the spend cap.
TOO_EXPENSIVE_REASON = "too_expensive"

# Kill switches that should auto-clear when their condition recovers.
# The orchestrator strips these from active_switches before each risk
# evaluation so risk_manager.evaluate recomputes them fresh:
#   - x_stream_disconnected / alpaca_disconnected: clear on reconnect
#   - consecutive_losses: clears after its cooldown window elapses
# daily_loss is intentionally NOT here — it stays latched for the
# session and clears next day when realized_pnl_today resets.
_RECOMPUTED_SWITCHES = frozenset({
    "x_stream_disconnected",
    "alpaca_disconnected",
    "consecutive_losses",
})

# Run the full position reconciliation (compare in-memory _open_positions
# against Alpaca's actual position list) every N ticks. At a 5-second
# tick interval, every 6 ticks = ~30s — generous enough to ride out a
# transient Alpaca API hiccup, tight enough to catch a stop-fired close
# before the bot does too many wasted advance-position passes.
_RECONCILE_EVERY_N_TICKS = 6

# Post-signal price-tracking study: for every signal (taken OR rejected),
# record the option mid at these minute offsets after the bot received
# the signal. The resulting signal_price_tracks rows answer "is there
# capturable move after the tweet". A capture only happens inside a grace
# window after its target so a bot restart can't backfill a wildly-late
# price as if it were on time.
_PRICE_TRACK_OFFSETS_MIN = (1, 5, 15, 30)
_PRICE_TRACK_GRACE = timedelta(minutes=10)


def _stream_listener_alive(listener: Any | None) -> bool:
    """True iff tweepy's background stream thread is currently running.

    Tweepy's StreamingClient exposes a `running` property that returns
    `self.thread is not None and self.thread.is_alive()`. We use this
    as the authoritative "stream is up" signal rather than depending on
    on_keep_alive callbacks, which can be silent for 60+ seconds even
    on healthy filtered streams.
    """
    if listener is None:
        return False
    # tweepy exposes .running; if a future version changes the attr,
    # fall back to inspecting the thread directly. Both checks are
    # defensive — we never want this helper to crash a risk evaluation.
    try:
        running = getattr(listener, "running", None)
        if isinstance(running, bool):
            return running
        thread = getattr(listener, "thread", None)
        return bool(thread is not None and thread.is_alive())
    except Exception:  # noqa: BLE001
        return False


def _order_is_filled(order: Any | None) -> bool:
    """True iff an Alpaca order has fully filled.

    The fast path uses the order's `filled_avg_price` + `filled_at` —
    both populate when the order completes. We use these rather than
    the status string because alpaca-py occasionally returns enum
    values that look like 'OrderStatus.FILLED' instead of the bare
    'filled', and the price/at fields are stable across API versions.
    """
    if order is None:
        return False
    return (
        getattr(order, "filled_avg_price", None) is not None
        and getattr(order, "filled_at", None) is not None
    )


def _compute_qty(live_ask: Decimal, rcfg: BotConfig) -> int:
    """Derive contract qty from the operator's spend cap.

    qty = floor(max_position_spend_usd / (live_ask × 100)),
    then clamped to max_qty_per_position. Returns 0 when the cap can't
    even afford a single contract — the caller should treat that as a
    "too expensive" rejection rather than place a 0-qty order.
    """
    contract_cost = live_ask * OPTION_CONTRACT_MULTIPLIER
    if contract_cost <= 0:
        return 0
    raw = int(rcfg.max_position_spend_usd // contract_cost)
    return max(0, min(raw, rcfg.max_qty_per_position))


# ---- In-memory state shapes -----------------------------------------------

@dataclass
class PositionRecord:
    """Everything the orchestrator needs to keep track of an open position."""

    signal_id: int
    ticker: str
    contract_symbol: str
    option_type: str                       # 'call' or 'put'
    strike: Decimal
    expiration: date
    qty: int
    entry_price: Decimal
    opened_at: datetime
    strategy_position: strategy.Position
    stop_order_id: str | None              # currently-active protective stop on Alpaca
    entry_order_row_id: int                # journal.orders.id
    # Most recent option mid seen by _advance_position. Refreshed every
    # tick (~5s); surfaced via /positions so the dashboard can show live
    # unrealized P&L without depending on a WebSocket push.
    last_option_mid: Decimal | None = None


@dataclass
class OrchestratorState:
    """Aggregated state for risk evaluation + diagnostics."""

    starting_equity: Decimal = Decimal("100000")
    current_equity: Decimal = Decimal("100000")
    last_x_received_at: datetime | None = None
    last_alpaca_ok_at: datetime | None = None
    active_switches: frozenset[str] = frozenset()
    consecutive_losses: int = 0
    # Wall-clock at the end of the most recent successful tick. Surfaced
    # via /healthz so operators can detect a "zombie" state where uvicorn
    # is up but the orchestrator thread died. Stale (> 3× tick interval)
    # means the loop has stopped iterating.
    last_tick_at: datetime | None = None
    # Last cached market-open flag (from Alpaca's get_clock at each tick).
    # Surfaced via /healthz so the dashboard's Market label reflects
    # reality instead of always showing "closed" (the JS default).
    market_open: bool = False
    # Monotonic tick counter — used to throttle expensive reconciliation
    # checks (full Alpaca position diff) to every Nth tick.
    tick_count: int = 0
    # Switches we've already broadcast/notified about this session. Used to
    # dedupe alerts when a switch keeps re-tripping every tick (e.g. the
    # connection switches that get stripped from active_switches each tick
    # for auto-clear). A switch only generates a Telegram alert + WS event
    # the first time it appears here; subsequent re-trips are silent until
    # it clears.
    notified_switches: frozenset[str] = frozenset()


# ---- Stream callback payload ----------------------------------------------

@dataclass(frozen=True)
class _StreamEvent:
    post_id: str
    post_text: str
    posted_at: datetime
    received_at: datetime


# ---- The Orchestrator -----------------------------------------------------

class Orchestrator:
    """The integration layer. Holds open positions and runs the tick loop."""

    def __init__(
        self,
        *,
        config: Config,
        conn: Any,                                  # psycopg.Connection
        data_service: MarketDataProvider,
        executor: exec_mod.Executor,
        scheduler: SnapshotScheduler,
        anthropic_client: Any,                      # has .messages.create
        broadcast: Callable[[str, dict[str, Any]], None] | None = None,
        tick_seconds: float = DEFAULT_TICK_SECONDS,
        deploy_dir: Path | None = None,
        config_store: BotConfigStore | None = None,
        notifier: TelegramNotifier | None = None,
    ) -> None:
        self._cfg = config
        self._conn = conn
        self._ds = data_service
        self._executor = executor
        self._scheduler = scheduler
        self._anthropic = anthropic_client
        self._broadcast = broadcast or (lambda _event, _payload: None)
        self._tick_seconds = tick_seconds
        self._deploy_dir = deploy_dir or Path(__file__).resolve().parent.parent / "deploy"
        # The runtime-tunable settings store (spend cap, qty ceiling, daily
        # loss kill %, x_stream pause). Optional so test fixtures that
        # don't care about the dashboard don't have to construct one — a
        # missing store falls back to the static Config values in
        # `_runtime_config()`.
        self._config_store = config_store
        # Optional Telegram alerter. Test fixtures pass None; production
        # builds it in api.main from Config.telegram_bot_token + chat_id.
        # All notifier calls are guarded with `if self._notifier is not None`.
        self._notifier = notifier

        self._post_queue: "queue.Queue[_StreamEvent]" = queue.Queue()
        self._open_positions: dict[int, PositionRecord] = {}
        self._state = OrchestratorState()
        self._shutdown_event = threading.Event()
        self._stream_listener: Any = None
        self._lock = threading.Lock()

    # ---- Runtime config snapshot --------------------------------------

    def _runtime_config(self) -> BotConfig:
        """Read-once snapshot of the dashboard-tunable settings.

        Falls back to the static Config when no store is attached (test
        fixtures); production always supplies the store via
        `api.main.build_production_app`. Call this at the top of any
        function that needs a setting and stash the snapshot in a local —
        re-reading is cheap, but a snapshot keeps the whole function
        consistent if the operator changes a value mid-evaluation.
        """
        if self._config_store is not None:
            return self._config_store.snapshot()
        # Test/legacy path: synthesize a BotConfig from the env-driven
        # Config so callers can read the same attributes.
        return BotConfig(
            max_position_spend_usd=Decimal("500.00"),
            max_qty_per_position=10,
            daily_loss_kill_pct=self._cfg.daily_loss_kill_pct,
            disable_x_stream=self._cfg.disable_x_stream,
        )

    # ---- Public lifecycle ---------------------------------------------

    def run(self) -> int:
        """Block until shutdown. Returns exit code."""
        try:
            self._reconcile_on_startup()
            self._start_stream()
            logger.info("orchestrator running; tick=%ss", self._tick_seconds)
            while not self._shutdown_event.is_set():
                self.tick(datetime.now(timezone.utc))
                self._shutdown_event.wait(timeout=self._tick_seconds)
        except Exception:
            logger.exception("orchestrator crashed")
            return 1
        finally:
            self._stop_stream()
            logger.info("orchestrator stopped")
        return 0

    def request_shutdown(self) -> None:
        logger.info("shutdown requested")
        self._shutdown_event.set()

    # ---- The tick (testable) ------------------------------------------

    def tick(self, now: datetime) -> None:
        """One iteration of the main loop. Tests drive this directly.

        Every step here is wrapped so an exception in one position, one
        snapshot, or one risk evaluation can't crash the loop and turn
        the bot into a zombie API server. Failures log loudly and the
        loop continues; the rest of the system keeps managing other
        positions.
        """
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")

        # 1. Drain any X stream events that arrived since last tick.
        try:
            self._drain_post_queue(now)
        except Exception:  # noqa: BLE001
            logger.exception("drain_post_queue failed; continuing")

        # 2. Mandatory 15:55 ET close — flatten everything if past.
        try:
            if self._executor.is_at_or_past_close(now):
                self._flatten_at_close(now)
                self._state.last_tick_at = now
                return
        except Exception:  # noqa: BLE001
            logger.exception("close-time check failed; continuing")

        # 3. Advance every open position through strategy.evaluate.
        #    A failure in one position must not poison the rest of the tick.
        for record in list(self._open_positions.values()):
            try:
                self._advance_position(record, now)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "advance_position failed for signal_id=%s; continuing",
                    record.signal_id,
                )

        # 4. Take any monitor snapshots that came due.
        try:
            due = list(self._scheduler.positions_due(now))
        except Exception:  # noqa: BLE001
            logger.exception("scheduler.positions_due failed; continuing")
            due = []
        for tracked in due:
            record = self._open_positions.get(tracked.ctx.signal_id)
            if record is None:
                continue
            try:
                self._take_monitor_snapshot(record, now)
                self._scheduler.mark_snapshotted(tracked.ctx.signal_id, now)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "monitor snapshot failed for signal_id=%s; continuing",
                    tracked.ctx.signal_id,
                )

        # 5. Risk pulse — log a single info-level event per tick so we
        #    have a heartbeat in the events table. Connection switches
        #    will trip here if the heartbeats grew stale.
        try:
            self._risk_pulse(now)
        except Exception:  # noqa: BLE001
            logger.exception("risk_pulse failed; continuing")

        # 6. Periodic position reconciliation — catches positions that
        #    closed on Alpaca's side via paths the bot didn't initiate
        #    (manual close, account-level liquidation, race conditions
        #    where the per-tick stop-order check missed it).
        self._state.tick_count += 1
        if self._state.tick_count % _RECONCILE_EVERY_N_TICKS == 0:
            try:
                self._reconcile_positions(now)
            except Exception:  # noqa: BLE001
                logger.exception("reconcile_positions failed; continuing")

        # 7. Post-signal price tracking — capture option mids for the
        #    research study (signal_price_tracks). Cheap when nothing is
        #    due; throttled to the reconciliation cadence so we don't
        #    query for due captures on every 5s tick.
        if self._state.tick_count % _RECONCILE_EVERY_N_TICKS == 0:
            try:
                self._capture_due_price_tracks(now)
            except Exception:  # noqa: BLE001
                logger.exception("capture_due_price_tracks failed; continuing")

        self._state.last_tick_at = now

    # ---- Reconciliation ----------------------------------------------

    def _reconcile_on_startup(self) -> None:
        """Adopt any open Alpaca positions back into in-memory tracking.

        The orchestrator holds open positions in `_open_positions`, which
        is wiped on every process restart. Without re-adoption, a position
        opened before a restart becomes an orphan — still live on Alpaca
        with its protective stop, but unmanaged: the bot won't ratchet it,
        won't flatten it at 15:55, and won't record/alert on its close.

        Here we read Alpaca's actual open positions and rebuild a
        PositionRecord for each, matching it back to its originating
        signal and its live stop order.
        """
        snap = self._executor.reconcile(now=datetime.now(timezone.utc))
        self._state.last_alpaca_ok_at = snap.captured_at

        if not snap.open_positions and not snap.open_orders:
            logger.info("startup reconciliation: account is clean")
            return

        logger.warning(
            "startup reconciliation: %d open positions, %d open orders — adopting",
            len(snap.open_positions), len(snap.open_orders),
        )

        # Index live stop orders by contract symbol for quick lookup.
        stops_by_symbol: dict[str, str] = {}
        for o in snap.open_orders:
            if str(o.order_type).lower().endswith("stop"):
                stops_by_symbol[o.symbol] = o.alpaca_order_id

        adopted = 0
        for pos in snap.open_positions:
            try:
                if self._adopt_position(pos, stops_by_symbol):
                    adopted += 1
            except Exception:  # noqa: BLE001
                logger.exception(
                    "startup: failed to adopt %s; leaving orphaned", pos.symbol,
                )

        journal.insert_event(
            self._conn, ts=snap.captured_at, severity="warning",
            category="reconcile", message="startup_reconciliation",
            context={
                "open_positions": len(snap.open_positions),
                "open_orders": len(snap.open_orders),
                "adopted": adopted,
            },
        )
        logger.info(
            "startup reconciliation: adopted %d/%d open positions",
            adopted, len(snap.open_positions),
        )

    def _adopt_position(
        self,
        pos: Any,             # executor.OpenPosition
        stops_by_symbol: dict[str, str],
    ) -> bool:
        """Rebuild a PositionRecord for one open Alpaca position.

        Returns True if adopted, False if it can't be matched to a
        signal (in which case the position stays orphaned but is logged).
        """
        contract_symbol = pos.symbol
        try:
            ticker, expiration, option_type, strike = parse_occ_symbol(contract_symbol)
        except Exception:  # noqa: BLE001
            logger.warning("startup: cannot parse OCC %s; skipping", contract_symbol)
            return False

        # Match to the most recent taken signal for this exact contract
        # that has no closing trade row yet.
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.id, s.parsed_at
                FROM signals s
                LEFT JOIN trades t ON t.signal_id = s.id
                WHERE s.taken = true
                  AND s.ticker = %s AND s.option_type = %s
                  AND s.strike = %s AND s.expiration = %s
                  AND t.id IS NULL
                ORDER BY s.id DESC
                LIMIT 1
                """,
                (ticker, option_type, strike, expiration),
            )
            row = cur.fetchone()
        if row is None:
            logger.warning(
                "startup: open position %s has no matching signal; "
                "leaving orphaned", contract_symbol,
            )
            return False
        signal_id, opened_at = row

        qty = abs(int(pos.qty))
        entry_price = Decimal(str(pos.avg_entry_price))

        # Stop price: prefer the live Alpaca stop order; fall back to the
        # configured initial stop if none is on the book.
        stop_order_id = stops_by_symbol.get(contract_symbol)
        stop_price: Decimal | None = None
        if stop_order_id is not None:
            try:
                stop_price = self._executor.get_order(stop_order_id).stop_price
            except Exception:  # noqa: BLE001
                logger.warning("startup: could not read stop order %s", stop_order_id)
        if stop_price is None:
            stop_price = (
                entry_price * (Decimal(1) - self._cfg.stop_loss_pct)
            ).quantize(Decimal("0.01"))
            logger.warning(
                "startup: %s has no stop order on Alpaca — using computed "
                "stop %s; the strategy will re-place it on the next ratchet",
                contract_symbol, stop_price,
            )

        # Rebuild the strategy Position. ratchet_level starts at 0: the
        # ratchet only ever RAISES the stop (candidate > new_stop guard),
        # so seeding it with the real — possibly already-ratcheted — stop
        # price is safe. The stop cannot drop, and a still-elevated price
        # simply re-ratchets on the next tick.
        strat_pos = strategy.Position(
            entry_price=entry_price,
            qty=qty,
            opened_at=opened_at,
            expiration=expiration,
            initial_stop_pct=self._cfg.stop_loss_pct,
            stop_price=stop_price,
            ratchet_level=0,
        )
        record = PositionRecord(
            signal_id=signal_id, ticker=ticker, contract_symbol=contract_symbol,
            option_type=option_type, strike=strike, expiration=expiration,
            qty=qty, entry_price=entry_price, opened_at=opened_at,
            strategy_position=strat_pos, stop_order_id=stop_order_id,
            entry_order_row_id=0,  # entry already journaled; unused post-entry
        )
        with self._lock:
            self._open_positions[signal_id] = record

        ctx = SnapshotContext(
            signal_id=signal_id, contract_symbol=contract_symbol,
            underlying_ticker=ticker,
        )
        self._scheduler.register(ctx, opened_at=opened_at)

        logger.info(
            "startup: adopted %s signal_id=%s qty=%s entry=%s stop=%s",
            contract_symbol, signal_id, qty, entry_price, stop_price,
        )
        return True

    # ---- Stream lifecycle ---------------------------------------------

    def _start_stream(self) -> None:
        """Wire up the X stream listener with our queue-push callback.

        If `disable_x_stream` is set (operator pause switch, or env-driven
        when no config_store is attached), skip the connect entirely and
        suppress the kill switch — operators set this when their X API
        billing is in a CreditsDepleted state, for example.

        Flipping the toggle on/off through the dashboard suppresses the
        kill switch live but does NOT reconnect or disconnect the stream
        — that takes a service restart. The pause is still effective
        because `_process_post` re-reads the flag and drops incoming
        posts when disabled.

        Any other failure (bad creds, network, tweepy version mismatch) is
        caught and logged. The orchestrator runs in a degraded mode — no
        new signals arrive, but the API, snapshot scheduler, and position
        management all continue.
        """
        if self._runtime_config().disable_x_stream:
            logger.info("x_stream disabled via config; skipping connect")
            self._log_stream_disabled(reason="disabled_via_config")
            return

        try:
            from x_alpaca_trading_bot.x_stream import make_listener
        except Exception as exc:  # noqa: BLE001
            logger.warning("x_stream not loaded; running without it: %s", exc)
            self._log_stream_disabled(reason=f"import_failed: {exc!r}")
            return

        try:
            self._stream_listener = make_listener(
                bearer_token=self._cfg.x_bearer_token,
                target_account_id=self._cfg.x_target_account_id,
                on_post=self._on_x_post,
                on_connect=self._on_stream_connected,
                # X sends a keep-alive every ~20s during quiet periods; using
                # it as a heartbeat keeps the kill switch from misfiring on
                # low-volume target accounts where tweets can be sparse.
                on_keep_alive=self._on_stream_connected,
            )
            # tweet_fields=["created_at"] is required for tweet.created_at
            # to be populated — without it tweepy omits the field and
            # XStreamListener.on_tweet falls back to wall-clock now, so
            # posted_at == received_at and the time_age staleness gate
            # (and any latency measurement) is blind.
            self._stream_listener.filter(threaded=True, tweet_fields=["created_at"])
            logger.info("x stream listener started")
        except Exception as exc:  # noqa: BLE001
            # Bad bearer token, target id, network, or tweepy version drift.
            # The bot keeps running; the kill switch will trip if no
            # heartbeat arrives during market hours.
            logger.warning("x_stream startup failed; running without it: %s", exc)
            self._stream_listener = None
            self._log_stream_disabled(reason=f"connect_failed: {exc!r}")

    def _log_stream_disabled(self, *, reason: str) -> None:
        try:
            journal.insert_event(
                self._conn, ts=datetime.now(timezone.utc),
                severity="warning", category="x_stream",
                message="x_stream_disabled",
                context={"reason": reason},
            )
        except Exception:  # noqa: BLE001
            logger.exception("failed to log x_stream_disabled event")

    def _stop_stream(self) -> None:
        listener = self._stream_listener
        if listener is None:
            return
        try:
            listener.disconnect()
        except Exception:  # noqa: BLE001
            logger.exception("failed to disconnect stream cleanly")

    def _on_x_post(self, post_id: str, post_text: str, posted_at: datetime) -> None:
        """Stream callback (runs in stream thread). Push onto queue + bump heartbeat."""
        received_at = datetime.now(timezone.utc)
        with self._lock:
            self._state.last_x_received_at = received_at
        self._post_queue.put(_StreamEvent(post_id, post_text, posted_at, received_at))

    def _on_stream_connected(self) -> None:
        """Heartbeat-bump callback for both tweepy events:

        - on_connect: fires on initial connect + every reconnect (every
          ~20 minutes when X idle-closes the stream)
        - on_keep_alive: fires on X's ~20-second TCP keep-alives during
          quiet periods between tweets

        Either is sufficient proof the stream is alive, so they share one
        handler. This decouples the x_stream_disconnected kill switch
        from tweet arrival rate — a target account that tweets once an
        hour no longer trips the switch every 60 seconds.
        """
        received_at = datetime.now(timezone.utc)
        with self._lock:
            self._state.last_x_received_at = received_at

    # ---- Post handling -----------------------------------------------

    def _drain_post_queue(self, now: datetime) -> None:
        while True:
            try:
                event = self._post_queue.get_nowait()
            except queue.Empty:
                return
            # Each event is itself fresh evidence that the X stream is alive,
            # so keep last_x_received_at no older than the event's received_at.
            with self._lock:
                prior = self._state.last_x_received_at
                if prior is None or event.received_at > prior:
                    self._state.last_x_received_at = event.received_at
            try:
                self._handle_post(event, now)
            except Exception:  # noqa: BLE001
                logger.exception("unhandled error processing %s", event.post_id)
                journal.insert_event(
                    self._conn, ts=now, severity="error",
                    category="orchestrator",
                    message="post_handler_crash",
                    context={"post_id": event.post_id},
                )

    def _handle_post(self, event: _StreamEvent, now: datetime) -> None:
        """End-to-end: parse → validate → risk → submit entry → fill → stop."""
        logger.info("handle_post id=%s text=%r", event.post_id, event.post_text[:120])
        rcfg = self._runtime_config()
        # Pause switch: the dashboard can flip disable_x_stream at any time.
        # When it's on we drop incoming posts on the floor — they never get
        # parsed, validated, or journaled. The kill-switch suppression for
        # this case is handled in `_build_session_state`.
        if rcfg.disable_x_stream:
            logger.info("disable_x_stream=true; dropping post id=%s", event.post_id)
            return
        # 1. Parse via Claude
        result = parse_post(event.post_text, event.posted_at, self._anthropic)
        actionable = result.signal is not None
        logger.info("parsed id=%s actionable=%s parse_version=%s error=%s",
                    event.post_id, actionable, result.parse_version, result.error)
        journal_payload = parse_result_to_journal_dict(result)

        x_post_id = journal.insert_raw_post(
            self._conn,
            post_id=event.post_id,
            post_text=event.post_text,
            posted_at=event.posted_at,
            received_at=event.received_at,
            parse_result=journal_payload,
            actionable=actionable,
        )
        self._broadcast("signal.received", {"post_id": event.post_id, "actionable": actionable})

        if not actionable or result.signal is None:
            return

        # 2. Validate signal against live market
        signal = result.signal
        validation = validator.validate(
            signal,
            self._ds,
            now,
            signal_stale_seconds=self._cfg.signal_stale_seconds,
            price_deviation_pct=self._cfg.price_deviation_pct,
        )
        self._state.last_alpaca_ok_at = now
        signal_id = journal.insert_signal(
            self._conn,
            x_post_id=x_post_id,
            parsed_at=event.received_at,
            ticker=signal.ticker,
            option_type=signal.option_type,
            strike=signal.strike,
            expiration=signal.expiration,
            posted_price=signal.posted_price,
            live_ask=validation.live_ask,
            taken=False,                             # may be flipped below
            rejection_reason=validation.rejection_reason,
            gate_results=validator.gate_results_to_dict(validation),
        )
        logger.info(
            "validated signal_id=%s accepted=%s rejection=%s live_ask=%s elapsed=%.2fs",
            signal_id, validation.accepted, validation.rejection_reason,
            validation.live_ask, validation.elapsed_seconds,
        )
        self._broadcast("signal.validated", {
            "signal_id": signal_id, "accepted": validation.accepted,
            "rejection_reason": validation.rejection_reason,
        })
        if not validation.accepted:
            return

        # 3. Risk check
        state = self._build_session_state(now)
        risk_decision = risk_manager.evaluate_and_log(
            self._conn, state, now,
            daily_loss_kill_pct=rcfg.daily_loss_kill_pct,
            max_consecutive_losses=self._cfg.max_consecutive_losses,
            context={"signal_id": signal_id},
        )
        if not risk_decision.accepted:
            self._state = replace(
                self._state, active_switches=frozenset(risk_decision.tripped_switches),
            )
            # Backfill rejection_reason with the first tripped switch so
            # the signal isn't an orphan "skipped · null" in the timeline.
            journal.update_signal_rejection(
                self._conn, signal_id=signal_id,
                rejection_reason=risk_decision.reason or "risk_blocked",
            )
            return

        # 4. Submit entry — limit buy at live ask
        if validation.live_ask is None:
            logger.warning("validation accepted but live_ask is None; skipping")
            journal.update_signal_rejection(
                self._conn, signal_id=signal_id,
                rejection_reason="no_live_ask",
            )
            return

        # Size the order from the operator's spend cap. Options trade in
        # 100-share contracts, so one contract at $1.20/share costs $120.
        # Floor to whole contracts, then clamp to MAX_QTY_PER_POSITION.
        # If the contract is too expensive to buy even one, journal a
        # rejection rather than placing a 0-qty order.
        qty = _compute_qty(validation.live_ask, rcfg)
        if qty < 1:
            logger.info(
                "signal_id=%s too_expensive: ask=%s × 100 > spend cap %s",
                signal_id, validation.live_ask, rcfg.max_position_spend_usd,
            )
            journal.insert_event(
                self._conn, ts=now, severity="info",
                category="strategy", message=TOO_EXPENSIVE_REASON,
                context={
                    "signal_id": signal_id,
                    "live_ask": str(validation.live_ask),
                    "max_position_spend_usd": str(rcfg.max_position_spend_usd),
                },
            )
            journal.update_signal_rejection(
                self._conn, signal_id=signal_id,
                rejection_reason=TOO_EXPENSIVE_REASON,
            )
            return

        contract_symbol = build_occ_symbol(
            signal.ticker, signal.expiration, signal.option_type, signal.strike,
        )
        entry_order = self._executor.submit_limit_buy(
            contract_symbol, qty, validation.live_ask,
        )
        entry_order_row_id = journal.insert_order(
            self._conn, signal_id=signal_id,
            alpaca_order_id=entry_order.alpaca_order_id,
            submitted_at=entry_order.submitted_at,
            symbol=entry_order.symbol, side=entry_order.side, qty=entry_order.qty,
            order_type=entry_order.order_type,
            limit_price=entry_order.limit_price, stop_price=None,
            status=entry_order.status, raw=entry_order.raw,
        )

        # 5. Wait for fill
        fill = self._executor.wait_for_fill(
            entry_order.alpaca_order_id,
            timeout_seconds=self._cfg.max_fill_wait_seconds,
        )
        if fill is None:
            # Cancel + log missed fill; signal stays taken=False
            self._executor.cancel_order(entry_order.alpaca_order_id)
            journal.insert_event(
                self._conn, ts=datetime.now(timezone.utc), severity="info",
                category="executor", message="entry_fill_timeout",
                context={"signal_id": signal_id, "alpaca_order_id": entry_order.alpaca_order_id},
            )
            journal.update_signal_rejection(
                self._conn, signal_id=signal_id,
                rejection_reason="fill_timeout",
            )
            return

        # 6. Record the fill
        journal.insert_fill(
            self._conn, order_id=entry_order_row_id,
            filled_at=fill.filled_at, symbol=fill.symbol, side=fill.side,
            qty=fill.qty, fill_price=fill.fill_price,
        )

        # 7. Place protective stop
        # opened_at = the tick time (synthetic-safe). fill.filled_at can drift
        # microseconds in prod and arbitrary amounts in tests; using `now`
        # keeps the position aligned with the orchestrator's reference frame.
        opened_at = now
        stop_price = (fill.fill_price * (Decimal(1) - self._cfg.stop_loss_pct)).quantize(Decimal("0.01"))
        stop_order = self._executor.submit_stop_sell(contract_symbol, fill.qty, stop_price)
        journal.insert_order(
            self._conn, signal_id=signal_id,
            alpaca_order_id=stop_order.alpaca_order_id,
            submitted_at=stop_order.submitted_at,
            symbol=stop_order.symbol, side=stop_order.side, qty=stop_order.qty,
            order_type=stop_order.order_type,
            limit_price=None, stop_price=stop_order.stop_price,
            status=stop_order.status, raw=stop_order.raw,
        )

        # 8. Build strategy.Position + register with scheduler
        strat_pos = strategy.open_position(
            entry_price=fill.fill_price, qty=fill.qty, opened_at=opened_at,
            expiration=signal.expiration, initial_stop_pct=self._cfg.stop_loss_pct,
        )
        record = PositionRecord(
            signal_id=signal_id, ticker=signal.ticker, contract_symbol=contract_symbol,
            option_type=signal.option_type, strike=signal.strike,
            expiration=signal.expiration, qty=fill.qty,
            entry_price=fill.fill_price, opened_at=opened_at,
            strategy_position=strat_pos, stop_order_id=stop_order.alpaca_order_id,
            entry_order_row_id=entry_order_row_id,
        )
        with self._lock:
            self._open_positions[signal_id] = record
        ctx = SnapshotContext(
            signal_id=signal_id, contract_symbol=contract_symbol,
            underlying_ticker=signal.ticker,
        )
        self._scheduler.register(ctx, opened_at=opened_at)

        # 9. Entry snapshot
        capture_snapshot(
            self._conn, self._ds, ctx,
            snapshot_type="entry", now=opened_at,
            option_expiration=signal.expiration,
            option_type=signal.option_type, strike=signal.strike,
        )

        # 10. Flip the signals row's `taken` flag to True now that we're in.
        with self._conn.cursor() as cur:
            cur.execute("UPDATE signals SET taken = TRUE WHERE id = %s", (signal_id,))
        self._conn.commit()

        self._broadcast("trade.entered", {
            "signal_id": signal_id, "symbol": contract_symbol,
            "fill_price": str(fill.fill_price), "stop_price": str(stop_price),
        })

        if self._notifier is not None:
            self._notifier.notify_trade_entered(
                ticker=signal.ticker,
                option_type=signal.option_type,
                strike=signal.strike,
                expiration=signal.expiration,
                qty=fill.qty,
                entry_price=fill.fill_price,
                post_text=event.post_text,
            )

    # ---- Per-position advancement ------------------------------------

    def _advance_position(self, record: PositionRecord, now: datetime) -> None:
        """One tick of position management: fetch price, ratchet, maybe exit."""
        # Fast path: if Alpaca's stop order has already filled, the position
        # is gone server-side. Record the close, unregister, bail out before
        # we waste a quote call or try to modify a dead order. Without this,
        # _open_positions accumulates "ghost" positions that stay forever
        # because nothing else in the loop notices the Alpaca-side exit.
        if record.stop_order_id is not None:
            stop_order = None
            try:
                stop_order = self._executor.get_order(record.stop_order_id)
            except Exception:  # noqa: BLE001
                # Order might be 404 (replaced/canceled). Fall through to
                # the periodic reconciliation in tick() to catch that case.
                logger.debug("get_order failed for stop_order_id=%s", record.stop_order_id)
            if _order_is_filled(stop_order):
                self._handle_alpaca_filled_stop(record, stop_order, now)
                return

        quote = self._ds.get_option_quote(
            record.ticker, record.expiration, record.option_type, record.strike,
        )
        if quote is None:
            return  # no fresh quote — try next tick
        self._state.last_alpaca_ok_at = now

        current_price = quote.mid
        # Stash the live mid so /positions can serve fresh unrealized P&L.
        record.last_option_mid = current_price
        # Track running max-gain / max-loss for trades.max_gain_pct / max_loss_pct
        gain_pct = (current_price - record.entry_price) / record.entry_price
        self._scheduler.update_extremes(record.signal_id, gain_pct)

        eval_result = strategy.evaluate(record.strategy_position, current_price, now)

        # Update PositionRecord with the new strategy state.
        record.strategy_position = eval_result.position

        # If the strategy ratcheted the stop up, modify the Alpaca stop.
        new_stop = eval_result.position.stop_price
        old_stop = (
            record.strategy_position.stop_price
            if record.strategy_position is eval_result.position
            else None
        )
        # eval_result.position.stop_price is the canonical new value; compare to
        # whatever Alpaca currently holds. We track the stop separately via
        # record.stop_order_id; refresh when the strategy lifted it.
        if record.stop_order_id is not None and new_stop != record.strategy_position.stop_price - Decimal(0):
            # (Strategy only updates stop upward; condition is true iff new_stop changed)
            pass  # the strategy already wrote new_stop into record.strategy_position above
        # Re-check: did the ratchet move?
        # If yes, replace the stop on Alpaca with a fresh one at new_stop.
        prior_stop_on_book = self._stop_price_on_book(record)
        if prior_stop_on_book is not None and new_stop > prior_stop_on_book:
            # Alpaca rejects stop orders priced at-or-above the market — they
            # would fire immediately. Our quote.mid can briefly disagree with
            # Alpaca's reference price (illiquid contracts, wide spreads,
            # data-source skew), so before pushing the new stop, sanity-check
            # against the same `current_price` the ratchet just used. If they
            # contradict, skip THIS tick and retry next pass — the strategy's
            # internal ratchet state stays at the higher level, so we'll
            # catch up as soon as the prices line up.
            if new_stop >= current_price:
                logger.warning(
                    "skipping stop modify for signal_id=%s: new_stop=%s >= current_price=%s",
                    record.signal_id, new_stop, current_price,
                )
            else:
                try:
                    new_stop_order = self._executor.modify_stop(
                        record.stop_order_id, record.contract_symbol, record.qty, new_stop,
                    )
                except Exception:  # noqa: BLE001
                    # Belt-and-suspenders. modify_stop can fail for reasons
                    # we can't predict (Alpaca outage, race condition with a
                    # fill, etc.). Log and continue — the next tick retries.
                    logger.exception(
                        "modify_stop failed for signal_id=%s new_stop=%s; will retry",
                        record.signal_id, new_stop,
                    )
                else:
                    record.stop_order_id = new_stop_order.alpaca_order_id
                    journal.insert_order(
                        self._conn, signal_id=record.signal_id,
                        alpaca_order_id=new_stop_order.alpaca_order_id,
                        submitted_at=new_stop_order.submitted_at,
                        symbol=new_stop_order.symbol, side=new_stop_order.side,
                        qty=new_stop_order.qty, order_type=new_stop_order.order_type,
                        limit_price=None, stop_price=new_stop_order.stop_price,
                        status=new_stop_order.status, raw=new_stop_order.raw,
                    )
                    self._broadcast("trade.stop_moved", {
                        "signal_id": record.signal_id, "new_stop": str(new_stop),
                    })

        if eval_result.exit is None:
            return

        # Exit path: cancel any live stop, sell at market, close out journals.
        self._close_position(record, eval_result.exit, now)

    def _stop_price_on_book(self, record: PositionRecord) -> Decimal | None:
        if record.stop_order_id is None:
            return None
        try:
            o = self._executor.get_order(record.stop_order_id)
            return o.stop_price
        except Exception:  # noqa: BLE001
            return None

    def _close_position(
        self,
        record: PositionRecord,
        exit_decision: strategy.ExitDecision,
        now: datetime,
    ) -> None:
        """Cancel protective stop, submit market sell, journal everything."""
        if record.stop_order_id:
            self._executor.cancel_order(record.stop_order_id)

        close_order = self._executor.submit_market_sell(record.contract_symbol, record.qty)
        close_row_id = journal.insert_order(
            self._conn, signal_id=record.signal_id,
            alpaca_order_id=close_order.alpaca_order_id,
            submitted_at=close_order.submitted_at,
            symbol=close_order.symbol, side=close_order.side, qty=close_order.qty,
            order_type=close_order.order_type,
            limit_price=None, stop_price=None,
            status=close_order.status, raw=close_order.raw,
        )

        fill = self._executor.wait_for_fill(close_order.alpaca_order_id, timeout_seconds=30)
        exit_price = fill.fill_price if fill is not None else exit_decision.exit_price
        if fill is not None:
            journal.insert_fill(
                self._conn, order_id=close_row_id,
                filled_at=fill.filled_at, symbol=fill.symbol, side=fill.side,
                qty=fill.qty, fill_price=fill.fill_price,
            )

        ctx = SnapshotContext(
            signal_id=record.signal_id, contract_symbol=record.contract_symbol,
            underlying_ticker=record.ticker,
        )
        close_trade(
            self._conn, self._ds,
            ctx=ctx, scheduler=self._scheduler,
            opened_at=record.opened_at, closed_at=now,
            option_type=record.option_type, strike=record.strike,
            expiration=record.expiration,
            entry_price=record.entry_price, exit_price=exit_price,
            qty=record.qty, exit_reason=exit_decision.reason,
        )

        with self._lock:
            self._open_positions.pop(record.signal_id, None)

        self._broadcast("trade.exited", {
            "signal_id": record.signal_id, "exit_price": str(exit_price),
            "reason": exit_decision.reason,
        })

        if self._notifier is not None:
            pnl = (
                (exit_price - record.entry_price)
                * Decimal(record.qty) * OPTION_CONTRACT_MULTIPLIER
            )
            pnl_pct = (
                (exit_price - record.entry_price) / record.entry_price
                if record.entry_price > 0 else Decimal(0)
            )
            hold_minutes = int((now - record.opened_at).total_seconds() // 60)
            self._notifier.notify_trade_closed(
                ticker=record.ticker,
                option_type=record.option_type,
                strike=record.strike,
                expiration=record.expiration,
                qty=record.qty,
                entry_price=record.entry_price,
                exit_price=exit_price,
                pnl=pnl,
                pnl_pct=pnl_pct,
                exit_reason=exit_decision.reason,
                hold_minutes=hold_minutes,
            )

    # ---- Reconciliation: detect Alpaca-side closes -------------------

    def _handle_alpaca_filled_stop(
        self,
        record: PositionRecord,
        stop_order: Any,
        now: datetime,
    ) -> None:
        """The stop on Alpaca has already filled. Record the trade and
        unregister the position — we are NOT submitting another sell
        (it would open a short)."""
        exit_price = Decimal(str(stop_order.filled_avg_price))
        closed_at = stop_order.filled_at or now
        self._record_external_close(
            record, exit_price=exit_price, closed_at=closed_at,
            exit_reason="stop_loss", now=now,
        )

    def _record_external_close(
        self,
        record: PositionRecord,
        *,
        exit_price: Decimal,
        closed_at: datetime,
        exit_reason: str,
        now: datetime,
    ) -> None:
        """Common path for any close that happened on Alpaca without going
        through `_close_position`. Writes the trades row, fires the
        broadcast, fires the Telegram alert, and unregisters from
        in-memory state.

        Idempotent: if a trade row already exists for this signal_id
        (e.g. _close_position recorded it but failed to pop the position),
        we skip the close_trade call so the reconciliation pass can't
        create duplicates. We still clean up in-memory state.
        """
        already_recorded = False
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM trades WHERE signal_id = %s",
                    (record.signal_id,),
                )
                already_recorded = cur.fetchone() is not None
        except Exception:  # noqa: BLE001
            logger.exception(
                "trade-existence check failed for signal_id=%s; "
                "proceeding cautiously (skipping close_trade)",
                record.signal_id,
            )
            already_recorded = True  # err on the side of "don't duplicate"

        if already_recorded:
            logger.info(
                "trade row already exists for signal_id=%s; cleaning up state only",
                record.signal_id,
            )
        else:
            try:
                ctx = SnapshotContext(
                    signal_id=record.signal_id,
                    contract_symbol=record.contract_symbol,
                    underlying_ticker=record.ticker,
                )
                close_trade(
                    self._conn, self._ds,
                    ctx=ctx, scheduler=self._scheduler,
                    opened_at=record.opened_at, closed_at=closed_at,
                    option_type=record.option_type, strike=record.strike,
                    expiration=record.expiration,
                    entry_price=record.entry_price, exit_price=exit_price,
                    qty=record.qty, exit_reason=exit_reason,
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "close_trade failed for signal_id=%s; continuing cleanup",
                    record.signal_id,
                )

        with self._lock:
            self._open_positions.pop(record.signal_id, None)

        # Skip broadcast/telegram if the trade was already recorded —
        # _close_position would have fired those at the time. Otherwise
        # we'd send a second WS event and a second Telegram alert for the
        # same close.
        if already_recorded:
            logger.info(
                "external close cleanup complete for signal_id=%s (no re-notify)",
                record.signal_id,
            )
            return

        self._broadcast("trade.exited", {
            "signal_id": record.signal_id, "exit_price": str(exit_price),
            "reason": exit_reason,
        })

        if self._notifier is not None:
            try:
                pnl = (
                (exit_price - record.entry_price)
                * Decimal(record.qty) * OPTION_CONTRACT_MULTIPLIER
            )
                pnl_pct = (
                    (exit_price - record.entry_price) / record.entry_price
                    if record.entry_price > 0 else Decimal(0)
                )
                hold_minutes = int((closed_at - record.opened_at).total_seconds() // 60)
                self._notifier.notify_trade_closed(
                    ticker=record.ticker,
                    option_type=record.option_type,
                    strike=record.strike,
                    expiration=record.expiration,
                    qty=record.qty,
                    entry_price=record.entry_price,
                    exit_price=exit_price,
                    pnl=pnl,
                    pnl_pct=pnl_pct,
                    exit_reason=exit_reason,
                    hold_minutes=hold_minutes,
                )
            except Exception:  # noqa: BLE001
                logger.exception("notifier failed for signal_id=%s", record.signal_id)

        logger.info(
            "external close detected for signal_id=%s reason=%s exit=%s",
            record.signal_id, exit_reason, exit_price,
        )

    def _reconcile_positions(self, now: datetime) -> None:
        """Compare in-memory _open_positions against Alpaca's actual position
        list. Anything in memory that Alpaca no longer has is a "ghost" —
        the stop fired or the position was closed externally. Record the
        trade and unregister.

        Cheap-ish: one get_all_positions call. Throttled by tick_count to
        run every _RECONCILE_EVERY_N_TICKS ticks. Skips quietly on any
        Alpaca API failure (the next pass will retry).
        """
        if not self._open_positions:
            return

        try:
            alpaca_positions = self._executor._client.get_all_positions()
        except Exception:  # noqa: BLE001
            logger.exception("get_all_positions failed during reconciliation")
            return

        alpaca_symbols = {p.symbol for p in alpaca_positions}
        ghosts = [
            (sig_id, rec)
            for sig_id, rec in list(self._open_positions.items())
            if rec.contract_symbol not in alpaca_symbols
        ]
        if not ghosts:
            return

        for signal_id, record in ghosts:
            # Try to find the actual close fill for accurate exit data.
            exit_price = record.entry_price  # fallback
            closed_at = now
            exit_reason = "external_close"
            try:
                stop_order = (
                    self._executor.get_order(record.stop_order_id)
                    if record.stop_order_id is not None else None
                )
            except Exception:  # noqa: BLE001
                stop_order = None
            if _order_is_filled(stop_order):
                exit_price = Decimal(str(stop_order.filled_avg_price))
                closed_at = stop_order.filled_at or now
                exit_reason = "stop_loss"
            else:
                logger.warning(
                    "ghost position detected with no fillable stop order: signal_id=%s symbol=%s; "
                    "recording close at entry price",
                    signal_id, record.contract_symbol,
                )

            self._record_external_close(
                record,
                exit_price=exit_price,
                closed_at=closed_at,
                exit_reason=exit_reason,
                now=now,
            )

    # ---- Post-signal price tracking (research study) -----------------

    def _capture_due_price_tracks(self, now: datetime) -> None:
        """Record the option mid for recent signals at fixed offsets after
        the bot received them — feeds the signal_price_tracks study.

        Stateless: what's due is derived from the DB each call by
        cross-joining signals against the offset list and anti-joining
        whatever's already captured. A bot restart loses nothing. Each
        capture only fires inside [target, target + grace] so a late
        catch-up can't record a stale price as if it were on time.
        """
        offsets = list(_PRICE_TRACK_OFFSETS_MIN)
        grace_min = int(_PRICE_TRACK_GRACE.total_seconds() // 60)
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.id, s.ticker, s.option_type, s.strike, s.expiration, off.n
                FROM signals s
                CROSS JOIN unnest(%s::int[]) AS off(n)
                LEFT JOIN signal_price_tracks t
                       ON t.signal_id = s.id AND t.offset_minutes = off.n
                WHERE t.id IS NULL
                  AND s.parsed_at > %s
                  AND %s >= s.parsed_at + (off.n * INTERVAL '1 minute')
                  AND %s <  s.parsed_at + (off.n * INTERVAL '1 minute')
                            + (%s * INTERVAL '1 minute')
                """,
                (
                    offsets,
                    now - timedelta(minutes=max(offsets) + grace_min),
                    now,
                    now,
                    grace_min,
                ),
            )
            due = cur.fetchall()

        for signal_id, ticker, option_type, strike, expiration, offset_n in due:
            mid = None
            try:
                quote = self._ds.get_option_quote(
                    ticker, expiration, option_type, strike,
                )
                if quote is not None:
                    mid = quote.mid
            except Exception:  # noqa: BLE001
                logger.exception(
                    "price-track quote failed signal_id=%s offset=%s",
                    signal_id, offset_n,
                )
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO signal_price_tracks
                        (signal_id, offset_minutes, captured_at, option_mid)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (signal_id, offset_minutes) DO NOTHING
                    """,
                    (signal_id, offset_n, now, mid),
                )
            self._conn.commit()

    # ---- Monitor snapshots -------------------------------------------

    def _take_monitor_snapshot(self, record: PositionRecord, now: datetime) -> None:
        ctx = SnapshotContext(
            signal_id=record.signal_id, contract_symbol=record.contract_symbol,
            underlying_ticker=record.ticker,
        )
        capture_snapshot(
            self._conn, self._ds, ctx,
            snapshot_type="monitor", now=now,
            option_expiration=record.expiration,
            option_type=record.option_type, strike=record.strike,
        )

    # ---- 15:55 flatten -----------------------------------------------

    def _flatten_at_close(self, now: datetime) -> None:
        """Cancel everything + close all positions + emit exits/trades."""
        if not self._open_positions and not self._executor.list_open_orders():
            return

        logger.info("15:55 ET — flattening %d open positions", len(self._open_positions))
        # Close each tracked position deterministically with our own bookkeeping.
        for record in list(self._open_positions.values()):
            self._close_position(
                record,
                strategy.ExitDecision(reason="time_stop_1555",
                                      exit_price=record.entry_price,  # nominal; real fill price overrides
                                      triggered_at=now),
                now,
            )
        # Belt-and-suspenders cleanup of anything Alpaca shows.
        self._executor.flatten_all()

    # ---- Risk pulse ---------------------------------------------------

    def _risk_pulse(self, now: datetime) -> None:
        state = self._build_session_state(now)
        rcfg = self._runtime_config()
        decision = risk_manager.evaluate(
            state, now,
            daily_loss_kill_pct=rcfg.daily_loss_kill_pct,
            max_consecutive_losses=self._cfg.max_consecutive_losses,
        )

        # Always mirror the latest tripped set into active_switches so the
        # rest of the orchestrator (the Header status, _handle_post's
        # risk-block path) sees current state.
        tripped = frozenset(decision.tripped_switches)
        if tripped != self._state.active_switches:
            self._state = replace(self._state, active_switches=tripped)

        # Dedupe alerts: only fire on switches that aren't already in
        # `notified_switches`. Without this, connection switches that get
        # stripped each tick for auto-clear purposes would re-appear in
        # `newly_tripped` every 5 seconds and spam Telegram.
        announce = tripped - self._state.notified_switches
        cleared = self._state.notified_switches - tripped
        if announce or cleared:
            self._state = replace(self._state, notified_switches=tripped)

        if announce:
            journal.insert_event(
                self._conn, ts=now, severity="critical",
                category="kill_switch",
                message=decision.reason or "trip",
                context={
                    "newly_tripped": sorted(announce),
                    "tripped_switches": sorted(tripped),
                },
            )
            self._broadcast("killswitch.tripped", {
                "tripped": sorted(tripped),
                "newly": sorted(announce),
            })
            if self._notifier is not None:
                self._notifier.notify_killswitch_tripped(
                    newly_tripped=sorted(announce),
                    all_tripped=sorted(tripped),
                    reason=decision.reason,
                )

    def _build_session_state(self, now: datetime) -> risk_manager.SessionState:
        today = now.astimezone(timezone.utc).date()
        realized = risk_manager.realized_pnl_today(self._conn, today)
        # Get-clock is itself a successful Alpaca call; bump the heartbeat
        # before reading it. If get_clock raises, we leave the heartbeat
        # alone and the connection switch will trip on its own.
        market_open = False
        try:
            market_open = self._executor._client.get_clock().is_open
            self._state.last_alpaca_ok_at = now
        except Exception:  # noqa: BLE001
            logger.warning("get_clock failed; market_open=False")
        # Cache the latest market-open flag so /healthz can surface it
        # without making its own Alpaca call.
        self._state.market_open = market_open

        # Some switches must auto-clear when their condition recovers.
        # risk_manager.evaluate unions newly-tripped with active_switches,
        # so anything left in active_switches persists forever. Strip the
        # self-healing switches here so evaluate recomputes them fresh:
        #   - x_stream_disconnected / alpaca_disconnected: clear when the
        #     heartbeats recover
        #   - consecutive_losses: clears once its cooldown elapses (see
        #     risk_manager.DEFAULT_CONSECUTIVE_LOSS_COOLDOWN)
        # daily_loss is NOT stripped — it stays latched for the session
        # and clears naturally next day when realized_pnl_today resets.
        last_x = self._state.last_x_received_at
        active = self._state.active_switches - _RECOMPUTED_SWITCHES
        if self._runtime_config().disable_x_stream:
            # Operator pause: keep the heartbeat fresh so the switch
            # doesn't re-trip in risk_manager.evaluate this call.
            last_x = now
        elif _stream_listener_alive(self._stream_listener):
            # Authoritative liveness signal: tweepy's background thread
            # is alive. On low-volume target accounts the on_keep_alive
            # callback can go silent for 60+ seconds even when the
            # connection is perfectly healthy, which was repeatedly
            # tripping x_stream_disconnected. Trusting the thread state
            # is more honest — if the thread dies (real disconnect),
            # this falls through and the kill switch trips correctly.
            last_x = now
        if active != self._state.active_switches:
            self._state = replace(self._state, active_switches=active)

        return risk_manager.SessionState(
            starting_equity=self._state.starting_equity,
            current_equity=self._state.starting_equity + realized,
            consecutive_losses=risk_manager.consecutive_loss_count(self._conn, before=now),
            last_x_received_at=last_x,
            last_alpaca_ok_at=self._state.last_alpaca_ok_at,
            market_open=market_open,
            active_switches=active,
            last_trade_closed_at=risk_manager.last_trade_closed_at(self._conn),
        )


# ---- main() ----------------------------------------------------------------

def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def main() -> int:
    _configure_logging()

    cfg = Config.load()
    assert_paper_mode(cfg.alpaca_base_url)

    project_root = Path(__file__).resolve().parent.parent
    deploy_dir = project_root / "deploy"

    conn = db.connect(cfg.database_url)
    db.run_migrations(conn, deploy_dir)

    ds = DataService(
        alpaca_api_key=cfg.alpaca_api_key,
        alpaca_secret_key=cfg.alpaca_secret_key,
        alpaca_base_url=cfg.alpaca_base_url,
        polygon_api_key=cfg.polygon_api_key,
    )
    executor = exec_mod.Executor(
        alpaca_api_key=cfg.alpaca_api_key,
        alpaca_secret_key=cfg.alpaca_secret_key,
        alpaca_base_url=cfg.alpaca_base_url,
    )
    sched = SnapshotScheduler()

    import anthropic
    anthro = anthropic.Anthropic(api_key=cfg.anthropic_api_key)

    orch = Orchestrator(
        config=cfg, conn=conn, data_service=ds, executor=executor,
        scheduler=sched, anthropic_client=anthro, deploy_dir=deploy_dir,
    )

    def _stop(*_args: Any) -> None:
        orch.request_shutdown()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    return orch.run()


if __name__ == "__main__":
    sys.exit(main())
