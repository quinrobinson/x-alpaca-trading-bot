# X → Alpaca Options Bot — Claude Code Handoff

**Project:** `x_alpaca_options_bot`
**Owner:** [Your name]
**Status:** New build, paper-only
**Last updated:** 2026-05-12

---

## 0. Read This First

You are Claude Code. This document is your spec, your build plan, and your acceptance checklist. Work through it phase by phase. Do not skip phases. Do not write live-trading code paths in this build — paper only, full stop.

This is a **new project**. Create a fresh directory `x_alpaca_options_bot/`. Do not reference or import from any other bot projects.

After completing each phase, run the acceptance criteria. Do not move to the next phase until all checks pass. If a gate fails, stop and report back — do not tune your way through it.

---

## 1. Strategy Specification (Locked)

### 1.1 Signal Source

Monitor a specific X (Twitter) account in real time for options trade signals. Each post from this account may contain:
- Ticker symbol (e.g. `$AAPL`)
- Option type (call or put)
- Strike price
- Expiration date
- Entry price target

### 1.2 Signal Validation

Before executing any trade, the signal must pass all of these gates:
1. **Parse complete** — all required fields extracted successfully
2. **Time gate** — post is less than 2–3 minutes old (stale signals are skipped)
3. **Price gate** — live ask price is within 10% of the posted entry price
4. **Contract exists** — the specific contract is available on Alpaca paper
5. **Spread gate** — bid/ask spread is less than 10% of mid price
6. **Market open** — market is currently in regular trading hours

If any gate fails → skip trade, log reason, continue monitoring.

### 1.3 Entry Execution

- Submit limit order at live ask price
- Max fill wait: 60 seconds
- If not filled → cancel, log as missed, continue

### 1.4 Position Management

**Initial stop loss:** 20% below fill price (tunable via config)

**Trailing stop logic (ratchet, never moves down):**

| Position Gain | Stop Loss Action |
|---|---|
| +10% | Move stop to breakeven |
| +20% | Move stop to +10% |
| +25% | Move stop to +20% |
| +40%+ | Tighten to +30%, reassess |

**Hard exit rules:**
- Stop loss hit → market order to close immediately
- 15:55 ET → flatten all positions, no exceptions
- DTE = 1 → close regardless of P&L
- Position open >4 hours with no directional movement → evaluate and close

### 1.5 Indicators to Capture (Every Trade)

Capture at entry, every 15 minutes while open, and at exit:

**Greeks:** Delta, Gamma, Theta, Vega

**IV:** Implied Volatility, IV Rank (IVR), IV Percentile

**Technical (underlying):** RSI(14), MACD, VWAP relationship, EMA9, EMA21, ATR(14), Bollinger Band position

**Volume/Structure:** Options volume, Open Interest, Put/Call Ratio, Bid/Ask Spread %

**Market context:** VIX, SPY trend (above/below EMA21), sector ETF direction, upcoming catalyst flag

### 1.6 Kill Switches

- **Daily loss kill switch:** realized + unrealized P&L reaches -3% of starting equity → flatten and pause until next session
- **Consecutive loss pause:** 4 consecutive losing trades → pause, require manual restart
- **Connection kill switch:** X stream or Alpaca WebSocket down >60 seconds during market hours → flatten positions, pause, alert

---

## 2. Architecture

### 2.1 Directory Structure

```
x_alpaca_options_bot/
├── x_alpaca_options_bot/
│   ├── __init__.py
│   ├── config.py              # All tunables, env-loaded credentials
│   ├── x_stream.py            # X API v2 filtered stream listener
│   ├── parser.py              # Claude API call to parse X posts into signals
│   ├── validator.py           # Market validation gate logic
│   ├── data_service.py        # Alpaca + Polygon market data, Greeks, indicators
│   ├── strategy.py            # Pure signal + position management logic (no I/O)
│   ├── risk_manager.py        # Kill switches, caps, position limits
│   ├── executor.py            # Alpaca paper order submission and management
│   ├── journal.py             # Supabase writes, indicator snapshots, Telegram alerts
│   ├── db.py                  # Supabase/Postgres connection, schema migrations
│   ├── alerts.py              # Telegram wrapper
│   └── main.py                # Wiring, WebSocket server, signal handling, shutdown
├── dashboard/
│   ├── src/
│   │   ├── App.jsx
│   │   ├── components/
│   │   │   ├── StatusBar.jsx       # System health top bar
│   │   │   ├── SignalFeed.jsx      # Live X post + parse results
│   │   │   ├── PositionCard.jsx    # Active position with Greeks + trailing stop
│   │   │   ├── MarketContext.jsx   # VIX, SPY, sector heatmap
│   │   │   └── PerformanceHistory.jsx  # Stats, P&L chart, trade log
│   │   └── hooks/
│   │       └── useWebSocket.js     # WebSocket connection manager
│   ├── package.json
│   └── vite.config.js
├── api/
│   ├── main.py                # FastAPI app with WebSocket endpoint
│   ├── routers/
│   │   ├── positions.py       # REST: current positions
│   │   ├── signals.py         # REST: signal history
│   │   └── performance.py     # REST: stats and trade log
│   └── ws_manager.py          # WebSocket broadcast manager
├── tests/
│   ├── test_parser.py         # Claude parsing with synthetic posts
│   ├── test_validator.py      # Gate logic unit tests
│   ├── test_strategy.py       # Position management unit tests
│   └── test_risk_manager.py   # Kill switch tests
├── scripts/
│   ├── backtest_signals.py    # Replay historical X posts through strategy
│   └── reset_paper_account.py # Wipe Alpaca paper for clean reruns
├── deploy/
│   ├── x-alpaca-bot.service   # systemd unit for bot
│   ├── x-alpaca-api.service   # systemd unit for FastAPI
│   └── install.sh             # Droplet setup script
├── pyproject.toml
├── .env.example
├── README.md
└── CLAUDE.md                  # Context for future Claude Code sessions
```

### 2.2 Module Contracts

**`config.py`** — Loads all config from environment. Exposes frozen `Config` dataclass. Paper endpoint guard: asserts Alpaca base URL is paper endpoint on startup. No business logic.

**`x_stream.py`** — Connects to X API v2 filtered stream for the target account handle. On each post, calls `on_post(post_text, posted_at)` callback. Tracks connection health; triggers kill switch on stall >60s.

**`parser.py`** — Calls Claude API (claude-sonnet-4-20250514) with the raw post text. Returns structured `Signal` object or `None` if post is not a trade signal. Prompt is versioned and logged with each parse result so you can track prompt quality over time.

**`validator.py`** — Takes a `Signal` and runs all market validation gates. Returns `ValidatedSignal` or `RejectionResult` with specific gate that failed. Fetches live market data via `data_service.py`. Pure gate logic — no execution here.

**`data_service.py`** — All market data fetching: Alpaca for quotes and positions, Polygon for Greeks and IV data. Exposes:
- `get_quote(symbol, strike, expiration, option_type)` → bid, ask, mid
- `get_greeks(contract)` → delta, gamma, theta, vega
- `get_indicators(ticker)` → RSI, MACD, VWAP, EMA9/21, ATR, IV, IVR
- `get_market_context()` → VIX, SPY/QQQ trend, sector ETFs

**`strategy.py`** — Pure function. No I/O. No `datetime.now()`. Takes position state + bar updates, returns exit decisions. Implements trailing stop ratchet logic, hard exit rules. Fully unit testable with synthetic data.

**`risk_manager.py`** — Validates signals and position actions against kill switches and limits. Queries Supabase for today's fills and computes real-time P&L, consecutive loss count. Logs every decision.

**`executor.py`** — Translates validated signals into Alpaca paper orders. Manages order lifecycle (submit → fill → stop/target management). Handles 15:55 ET flatten via scheduled task. On startup, reconciles any open positions/orders with DB.

**`journal.py`** — All Supabase writes. Logs signals (taken and rejected), orders, fills, indicator snapshots, P&L snapshots, system events. Also handles Telegram alerts.

**`api/main.py`** — FastAPI app. Exposes WebSocket endpoint `/ws` and REST endpoints. Receives state updates from bot via internal queue. Broadcasts to all connected dashboard clients.

### 2.3 Critical Design Rules

1. **Strategy is broker-agnostic.** `strategy.py` knows nothing about Alpaca or X.
2. **Log before act.** Every signal hits the journal before validator or executor see it.
3. **Time is explicit.** No `datetime.now()` inside strategy or risk_manager. All time inputs come as parameters.
4. **All money uses `Decimal`.** Never `float` for prices, strikes, or P&L.
5. **No hardcoded credentials.** Everything via environment variables.
6. **Paper-only enforcement.** `executor.py` asserts paper endpoint on startup. Hard stop if live URL detected.
7. **Indicator snapshots are non-negotiable.** Even if a trade is a loss, the snapshot data is the long-term value. Never skip logging Greeks and indicators.

---

## 3. Data Model (Supabase/Postgres)

```sql
-- Raw X posts (everything received, actionable or not)
CREATE TABLE x_posts (
    id              BIGSERIAL PRIMARY KEY,
    posted_at       TIMESTAMPTZ NOT NULL,
    received_at     TIMESTAMPTZ NOT NULL,
    post_id         TEXT UNIQUE NOT NULL,
    post_text       TEXT NOT NULL,
    parse_result    JSONB,              -- null if not a trade signal
    actionable      BOOLEAN NOT NULL DEFAULT false
);

-- Signals (parsed and structured trade signals)
CREATE TABLE signals (
    id              BIGSERIAL PRIMARY KEY,
    x_post_id       BIGINT REFERENCES x_posts(id),
    parsed_at       TIMESTAMPTZ NOT NULL,
    ticker          TEXT NOT NULL,
    option_type     TEXT NOT NULL,      -- 'call' or 'put'
    strike          NUMERIC(10, 2) NOT NULL,
    expiration      DATE NOT NULL,
    posted_price    NUMERIC(10, 4) NOT NULL,
    live_ask        NUMERIC(10, 4),     -- at time of validation
    taken           BOOLEAN NOT NULL,
    rejection_reason TEXT,
    gate_results    JSONB NOT NULL      -- each gate pass/fail
);

-- Orders
CREATE TABLE orders (
    id              BIGSERIAL PRIMARY KEY,
    signal_id       BIGINT REFERENCES signals(id),
    alpaca_order_id TEXT UNIQUE NOT NULL,
    submitted_at    TIMESTAMPTZ NOT NULL,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL,
    qty             INTEGER NOT NULL,
    order_type      TEXT NOT NULL,
    limit_price     NUMERIC(10, 4),
    stop_price      NUMERIC(10, 4),
    status          TEXT NOT NULL,
    raw             JSONB NOT NULL
);

-- Fills
CREATE TABLE fills (
    id              BIGSERIAL PRIMARY KEY,
    order_id        BIGINT REFERENCES orders(id),
    filled_at       TIMESTAMPTZ NOT NULL,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL,
    qty             INTEGER NOT NULL,
    fill_price      NUMERIC(10, 4) NOT NULL,
    commission      NUMERIC(8, 4) NOT NULL DEFAULT 0
);

-- Indicator snapshots (entry, every 15min, exit)
CREATE TABLE indicator_snapshots (
    id              BIGSERIAL PRIMARY KEY,
    signal_id       BIGINT REFERENCES signals(id),
    ts              TIMESTAMPTZ NOT NULL,
    snapshot_type   TEXT NOT NULL,      -- 'entry', 'monitor', 'exit'
    -- Greeks
    delta           NUMERIC(8, 4),
    gamma           NUMERIC(8, 4),
    theta           NUMERIC(8, 4),
    vega            NUMERIC(8, 4),
    -- IV
    iv              NUMERIC(8, 4),
    iv_rank         NUMERIC(8, 4),
    iv_percentile   NUMERIC(8, 4),
    -- Technical
    rsi_14          NUMERIC(8, 4),
    macd            NUMERIC(10, 4),
    macd_signal     NUMERIC(10, 4),
    vwap            NUMERIC(12, 4),
    ema_9           NUMERIC(12, 4),
    ema_21          NUMERIC(12, 4),
    atr_14          NUMERIC(10, 4),
    bb_position     NUMERIC(8, 4),      -- 0-1, position within bands
    -- Volume/structure
    options_volume  BIGINT,
    open_interest   BIGINT,
    put_call_ratio  NUMERIC(6, 4),
    bid_ask_spread_pct NUMERIC(6, 4),
    -- Market context
    vix             NUMERIC(8, 4),
    spy_vs_ema21    TEXT,               -- 'above' or 'below'
    sector_etf_trend TEXT,
    -- Option price
    option_bid      NUMERIC(10, 4),
    option_ask      NUMERIC(10, 4),
    option_mid      NUMERIC(10, 4),
    underlying_price NUMERIC(12, 4)
);
CREATE INDEX idx_snapshots_signal_ts ON indicator_snapshots (signal_id, ts);

-- Trades (closed positions with full summary)
CREATE TABLE trades (
    id              BIGSERIAL PRIMARY KEY,
    signal_id       BIGINT REFERENCES signals(id),
    opened_at       TIMESTAMPTZ NOT NULL,
    closed_at       TIMESTAMPTZ NOT NULL,
    ticker          TEXT NOT NULL,
    option_type     TEXT NOT NULL,
    strike          NUMERIC(10, 2) NOT NULL,
    expiration      DATE NOT NULL,
    entry_price     NUMERIC(10, 4) NOT NULL,
    exit_price      NUMERIC(10, 4) NOT NULL,
    qty             INTEGER NOT NULL,
    gross_pnl       NUMERIC(12, 4) NOT NULL,
    pnl_pct         NUMERIC(8, 4) NOT NULL,
    exit_reason     TEXT NOT NULL,      -- 'stop_loss', 'profit_target', 'time_stop', 'manual'
    hold_minutes    INTEGER NOT NULL,
    max_gain_pct    NUMERIC(8, 4),      -- peak unrealized gain during hold
    max_loss_pct    NUMERIC(8, 4)       -- trough unrealized loss during hold
);

-- P&L snapshots (every minute during market hours)
CREATE TABLE pnl_snapshots (
    ts              TIMESTAMPTZ PRIMARY KEY,
    equity          NUMERIC(14, 4) NOT NULL,
    day_pnl         NUMERIC(14, 4) NOT NULL,
    open_positions  JSONB NOT NULL,
    kill_switch_state JSONB NOT NULL
);

-- System events
CREATE TABLE events (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL,
    severity        TEXT NOT NULL,
    category        TEXT NOT NULL,
    message         TEXT NOT NULL,
    context         JSONB
);
```

---

## 4. Phased Build Plan

### Phase 1 — Project Scaffold and DB

**Goal:** Empty bot connects to Supabase and writes rows. Nothing trading-related yet.

Tasks:
1. Create directory structure with empty module stubs
2. Set up `pyproject.toml` with dependencies: `alpaca-py`, `tweepy`, `anthropic`, `fastapi`, `uvicorn`, `websockets`, `psycopg[binary]`, `python-dotenv`, `pydantic`, `httpx`, `pytest`
3. Write `config.py` with env-var loading and paper-endpoint guard
4. Write `db.py` with Supabase connection and migration runner
5. Write `deploy/postgres_setup.sql` with schema above
6. Write `.env.example` documenting all required vars
7. Write minimal `main.py` that loads config, connects to DB, runs migrations, logs "ready", exits cleanly

Acceptance:
- `python -m x_alpaca_options_bot.main` runs to completion
- All tables exist after first run
- Re-running doesn't error or duplicate schema
- Paper-endpoint guard rejects a live URL (unit test required)

---

### Phase 2 — X Stream and Parser

**Goal:** Bot receives X posts from the target account and parses them into structured signals.

Tasks:
1. Implement X API v2 filtered stream in `x_stream.py` for the target account handle
2. Write `parser.py` that calls Claude API with post text and returns `Signal | None`
3. Design and version the Claude parsing prompt — must handle varied post formats
4. Write `tests/test_parser.py` with synthetic post examples covering: complete signal, missing expiration, missing price, commentary-only post, ambiguous posts
5. Write all raw posts and parse results to `x_posts` table

Acceptance:
- X stream connects and receives posts during market hours
- Parser correctly extracts all fields from well-formed posts
- Parser returns `None` (not an error) for non-signal posts
- All posts written to DB within 1 second of receipt
- Test suite passes with ≥ 90% parse accuracy on synthetic examples

---

### Phase 3 — Market Data and Validator

**Goal:** Bot can fetch live market data and validate signals against real conditions.

Tasks:
1. Implement `data_service.py` with all data fetching methods
2. Implement all validation gates in `validator.py`
3. Write unit tests for every gate (pass and fail cases)
4. Integrate Polygon.io for Greeks and IV data
5. Log gate results to `signals` table for every evaluated signal

Acceptance:
- All validation gates testable with mocked market data
- Integration test: fetch live quote, Greeks, and indicators for a real SPY option contract
- Gate timing: full validation completes in <3 seconds
- All gate pass/fail results written to DB

---

### Phase 4 — Strategy Module (Fully Tested, No Live Execution)

**Goal:** `strategy.py` correctly manages position state and produces exit decisions on synthetic data.

Tasks:
1. Implement trailing stop ratchet logic
2. Implement hard exit rules (15:55 ET, DTE=1, time stop)
3. Write comprehensive unit tests:
   - Stop loss triggers correctly at -20%
   - Trailing stop ratchets at each threshold
   - Hard time exit fires at 15:55 ET
   - DTE=1 exit fires correctly
   - Stop never moves downward
4. Write `scripts/backtest_signals.py` that replays historical signals (from DB or CSV) through strategy and produces results

Acceptance:
- All unit tests pass
- Backtest on any synthetic dataset produces correct trade-by-trade results
- Strategy module has zero imports from Alpaca, X, or any I/O library

---

### Phase 5 — Risk Manager

**Goal:** Kill switches and position limits enforce correctly.

Tasks:
1. Implement `validate()` in `risk_manager.py`
2. Implement daily P&L tracker from fills table
3. Implement consecutive loss counter
4. Write unit tests for every kill switch

Acceptance:
- All kill switch unit tests pass
- Manual injection of synthetic fills shows correct kill switch behavior
- Every risk decision (taken or rejected) written to events table

---

### Phase 6 — Executor (Paper Only)

**Goal:** Validated signals become real Alpaca paper orders.

Tasks:
1. Implement order submission in `executor.py`
2. Implement order status monitoring and fill detection
3. Implement trailing stop order modification as ratchet triggers
4. Implement 15:55 ET flatten via scheduled task
5. Implement startup reconciliation against open Alpaca positions

Acceptance:
- Manual test signal creates order on Alpaca paper
- Fill detection works and writes to `fills` table
- Trailing stop modification executes correctly on paper position
- Kill the bot mid-trade, restart — reconciliation adopts open position
- 15:55 flatten leaves no open positions

---

### Phase 7 — Indicator Snapshots

**Goal:** Every active position has full indicator data captured on schedule.

Tasks:
1. Implement 15-minute snapshot scheduler in `main.py`
2. Capture all Greeks, IV, technical, volume, and market context indicators
3. Write to `indicator_snapshots` table
4. On trade close, write final snapshot with `snapshot_type = 'exit'`
5. Write to `trades` table with full summary on close

Acceptance:
- Open position accumulates snapshots every 15 minutes
- Entry and exit snapshots always present for every trade
- No missing fields — if a data source is unavailable, write null with an event log entry (don't crash)

---

### Phase 8 — FastAPI Backend + WebSocket Server

**Goal:** Real-time data available to the dashboard via WebSocket.

Tasks:
1. Implement `api/main.py` with FastAPI and WebSocket endpoint `/ws`
2. Implement `ws_manager.py` to broadcast to all connected clients
3. Wire bot state changes to broadcast queue in `main.py`
4. Implement all WebSocket events from spec (signal.received, trade.entered, etc.)
5. Implement REST endpoints for positions, signal history, performance stats

Acceptance:
- WebSocket client receives all events in real time during a simulated session
- REST endpoints return correct data from Supabase
- Multiple dashboard clients can connect simultaneously
- Client reconnection handled gracefully

---

### Phase 9 — React Dashboard

**Goal:** Full real-time dashboard running in browser.

Tasks:
1. Build React app in `dashboard/` with Vite
2. Implement `useWebSocket` hook with auto-reconnect
3. Build all five panels: StatusBar, SignalFeed, PositionCard, MarketContext, PerformanceHistory
4. Implement live P&L and Greek updates in PositionCard
5. Implement trailing stop visual in PositionCard
6. Implement trade log table with sorting and filtering in PerformanceHistory

Acceptance:
- Dashboard connects to WebSocket and receives live updates
- PositionCard updates Greeks and P&L in real time during simulated session
- Signal feed shows real-time parse and validation results
- Performance history renders correct stats from DB
- Dashboard works on both desktop and mobile

---

### Phase 10 — Deployment

**Goal:** Bot and dashboard running unattended on DigitalOcean.

Tasks:
1. Write `deploy/install.sh` for fresh Ubuntu 24.04 droplet
2. Write systemd units for bot and FastAPI server
3. Deploy React dashboard to Vercel
4. Write `README.md` as full operations runbook

Acceptance:
- Fresh droplet → `install.sh` → bot running within 20 minutes
- Both systemd services active and auto-restart on failure
- Dashboard accessible via Vercel URL
- Reboot droplet → everything comes back automatically

---

### Phase 11 — Paper Trading Runtime (30–60 Days)

**Goal:** Validate that the system works end-to-end in real market conditions.

This phase is **operational, not development.** Claude Code's job is to investigate anomalies, not change strategy code unless a clear bug is found.

Daily checks:
- Are X posts being received and parsed correctly?
- Did any kill switches trip unexpectedly?
- Are paper fills happening at reasonable prices?
- Are indicator snapshots populating for every trade?

Weekly checks:
- Win rate, avg gain/loss, profit factor on rolling window
- Parse accuracy — how many posts were actionable vs. skipped?
- Post-to-execution timing — are we fast enough?

Monthly:
- Full performance review
- Pattern analysis: which indicator combinations correlate with wins?
- Strategy hypothesis development

**Do not advance to live trading.** Live is out of scope for this build.

---

## 5. Environment Variables Required

```bash
# X API
X_BEARER_TOKEN=
X_TARGET_ACCOUNT_ID=        # The numeric account ID to monitor

# Claude API
ANTHROPIC_API_KEY=

# Alpaca (Paper Only)
ALPACA_API_KEY=
ALPACA_SECRET_KEY=
ALPACA_BASE_URL=https://paper-api.alpaca.markets

# Polygon.io (Greeks and IV data)
POLYGON_API_KEY=

# Supabase
SUPABASE_URL=
SUPABASE_KEY=
DATABASE_URL=               # Direct Postgres connection string

# Telegram
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# Bot Config (tunable)
STOP_LOSS_PCT=0.20          # Initial stop loss below entry
DAILY_LOSS_KILL_PCT=0.03    # Daily loss kill switch threshold
MAX_CONSECUTIVE_LOSSES=4    # Consecutive loss pause threshold
MAX_FILL_WAIT_SECONDS=60    # Order fill timeout
SIGNAL_STALE_SECONDS=180    # Max age of X post to act on
PRICE_DEVIATION_PCT=0.10    # Max deviation from posted price
```

---

## 6. Operating Principles for Claude Code

1. **Test before integrate.** Strategy, parser, and risk modules have unit tests before wired into `main.py`.
2. **No "TODO: handle this later" in critical paths.** Raise `NotImplementedError` if a case isn't handled — fail loudly.
3. **Money math uses `Decimal`.** Never `float`.
4. **Time math is timezone-aware.** All timestamps use `ZoneInfo("America/New_York")` or UTC in DB.
5. **Log signals before risk-checking; log risk decisions before executing.**
6. **Indicator snapshots are the long-term value.** Treat them as first-class, not afterthought logging.
7. **If any phase acceptance gate fails, STOP.** Report back. Do not tune your way through a failing gate.
8. **Commit at every phase boundary** with a tag (`phase-1-complete`, etc.).
9. **Read this document at the start of every Claude Code session.** It is the single source of truth.

---

## 7. First Claude Code Prompt

When starting the Claude Code session, paste this:

> Read `CLAUDE.md` in full before doing anything. Then execute Phase 1 only. Stop at the Phase 1 acceptance gate and report results. Do not proceed to Phase 2 without my confirmation.

---

**End of handoff document.**
*Version 1.0 — Ready for build*
