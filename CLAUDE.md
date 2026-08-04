# CLAUDE.md — X → Alpaca Options Bot

> Read this file at the start of every Claude Code session before doing anything else.
> This is the single source of truth for the project.

---

## What This Project Is

An automated pipeline that:
1. Monitors a specific X (Twitter) account in real time for options trade signals
2. Parses those posts using Claude API to extract structured signal data
3. Validates signals against live market conditions before acting
4. Executes paper trades through Alpaca's paper trading API
5. Manages positions with trailing stop logic
6. Captures Greeks and technical indicators on every trade for long-term pattern analysis
7. Streams all state in real time to a React dashboard via WebSocket

**This is a paper trading system only.** There is a hard code guard in `executor.py` that rejects any non-paper Alpaca endpoint. Do not remove it.

---

## Full Spec Location

The complete build specification lives in `X_ALPACA_OPTIONS_HANDOFF.md` at the project root. It contains:
- Full strategy specification (locked — do not change without owner approval)
- Module contracts for every file
- Complete Supabase/Postgres schema
- 11-phase build plan with acceptance gates
- Dashboard specification
- All environment variables

**When in doubt, the handoff doc is the authority.**

---

## Current Project State

> Claude Code: update this section at the end of every session.

| Field | Value |
|---|---|
| Current phase | **STRATEGIC PIVOT (2026-08-04): X-signal strategy abandoned by owner decision — see `SCANNER_PROGRAM.md` (new authority for the scanner program, Phases S1–S3).** Deployment unchanged: bot + API + dashboard live on DO droplet via Cloudflare Tunnel (`x-alpaca-bot.qr-project.dev`), Supabase as prod DB. Scanner Phase A logging continues (volume_ratio + 10:30 cutoff changes in PR #2). |
| Last completed phase | Phase 10. Tags: 1, 3, 4, 5, 7, 8, 9, 10. Phase 6 destructive smoke, Phase 2.a live X connect still pending (X Developer account in CreditsDepleted state). |
| Last session date | 2026-07-06 |
| Open issues | (0) FIXED 2026-07-06: dashboard Timeline + Scanner (and all DB-read endpoints) stalled because the API and orchestrator shared ONE psycopg connection; when Supabase pruned it the orchestrator's per-tick `ensure_connection` swapped `self._conn` to a fresh object but left `app.state.conn` on the dead handle. Fix: API now gets its own dedicated autocommit connection (`api_conn` in `build_production_app`) and every read router self-heals via `api/db_dep.py::resolve_conn` (ensure_connection + rebind under a lock). Requires redeploy/restart to take effect. Follow-up spawned: `config_store.py` has the same stale-cached-conn bug (Settings view + config PATCHes) — not yet fixed. (1) X Developer account is in CreditsDepleted (HTTP 402) — `DISABLE_X_STREAM=true` set on the droplet as a workaround. Resolve by adding API credits OR using a different account. (2) Phase 6 destructive gates need market-hours smoke. (3) Polygon VIX may return None on plan tier. (4) IV rank / percentile None until 252-day history exists. (5) StatusBar's "X stream: connected" is misleading when stream is disabled — show "disabled" instead. (6) systemd TimeoutStopSec=30s sometimes SIGKILLs during graceful shutdown; bump to 60s. (7) ~9/19 closed trades had NULL IV at entry-snapshot time — root cause was Polygon snapshot 403s falling through `_local_greeks` which returns None when the quote fetch fails. Self-resolves as Alpaca OPRA delivers consistent quotes; re-check with `SELECT COUNT(*) FILTER (WHERE iv IS NULL) FROM indicator_snapshots WHERE snapshot_type='entry'` after the next batch of fills. (8) Alpaca OPRA upgrade purchased + agreement signed 2026-06-11, returns empty `{}` (no error) during RTH — open support ticket pending Alpaca confirmation whether OPRA is provisioned for the paper account. |
| Next action | Scanner program Phase S1 (multi-hypothesis scanner lab) + S2 (shares-based execution of the validated failed-breakout slice) per `SCANNER_PROGRAM.md`. S2 sizing/risk parameters pending owner sign-off. X creds question is CLOSED — do not buy credits. |

---

## Directory Structure

```
x_alpaca_trading_bot/
├── x_alpaca_trading_bot/      # Core bot package
│   ├── config.py              # Env-loaded config, paper guard
│   ├── x_stream.py            # X API v2 filtered stream
│   ├── parser.py              # Claude API post parser
│   ├── validator.py           # Market validation gates
│   ├── data_service.py        # Alpaca + Polygon market data
│   ├── strategy.py            # Pure position management logic
│   ├── risk_manager.py        # Kill switches and caps
│   ├── executor.py            # Alpaca paper order execution
│   ├── journal.py             # Supabase writes + Telegram
│   ├── db.py                  # DB connection + migrations
│   ├── alerts.py              # Telegram wrapper
│   └── main.py                # Orchestration + WebSocket server
├── dashboard/                 # React frontend
├── api/                       # FastAPI + WebSocket backend
├── tests/                     # Unit tests
├── scripts/                   # Operational scripts (smoke tests, one-off
│                              # backfills, the strategy-replay CLI)
├── research/                  # Standalone signal research. READ-ONLY
│                              # relative to the live bot — see
│                              # research/README.md
├── deploy/                    # systemd units + install script
├── X_ALPACA_OPTIONS_HANDOFF.md
├── CLAUDE.md                  # This file
├── pyproject.toml
└── .env.example
```

---

## Tech Stack

| Layer | Tool |
|---|---|
| Language | Python 3.12 |
| X Streaming | Tweepy / X API v2 filtered stream |
| Signal Parsing | Claude API — `claude-sonnet-4-20250514` |
| Market Data | Alpaca Market Data API + Polygon.io |
| Trade Execution | Alpaca Paper Trading API (`https://paper-api.alpaca.markets`) |
| Backend API | FastAPI + WebSocket |
| Database | Supabase (Postgres) |
| Dashboard | React + Recharts + Tailwind CSS |
| Deployment | DigitalOcean (bot + API + dashboard, single FastAPI process). Cloudflare Tunnel + Cloudflare Access at the same hostname. |
| Alerts | Telegram Bot API |

---

## Non-Negotiable Rules

These rules apply in every session without exception:

1. **Paper only.** `executor.py` asserts paper endpoint on startup. Never remove or bypass this guard.
2. **No `float` for money.** All prices, strikes, P&L use `Decimal`.
3. **No `datetime.now()` in `strategy.py` or `risk_manager.py`.** Time is always passed as a parameter.
4. **Log before act.** Every signal hits the journal before the validator or executor sees it.
5. **Indicator snapshots are mandatory.** Every trade must have entry and exit snapshots. Missing snapshots are bugs, not acceptable gaps.
6. **Test before integrate.** `strategy.py`, `parser.py`, and `risk_manager.py` have full unit tests before being wired into `main.py`.
7. **Phase gates are hard stops.** If a phase acceptance gate fails, stop and report — do not tune parameters to force a pass.
8. **No "TODO: handle later" in critical paths.** Raise `NotImplementedError` so startup fails loudly.
9. **Commit at every phase boundary** with tag `phase-N-complete`.
10. **`research/` is read-only relative to the live bot.** Scripts in
    `research/` never import from `x_alpaca_trading_bot/`, never write
    to production tables, and never call Alpaca trading endpoints. The
    dependency arrow points one way: production never depends on
    research. Moving a research signal into the bot is an explicit,
    planned change — see `research/README.md`.

---

## Strategy Summary (Quick Reference)

**Signal source:** Single X account, monitored in real time

**Signal fields:** Ticker, option type (call/put), strike price, expiration date, entry price

**Validation gates (all must pass):**
- Post age < 3 minutes
- Live ask within 10% of posted price
- Contract available on Alpaca paper
- Bid/ask spread < 10% of mid
- Market is open

**Entry:** Limit order at live ask, 60-second fill window

**Stop loss:** 20% below fill price (configurable via env)

**Trailing stop (continuous peak-trail, 2026-06):**
- Below +5% peak gain: initial stop holds at -20% from entry
- +5%+ peak gain: trail activates at `peak × 0.95` (clamped to breakeven floor)
- +40%+ peak gain: trail tightens to `peak × 0.97` (aggressive regime)
- Stop only moves up; never down. Peak only moves up; never down.

Replaces the prior discrete table (+20/+30/+40/+60). Rationale: small-cap
tweet pumps frequently peak in the +5% to +15% band and decay before
crossing the old +20% activation, leaving the position unprotected. INOD
(2026-06-04) peaked at +8.89% and got flattened at -11.1% by 15:55 ET
under the old table. See strategy.py module docstring for full history.

**Hard exits:**
- Stop loss hit → immediate market order
- 15:55 ET → **flatten only if DTE ≤ 3**. Contracts with more time hold
  overnight, managed by the trailing stop. (Changed 2026-06; previously
  flattened unconditionally.)
- DTE = 1 → close regardless of P&L (always-on; protects against
  expiry overnight even for positions we hold past 15:55)
- >4 hours with no movement → evaluate and close

**Kill switches:**
- Daily loss -3% → flatten and pause
- 4 consecutive losses → pause, manual restart required
- X stream or Alpaca WebSocket down >60s → flatten and pause

---

## Indicators Captured Per Trade

Logged at entry, every 15 minutes, and at exit into `indicator_snapshots` table.

**Greeks:** Delta, Gamma, Theta, Vega

**IV:** Implied Volatility, IV Rank, IV Percentile

**Technical (underlying):** RSI(14), MACD, VWAP, EMA9, EMA21, ATR(14), Bollinger Band position

**Volume/Structure:** Options volume, Open Interest, Put/Call Ratio, Bid/Ask Spread %

**Market context:** VIX, SPY trend vs EMA21, Sector ETF direction, Upcoming catalyst flag

---

## Environment Variables

All required vars are documented in `.env.example`. The live `.env` is gitignored — never commit it.

Key vars:
- `ALPACA_BASE_URL` — must be `https://paper-api.alpaca.markets`
- `X_TARGET_ACCOUNT_ID` — numeric X account ID (not handle)
- `ANTHROPIC_API_KEY` — for Claude parser
- `POLYGON_API_KEY` — for Greeks and IV data
- `DATABASE_URL` — Supabase direct Postgres connection string

---

## Database

Supabase (Postgres). Schema lives in `deploy/postgres_setup.sql`.

Key tables:
- `x_posts` — every raw post received, actionable or not
- `signals` — parsed and validated signal records
- `orders` — every Alpaca order submitted
- `fills` — every executed fill
- `indicator_snapshots` — Greeks + indicators at entry/monitor/exit
- `trades` — closed position summaries
- `pnl_snapshots` — equity curve, one row per minute during market hours
- `events` — system events, kill switch trips, errors

Migration runner in `db.py` applies new SQL files in order. Never modify existing migrations — add new ones only.

---

## WebSocket Events (Bot → Dashboard)

| Event | Trigger |
|---|---|
| `signal.received` | New X post parsed |
| `signal.validated` | Validation gate completed |
| `trade.entered` | Fill confirmed |
| `trade.updated` | Greeks/indicator refresh (every 15min) |
| `trade.stop_moved` | Trailing stop ratcheted |
| `trade.exited` | Position closed |
| `killswitch.tripped` | Kill switch activated |
| `market.status` | Market open/close change |
| `system.heartbeat` | Every 30 seconds |

---

## How to Start a New Claude Code Session

1. Read this file (`CLAUDE.md`) in full
2. Read `X_ALPACA_OPTIONS_HANDOFF.md` if you need full phase details
3. Check the **Current Project State** table above
4. Pick up from the current phase
5. Do not start a new phase without owner confirmation at the acceptance gate
6. Update the **Current Project State** table at the end of the session

---

## Session Log

> Claude Code: append a one-line summary after each session.

| Date | Phase | Summary |
|---|---|---|
| — | — | Project initialized, CLAUDE.md created |
| 2026-05-12 | Phase 1 | Git init, package scaffold (config/db/main + stubs), schema SQL, paper-mode guard tests (5/5 pass). DB-touching gates 3–5 pending DATABASE_URL. |
| 2026-05-12 | Phase 2 | parser.py (Signal dataclass, prompt v1, parse_post returning ParseResult with metadata). x_stream.py (tweepy v2 filtered stream wrapper, on_post callback, health tracking). 24/24 tests pass incl. ≥90% accuracy meta-test. Live X stream + x_posts DB write deferred. |
| 2026-05-12 | Phase 1 verify | Stood up local Postgres 16, created `x_alpaca_trading_bot` DB, fixed config.py `load_dotenv(override=True)` so .env wins over inherited shell vars. Gates 3/4/5 all pass. Tagged `phase-1-complete` on commit 2ded7c2. |
| 2026-05-12 | Phase 2 verify | Built minimal `journal.insert_raw_post()` + `parser.parse_result_to_journal_dict()` helper. 3 integration tests against local Postgres pass; insert latency p50=1ms / max=7ms (gate 2.d budget is 1000ms). Gate 2.a remains blocked on X creds — no `phase-2-complete` tag yet. |
| 2026-05-12 | Phase 3 | data_service.py (Alpaca options quotes, Polygon Greeks/IV snapshots, Alpaca IEX bars, pandas-ta indicators, sector heatmap). validator.py with 5 gates (time_age, market_open, contract_exists, spread, price_deviation). journal.insert_signal extension. 48/48 tests pass; integration tests hit real APIs. End-to-end validate() latency: mean 148ms, max 318ms vs 3000ms budget. Tag pending — see commits below. |
| 2026-05-12 | Phase 4 | strategy.py (Position dataclass, RATCHET_TABLE, evaluate() with 4 hard exits — stop/15:55/DTE/stale). scripts/backtest_signals.py CLI for CSV replay. 37 new tests (30 strategy + 7 backtest); 85/85 across all phases. AST-based isolation test confirms strategy imports nothing from alpaca/tweepy/anthropic/psycopg/httpx/pandas. |
| 2026-05-12 | Phase 5 | risk_manager.py (SessionState/RiskDecision, evaluate() pure logic for 4 kill switches: daily_loss / consecutive_losses / x_stream_disconnected / alpaca_disconnected). SQL helpers realized_pnl_today() + consecutive_loss_count() against trades. journal.insert_event() for the events table. evaluate_and_log() convenience writes a row on every decision. 33 new tests (21 unit + 12 integration); 118/118 overall. |
| 2026-05-12 | Phase 6 | executor.py with PaperOrder/PaperFill/OpenPosition/ReconciliationSnapshot and an Executor class wrapping Alpaca TradingClient. Primitives: submit_limit_buy / submit_stop_sell / submit_market_sell / wait_for_fill / cancel_order / modify_stop / list_open_orders / list_open_positions / flatten_all / reconcile / is_at_or_past_close. journal extended with insert_order (upsert by alpaca_order_id) + insert_fill. scripts/executor_manual_smoke.py walks the destructive gates with operator confirmation. 23 new tests (20 unit + 3 read-only integration); 141/141 overall. Tag pending operator manual-smoke run. |
| 2026-05-12 | Phase 7 | snapshot.py (SnapshotContext, TrackedPosition, SnapshotScheduler, capture_snapshot, close_trade). journal extended with insert_indicator_snapshot + insert_trade. MarketDataProvider protocol gains get_underlying_price; DataService implements it via Alpaca IEX latest quote. capture_snapshot wraps every data fetch with try/except → null + event log row. close_trade writes exit snapshot + trades row + unregisters from scheduler. 25 new tests (19 scheduler unit + 6 capture/close integration); 166/166 overall. Fixed prior integration fixtures to clean tables in FK-correct order. |
| 2026-05-13 | Phase 7.5 | Orchestrator wired in main.py with PositionRecord, OrchestratorState, queue-based stream callback, tick() loop. Drains posts → parse → journal → validate → risk → submit entry → wait fill → place stop → register scheduler → entry snapshot. Per-tick: advance positions (ratchet → modify_stop, exit → close_position), take due snapshots, 15:55 ET flatten, risk pulse. Heartbeats updated from event.received_at on drain and from get_clock() success in build_session_state — orchestrator is self-healing on connection switches. 8 integration tests; 174/174 overall. |
| 2026-05-13 | Phase 8 | api/ws_manager.py (connect/disconnect/broadcast/dispatch_threadsafe, drops dead clients, JSON-coerces Decimals + datetimes). api/main.py FastAPI app factory with lifespan: attaches loop to WSManager, wires orchestrator._broadcast → dispatch_threadsafe, runs heartbeat task (system.heartbeat every 30s), runs orchestrator in a background thread. REST endpoints: /healthz, /positions (from orchestrator state), /signals (DB query), /performance (trade log + win rate / profit factor stats). WS endpoint /ws echoes pings + pushes events. 24 new tests (11 ws_manager + 13 api); 198/198 overall. build_production_app() entrypoint for uvicorn. |
| 2026-05-13 | Phase 9 | dashboard/ — Vite + React 19 + Tailwind v4. 5 panels (StatusBar, SignalFeed, PositionCard, MarketContext, PerformanceHistory). useWebSocket hook with exponential-backoff reconnect (500ms → 8s cap, resets on open). REST polling every 30s + WS events trigger immediate re-fetches. Hand-rolled equity-curve sparkline (no Recharts dep). 8 Vitest tests for the hook covering open/close/reconnect/backoff-reset/malformed-JSON/unmount-cleanup/send. `npm run build` ships a 66KB-gzipped JS bundle. |
| 2026-05-13 | Phase 10 | Deploy artifacts: api/main.py gains CORSMiddleware + CORS_ORIGINS env. dashboard/src/config.js centralizes apiUrl + wsUrl based on VITE_API_BASE (empty for dev → same-origin, set for prod → droplet URL). deploy/install.sh: idempotent installer, accepts INSTALL_DIR/SERVICE_USER/API_PORT/REPO_URL/REPO_REF env vars; supports `--update`; doesn't touch Postgres or existing services. deploy/x-alpaca-bot.service: systemd unit with NoNewPrivileges + ProtectSystem hardening, restart-on-failure, journal logging. deploy/SETUP.md: end-to-end runbook (Supabase → droplet → Vercel → verification → ops + troubleshooting). dashboard/vercel.json. Tests: 198 Python + 8 dashboard, all green. |
| 2026-07-06 | Bugfix | Timeline + Scanner dashboard views stalled. Root cause: API and orchestrator shared one psycopg connection; orchestrator's per-tick `db.ensure_connection` reconnect swapped `self._conn` but orphaned `app.state.conn` on the dead handle whenever Supabase pruned the idle connection. Fix: dedicated autocommit API connection + new `api/db_dep.py::resolve_conn` (validate/reconnect/rebind under a lock) used by all read routers (timeline, scanner, signals, performance, positions). 375 Python tests green. Needs droplet restart to deploy. Flagged `config_store.py` as the same latent bug (follow-up task spawned). |
| 2026-07-30 | Scanner review | Scored all 132 live scanner_events with forward returns via new `research/evaluate_scanner_events.py` (reads prod events, refetches 5-min IEX bars, measures +30m/+60m/to-close from failure_price, retro-computes volume_ratio). Verdict: raw signal has NO live edge (to-close delta +0.05% vs backtest's +0.62%); conditional edge survives — vol≥1.0x AND failure before 10:30 ET → -0.60% @60m, 59% hit, n=38; drift decays after 60m (exit horizon = 60min, not close). Found bug: main.py builds FailedBreakoutScanner without baseline_volume so volume_ratio is NULL on every live event. Trade autopsy: -$3,010 across 45 trades is entirely the 15 trades that never reached +5% peak (-$3,318); trades that peaked ≥5% net +$418, but 11/27 still closed red despite the breakeven floor (stop slippage). Implemented follow-ups same session: (1) per-ticker baseline volume — new `BarSource.get_baseline_bar_volume` + `DataServiceBarSource` impl (mean daily volume over 20 prior sessions ÷ 78, cached per ticker/day) so live events now log volume_ratio; scanner treats baseline fetch failure as ratio=None, never suppressing detection. (2) DEFAULT_BREAKOUT_CUTOFF 12:00 → 10:30 ET, overridable via new `SCANNER_BREAKOUT_CUTOFF` env (HH:MM, malformed value fails startup). 387 tests green (12 new). NOT yet committed/deployed — needs commit, push, droplet `install.sh --update` + restart. |
| 2026-08-04 | Pivot + S1 + S2 | Owner abandoned the X strategy (27% win rate, entries at BB 0.81–0.95 = buying tops by construction). New authority: SCANNER_PROGRAM.md. S1: scanners/lab.py — 4 hypotheses (failed_breakout, vwap_reject, gap_fade, prior_low_break control) in one shared data pass, each logging under its own scanner_name; SCANNER_HYPOTHESES env. S2 (built DISARMED): equity_strategy.py pure entry/exit (vol≥1.0 + failure<10:30 ET + <10min stale → $1,000 short, +1% buy-stop, 60-min time exit, 15:55 failsafe, max 3 concurrent); scanner_trades table (unique per event = double-entry guard); executor gains submit_market_buy/submit_stop_buy; orchestrator: scanner thread → signal queue → tick stages 1d/3b, EOD equity flatten before option flatten, restart reconciliation covers orphaned shorts; scanner book shares daily-loss kill switch via risk_manager.scanner_realized_pnl_today. Arm switch: SCANNER_TRADING_ENABLED (false until ~4wk fresh volume_ratio data confirms slice out-of-sample). 450 tests green (63 new today). All on PR #2. |
| 2026-08-04 | Scanner UI | Scanner program dashboard surface (branch scanner-program-ui). api/routers/scanner.py reworked: /scanner/status now reports hypotheses + per-scanner event counts + S2 trading config/arm state (getattr-guarded so fake/bare orchestrators can't 500 it), phase auto-derives S1/S2 from the arm switch; /scanner events include scanner_name with whitelisted ?scanner= filter (no SQL interpolation of user input); new /scanner/trades exposes the S2 book (open-first + win-rate/P&L stats). Scanner.jsx: per-hypothesis chips (click = filter), Paper-trading card with ARMED/DISARMED badge + trades table, generic Ref/Trigger/Confirm column names. Header: X stream shows neutral "off (retired)" instead of warning-toned "disabled" (closes open issue 5). 8 new API tests (459 Python total) + dashboard build/vitest green. 4 executor-integration failures during one slow run were transient Alpaca network flakes — all pass on rerun. |
| 2026-05-13 | Phase 10 LIVE | Operator-driven deploy walkthrough. Provisioned Supabase project (sotpmokcdqpdszvzwvat). Pushed repo to github.com/quinrobinson/x-alpaca-trading-bot. Added `x-alpaca-bot.qr-project.dev` ingress rule to existing Cloudflare Tunnel (orb-dashboard) — provides free HTTPS without new domain. Deployed Vercel dashboard with VITE_API_BASE=https://x-alpaca-bot.qr-project.dev. Surfaced + fixed: install.sh git-as-root safe.directory issue, install.sh pull→reset-hard semantics, X stream startup tolerance to bad creds, DISABLE_X_STREAM operator flag, /root/.cloudflared/config.yml vs /etc/cloudflared/config.yml gotcha. All 4 spec gates earned. Tagged phase-10-complete on 3f2a361. |

---

*Project: x_alpaca_trading_bot*
*Owner: [Your name]*
*Paper trading only — no live capital*
