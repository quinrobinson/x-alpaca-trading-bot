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
from x_alpaca_trading_bot.config import Config, assert_paper_mode
from x_alpaca_trading_bot.config_store import BotConfig, BotConfigStore
from x_alpaca_trading_bot.data_service import (
    DataService,
    MarketDataProvider,
    build_occ_symbol,
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


@dataclass
class OrchestratorState:
    """Aggregated state for risk evaluation + diagnostics."""

    starting_equity: Decimal = Decimal("100000")
    current_equity: Decimal = Decimal("100000")
    last_x_received_at: datetime | None = None
    last_alpaca_ok_at: datetime | None = None
    active_switches: frozenset[str] = frozenset()
    consecutive_losses: int = 0


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
        """One iteration of the main loop. Tests drive this directly."""
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")

        # 1. Drain any X stream events that arrived since last tick.
        self._drain_post_queue(now)

        # 2. Mandatory 15:55 ET close — flatten everything if past.
        if self._executor.is_at_or_past_close(now):
            self._flatten_at_close(now)
            return

        # 3. Advance every open position through strategy.evaluate.
        for record in list(self._open_positions.values()):
            self._advance_position(record, now)

        # 4. Take any monitor snapshots that came due.
        for tracked in self._scheduler.positions_due(now):
            record = self._open_positions.get(tracked.ctx.signal_id)
            if record is None:
                continue
            self._take_monitor_snapshot(record, now)
            self._scheduler.mark_snapshotted(tracked.ctx.signal_id, now)

        # 5. Risk pulse — log a single info-level event per tick so we
        #    have a heartbeat in the events table. Connection switches
        #    will trip here if the heartbeats grew stale.
        self._risk_pulse(now)

    # ---- Reconciliation ----------------------------------------------

    def _reconcile_on_startup(self) -> None:
        """Log whatever Alpaca currently shows open; do NOT auto-adopt.

        Manual operator intervention required if there are orphan
        positions (use scripts/executor_manual_smoke.py to clean up).
        """
        snap = self._executor.reconcile(now=datetime.now(timezone.utc))
        self._state.last_alpaca_ok_at = snap.captured_at
        if snap.open_orders or snap.open_positions:
            logger.warning(
                "startup reconciliation found state: %d open orders, %d positions",
                len(snap.open_orders), len(snap.open_positions),
            )
            for o in snap.open_orders:
                logger.warning("  open order: %s %s %s @ %s",
                               o.alpaca_order_id, o.side, o.symbol,
                               o.stop_price or o.limit_price)
            for p in snap.open_positions:
                logger.warning("  open position: %s qty=%s avg=%s",
                               p.symbol, p.qty, p.avg_entry_price)
            journal.insert_event(
                self._conn, ts=snap.captured_at, severity="warning",
                category="reconcile",
                message="startup_state_present",
                context={
                    "open_orders": len(snap.open_orders),
                    "open_positions": len(snap.open_positions),
                },
            )
        else:
            logger.info("startup reconciliation: account is clean")

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
            )
            self._stream_listener.filter(threaded=True)
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
            return

        # 4. Submit entry — limit buy at live ask
        if validation.live_ask is None:
            logger.warning("validation accepted but live_ask is None; skipping")
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

    # ---- Per-position advancement ------------------------------------

    def _advance_position(self, record: PositionRecord, now: datetime) -> None:
        """One tick of position management: fetch price, ratchet, maybe exit."""
        quote = self._ds.get_option_quote(
            record.ticker, record.expiration, record.option_type, record.strike,
        )
        if quote is None:
            return  # no fresh quote — try next tick
        self._state.last_alpaca_ok_at = now

        current_price = quote.mid
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
            new_stop_order = self._executor.modify_stop(
                record.stop_order_id, record.contract_symbol, record.qty, new_stop,
            )
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
        if decision.newly_tripped:
            # Log critical event; mirror tripped switches into our state.
            self._state = replace(
                self._state, active_switches=frozenset(decision.tripped_switches),
            )
            journal.insert_event(
                self._conn, ts=now, severity="critical",
                category="kill_switch",
                message=decision.reason or "trip",
                context={
                    "newly_tripped": list(decision.newly_tripped),
                    "tripped_switches": list(decision.tripped_switches),
                },
            )
            self._broadcast("killswitch.tripped", {
                "tripped": list(decision.tripped_switches),
                "newly": list(decision.newly_tripped),
            })

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

        # When the operator has explicitly disabled the X stream, suppress
        # the x_stream_disconnected kill switch by keeping the heartbeat
        # fresh and stripping it from active_switches if it was tripped
        # before the operator flipped the flag.
        last_x = self._state.last_x_received_at
        active = self._state.active_switches
        if self._runtime_config().disable_x_stream:
            last_x = now
            if "x_stream_disconnected" in active:
                active = frozenset(active) - {"x_stream_disconnected"}
                self._state = replace(self._state, active_switches=active)

        return risk_manager.SessionState(
            starting_equity=self._state.starting_equity,
            current_equity=self._state.starting_equity + realized,
            consecutive_losses=risk_manager.consecutive_loss_count(self._conn, before=now),
            last_x_received_at=last_x,
            last_alpaca_ok_at=self._state.last_alpaca_ok_at,
            market_open=market_open,
            active_switches=active,
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
