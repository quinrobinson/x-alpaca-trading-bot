"""FastAPI backend for the dashboard (Phase 8).

Single-process architecture:

    FastAPI lifespan
        ├── WSManager.attach_loop(asyncio loop)
        ├── start background heartbeat task (system.heartbeat every 30s)
        ├── start orchestrator in a thread (broadcast=ws_manager.dispatch_threadsafe)
        └── on shutdown: cancel heartbeat, request orchestrator shutdown

Routes:
    GET  /healthz          — liveness check (no auth)
    GET  /positions        — open positions from orchestrator state
    GET  /signals          — recent signals from the signals table
    GET  /performance      — closed-trade log + aggregate stats
    WS   /ws               — push events to dashboard clients

The factory `create_app()` is the test/runtime seam. Pass:
  - conn: a psycopg.Connection for the REST routers' DB reads
  - orchestrator (optional): if None, orchestrator-backed endpoints work but
    return empty / static state. Tests pass a fake; production passes a
    real Orchestrator (constructed inside this module's `build_orchestrator`
    helper or externally).
  - run_orchestrator: True to spawn the orchestrator thread on startup;
    False keeps the app inert (tests).
  - heartbeat_seconds: cadence for system.heartbeat broadcasts.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from api.routers import debug as debug_router
from api.routers import performance as performance_router
from api.routers import positions as positions_router
from api.routers import signals as signals_router
from api.ws_manager import WSManager

logger = logging.getLogger(__name__)


def create_app(
    *,
    conn: Any | None = None,
    orchestrator: Any | None = None,
    run_orchestrator: bool = False,
    heartbeat_seconds: float = 30.0,
    cors_origins: list[str] | None = None,
) -> FastAPI:
    """Build a FastAPI app wired with the given orchestrator + DB conn.

    `cors_origins` defaults to the comma-separated CORS_ORIGINS env var if
    set, otherwise wildcards "*" for local dev. In production set it to
    your Vercel dashboard origin, e.g. ["https://x-alpaca-bot.vercel.app"].
    """
    ws_manager = WSManager()
    if cors_origins is None:
        raw = os.environ.get("CORS_ORIGINS", "*")
        cors_origins = [o.strip() for o in raw.split(",") if o.strip()]

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 1. Plug the running asyncio loop into the WSManager so the
        #    orchestrator (sync thread) can schedule broadcasts.
        ws_manager.attach_loop(asyncio.get_running_loop())

        # 2. Wire orchestrator.broadcast → WSManager.dispatch_threadsafe.
        if orchestrator is not None:
            orchestrator._broadcast = ws_manager.dispatch_threadsafe  # type: ignore[attr-defined]

        # 3. Start the heartbeat broadcaster.
        heartbeat_task = asyncio.create_task(
            _heartbeat_loop(ws_manager, heartbeat_seconds, orchestrator),
            name="system-heartbeat",
        )

        # 4. Run the orchestrator in a background thread.
        orch_thread: threading.Thread | None = None
        if run_orchestrator and orchestrator is not None:
            orch_thread = threading.Thread(
                target=orchestrator.run, name="orchestrator", daemon=True,
            )
            orch_thread.start()
            logger.info("orchestrator thread started")

        try:
            yield
        finally:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
            if orchestrator is not None:
                try:
                    orchestrator.request_shutdown()
                except Exception:  # noqa: BLE001
                    logger.exception("orchestrator shutdown raised")
            if orch_thread is not None:
                orch_thread.join(timeout=10.0)
                if orch_thread.is_alive():
                    logger.warning("orchestrator thread did not stop in 10s")

    app = FastAPI(
        title="x-alpaca-trading-bot",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.ws_manager = ws_manager
    app.state.orchestrator = orchestrator
    app.state.conn = conn

    @app.get("/healthz", tags=["meta"])
    def healthz() -> dict[str, Any]:
        return {
            "ok": True,
            "ws_clients": ws_manager.client_count,
            "open_positions": (
                len(orchestrator._open_positions) if orchestrator is not None else 0
            ),
            "active_switches": (
                sorted(orchestrator._state.active_switches)
                if orchestrator is not None else []
            ),
            "x_stream_disabled": _x_stream_disabled(orchestrator),
        }

    app.include_router(positions_router.router)
    app.include_router(signals_router.router)
    app.include_router(performance_router.router)
    app.include_router(debug_router.router)

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket) -> None:
        """Push channel for dashboard clients.

        Clients don't need to send anything; we just keep the socket open
        and broadcast events as they happen. If a client wants to ping, we
        echo it back so connection health is observable.
        """
        await ws_manager.connect(websocket)
        try:
            while True:
                msg = await websocket.receive_text()
                # Optional ping: echo back so the client knows we're alive.
                await websocket.send_json({"event": "pong", "payload": {"echo": msg}})
        except WebSocketDisconnect:
            pass
        except Exception:  # noqa: BLE001
            logger.exception("ws endpoint crashed")
        finally:
            await ws_manager.disconnect(websocket)

    return app


async def _heartbeat_loop(
    ws_manager: WSManager,
    interval_seconds: float,
    orchestrator: Any | None,
) -> None:
    """Broadcast `system.heartbeat` every `interval_seconds` until canceled."""
    while True:
        try:
            payload: dict[str, Any] = {
                "ws_clients": ws_manager.client_count,
                "open_positions": (
                    len(orchestrator._open_positions) if orchestrator is not None else 0
                ),
                "active_switches": (
                    sorted(orchestrator._state.active_switches)
                    if orchestrator is not None else []
                ),
                "x_stream_disabled": _x_stream_disabled(orchestrator),
            }
            await ws_manager.broadcast("system.heartbeat", payload)
        except Exception:  # noqa: BLE001
            logger.exception("heartbeat broadcast failed")
        try:
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            raise


def _x_stream_disabled(orchestrator: Any | None) -> bool:
    """Read the operator's DISABLE_X_STREAM flag off the orchestrator's config."""
    if orchestrator is None:
        return False
    cfg = getattr(orchestrator, "_cfg", None)
    return bool(getattr(cfg, "disable_x_stream", False))


# ---- Production entrypoint ------------------------------------------------

def build_production_app() -> FastAPI:
    """Build the app with real config + real orchestrator. Imported by uvicorn."""
    import signal
    from pathlib import Path

    import anthropic

    from x_alpaca_trading_bot import db, executor as exec_mod
    from x_alpaca_trading_bot.config import Config, assert_paper_mode
    from x_alpaca_trading_bot.data_service import DataService
    from x_alpaca_trading_bot.main import Orchestrator
    from x_alpaca_trading_bot.snapshot import SnapshotScheduler

    # Surface our module loggers in journalctl. Uvicorn doesn't configure
    # the root logger, so without this, only WARNING+ gets through and we
    # lose all the "processing post", "No quote for ...", and per-tick
    # diagnostics that the orchestrator + data_service log at INFO.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

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
    anthro = anthropic.Anthropic(api_key=cfg.anthropic_api_key)

    orchestrator = Orchestrator(
        config=cfg, conn=conn, data_service=ds, executor=executor,
        scheduler=sched, anthropic_client=anthro,
    )

    app = create_app(
        conn=conn, orchestrator=orchestrator, run_orchestrator=True,
    )

    # Translate SIGINT / SIGTERM into orchestrator shutdown.
    def _stop(*_args: Any) -> None:
        orchestrator.request_shutdown()
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    return app
