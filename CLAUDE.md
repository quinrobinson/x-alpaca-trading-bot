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
| Current phase | Phase 2 — parser + X stream built and unit-tested; live X connect + DB write deferred |
| Last completed phase | None tagged yet — Phase 1 DB gates 3–5 and Phase 2 DB/stream gates pending DATABASE_URL and X creds |
| Last session date | 2026-05-12 |
| Open issues | DATABASE_URL empty; X_BEARER_TOKEN + X_TARGET_ACCOUNT_ID empty. Both phases have deferred gates waiting on these. |
| Next action | Either (a) provision Postgres + fill DB env to verify Phase 1/2 DB gates and tag, or (b) proceed to Phase 3 (data_service + validator) |

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
├── scripts/                   # Backtest and utility scripts
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
| Deployment | DigitalOcean (bot + API) + Vercel (dashboard) |
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

**Trailing stop ratchet:**
- +10% gain → stop to breakeven
- +20% gain → stop to +10%
- +25% gain → stop to +20%
- +40%+ gain → stop to +30%, tighten aggressively

**Hard exits:**
- Stop loss hit → immediate market order
- 15:55 ET → flatten everything
- DTE = 1 → close regardless of P&L
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

---

*Project: x_alpaca_trading_bot*
*Owner: [Your name]*
*Paper trading only — no live capital*
