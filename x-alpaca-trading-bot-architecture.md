# X → Alpaca Options Trading Bot
## Architecture & Logic Brief — Paper Trading Phase

---

## Overview

An automated pipeline that monitors a specific X account for options trade signals, validates those signals against live market conditions, executes paper trades through Alpaca, and manages positions using trailing stop logic.

---

## System Components

| Component | Role |
|---|---|
| X API (v2) | Stream posts from target account in real time |
| Claude API | Parse post content, extract signal data, validate criteria |
| Market Data API | Confirm live price, bid/ask spread, IV, contract availability |
| Alpaca Paper API | Execute trades, track positions, manage orders |
| Logging Layer | Record every decision point for post-analysis |

---

## Data Flow

```
X Post Published
      ↓
X API Stream (real-time listener)
      ↓
Post Filter (is this from target account + contains trade signal?)
      ↓
Claude Parses Post
      ↓
Signal Extracted (ticker, price, expiration, call/put)
      ↓
Market Validation Gate
      ↓
Entry Decision (execute or skip)
      ↓
Alpaca Paper Trade Placed
      ↓
Position Monitoring Loop
      ↓
Exit Trigger (stop loss, trailing stop, or profit target)
      ↓
Trade Closed + Logged
```

---

## Phase 1 — Signal Detection

**Trigger:** New post from target account appears in stream

**Filter Criteria:**
- Post is from the specific X handle being tracked
- Post contains a recognizable ticker symbol (e.g. `$AAPL`, `$TSLA`)
- Post contains a price point and/or expiration date

**Claude Parse Task:**
Extract and structure the following from the post:

```json
{
  "ticker": "AAPL",
  "option_type": "call" | "put",
  "strike_price": 185.00,
  "expiration_date": "2025-06-20",
  "entry_price": 2.50,
  "posted_at": "2025-05-11T09:32:00Z"
}
```

If any required field is missing or ambiguous → **skip and log as unactionable.**

---

## Phase 2 — Market Validation Gate

Before placing any trade, validate the signal is still actionable:

| Check | Pass Condition | Fail Action |
|---|---|---|
| Price in range | Live ask price is within X% of posted price | Skip trade, log reason |
| Time since post | Post is less than 2–3 minutes old | Skip — too late |
| Contract exists | Option contract is available on Alpaca | Skip, log |
| Bid/ask spread | Spread is not excessively wide | Flag, consider skipping |
| Market hours | Market is open | Queue or skip |

**Timing is critical.** If the live ask has already moved significantly above the posted price, the edge is gone. Default behavior: skip and log.

---

## Phase 3 — Entry Execution

If all validation gates pass:

1. Fetch the live ask price for the contract
2. Submit a limit order at or near ask through Alpaca Paper API
3. Set a max fill wait time (e.g. 30–60 seconds)
4. If not filled within window → cancel and log as missed

**Log at entry:**
- Post timestamp
- Execution timestamp (delta = how fast we got in)
- Fill price vs. posted price
- Contract details

---

## Phase 4 — Position Management

Once filled, the position enters active monitoring.

### Initial Stop Loss
- Set stop loss at **20% below fill price** (starting point — refine during paper trading)
- Example: Filled at $2.50 → stop loss at $2.00

### Trailing Stop Logic
As the position moves in your favor, the stop loss follows:

| Position Gain | Action |
|---|---|
| +10% | Move stop loss to breakeven |
| +20% | Move stop loss to +10% (lock in partial profit) |
| +25% | Consider full exit or tighten stop to +20% |
| +40%+ | Tighten aggressively, let it run with protection |

**Rule:** Stop loss only moves up. Never adjust it downward.

### Hard Exit Rules
- Stop loss hit → market order to close immediately
- Expiration within 1 day → close position regardless of P&L
- Position open for X hours with no movement → evaluate and close

---

## Phase 5 — Exit Execution

**Triggers:**
1. Stop loss order filled (Alpaca handles automatically)
2. Profit target reached → system places limit sell
3. Manual override (for edge cases during paper trading)

**Log at exit:**
- Exit price
- P&L (dollar and percentage)
- Hold duration
- Exit reason (stop loss / profit target / time-based / manual)

---

## Indicators to Track Per Trade

Every trade — entry, mid-position, and exit — logs the following indicators. Over time this dataset becomes the foundation for building an independent strategy.

---

### Greeks (Options-Specific)

| Greek | What It Tells You |
|---|---|
| **Delta** | How much the option price moves per $1 move in the underlying. High delta = more sensitive to price. Target range for entries: 0.40–0.70 for directional plays |
| **Gamma** | Rate of change of delta. High gamma near expiration = fast-moving, higher risk |
| **Theta** | Time decay per day. Options lose value daily — theta tells you how much. Key for understanding hold duration |
| **Vega** | Sensitivity to implied volatility changes. High vega = IV crush risk after events like earnings |
| **Rho** | Interest rate sensitivity. Less relevant for short-term plays but worth logging |

---

### Implied Volatility Indicators

| Indicator | What It Tells You |
|---|---|
| **IV (Implied Volatility)** | Market's expectation of future price movement. High IV = expensive options, higher risk of IV crush |
| **IV Rank (IVR)** | Where current IV sits relative to its 52-week range (0–100). Below 30 = cheap options, above 70 = expensive |
| **IV Percentile** | Similar to IVR but based on % of days IV was lower. Helps confirm whether options are over or underpriced |

---

### Technical Indicators (Underlying Stock)

| Indicator | What It Tells You |
|---|---|
| **RSI (14)** | Momentum oscillator 0–100. Above 70 = overbought, below 30 = oversold. Helps confirm entry direction |
| **MACD** | Trend and momentum. MACD line crossing signal line = potential momentum shift |
| **VWAP** | Volume-weighted average price. Price above VWAP = bullish bias, below = bearish. Critical for intraday entries |
| **Bollinger Bands** | Volatility bands around a moving average. Price touching upper band on a call = potentially overextended |
| **EMA 9 / EMA 21** | Short-term exponential moving averages. EMA 9 crossing above EMA 21 = bullish momentum signal |
| **ATR (Average True Range)** | Measures volatility of the underlying. Helps calibrate stop loss sizing relative to how much the stock typically moves |

---

### Volume & Market Structure Indicators

| Indicator | What It Tells You |
|---|---|
| **Options Volume** | High volume relative to open interest = unusual activity, confirms interest in the contract |
| **Open Interest (OI)** | Total outstanding contracts. Rising OI + rising price = strong trend confirmation |
| **Put/Call Ratio** | Market sentiment. High put/call = bearish sentiment, low = bullish. Useful as a contrarian signal |
| **Unusual Options Activity (UOA)** | Large block trades, sweeps, or high volume relative to OI. Often signals informed money moving |
| **Bid/Ask Spread %** | Wide spread = illiquid contract, harder to enter and exit cleanly. Flag anything over 10% of mid price |

---

### Market Context Indicators

| Indicator | What It Tells You |
|---|---|
| **VIX** | Market fear index. High VIX = volatile market, options are expensive. Low VIX = calm market, options cheaper |
| **SPY/QQQ Trend** | Overall market direction at time of entry. Trading with the market trend improves win rate |
| **Sector ETF Trend** | Is the stock's sector moving in the same direction as the trade? Confirmation matters |
| **Pre/Post Market Movement** | Significant gap ups or downs can invalidate a setup before the open |
| **Upcoming Catalyst** | Earnings, Fed announcements, CPI — any known event that could cause IV crush or violent moves |

---

### Snapshot Timing — When to Log

| Moment | What to Capture |
|---|---|
| **At signal detection** | RSI, MACD, VWAP, VIX, SPY trend, IV, IVR, OI, volume |
| **At entry fill** | All Greeks, fill price vs. mid, bid/ask spread, ATR |
| **Every 15 minutes while open** | Delta, theta, current P&L, RSI, VWAP relationship |
| **At exit** | Full Greeks snapshot, exit reason, final P&L, hold duration |

---

## Long-Term Strategy Development

The 30–60 day paper trading phase is Phase 1. The real goal is building an independent edge from the data collected.

### Phase 2 — Pattern Analysis (Month 2–3)
- Query the logged dataset to find which indicator combinations produced the highest win rates
- Example questions: "What was the win rate when IVR was below 40 at entry?" or "Did trades with delta above 0.60 outperform lower delta entries?"
- Identify the trader's signal patterns — what setups does he tend to post? Are there recurring conditions?

### Phase 3 — Strategy Hypothesis (Month 3–4)
- Build 2–3 hypothesis strategies based purely on the data, independent of the X signal
- Define entry criteria using a combination of indicators (e.g. RSI below 40 + VWAP reclaim + IVR below 30 + delta 0.50)
- Define exit rules for each strategy

### Phase 4 — Independent Paper Trading (Month 4–6)
- Run the hypothesis strategies in parallel with the X signal bot
- Compare performance: does your own signal outperform, underperform, or complement the X-based trades?

### Phase 5 — Live Trading Decision
- If paper results are consistently profitable over 60+ days with your own strategy, consider transitioning to live with small position sizes
- X signal bot can continue running as a secondary confirmation layer

---

## Paper Trading Metrics to Track (30–60 Days)

| Metric | Why It Matters |
|---|---|
| Signal hit rate | How often does the post translate to a valid, fillable trade? |
| Fill slippage | How far off posted price are we actually getting in? |
| Win rate | % of trades that hit profit target vs. stop loss |
| Average gain / average loss | Risk/reward ratio in practice |
| Time to fill | Are we fast enough to catch the entry? |
| Post-to-execution delta | How many minutes between post and our fill? |

---

## Open Questions to Resolve During Paper Trading

- [ ] What % price deviation makes a signal stale? (Starting assumption: 5–10%)
- [ ] What's the right stop loss % at entry? (Starting assumption: 20%)
- [ ] What trailing stop increment makes sense? (Starting assumption: 10% steps)
- [ ] How many contracts per trade for paper testing?
- [ ] What's the max daily loss limit before the system pauses?
- [ ] Should we track the trader's posts that we skipped to see if they would have won?

---

## Dashboard Specification

### Overview

A real-time web dashboard that gives you full visibility into the bot's state — incoming signals, active positions, live indicators, and historical performance — without needing to query the database directly.

**Delivery:** React frontend + Python FastAPI backend with WebSocket support. Deployed alongside the bot on the same server.

---

### Real-Time Data Architecture

```
Alpaca WebSocket  ──┐
X Stream Listener ──┤──→ FastAPI Backend ──→ WebSocket Server ──→ React Dashboard
Postgres DB       ──┘         ↑
                              │
                         Scheduler
                      (Greeks, indicators
                       snapshot every 15s)
```

- Backend maintains persistent connections to Alpaca and the X stream
- On any state change (new signal, fill, P&L update, indicator refresh), backend broadcasts to all connected dashboard clients via WebSocket
- Dashboard never polls — it receives pushes

---

### Dashboard Layout — Five Panels

#### Panel 1 — System Status Bar (Top)
Always visible. Shows bot health at a glance.

| Element | Description |
|---|---|
| Bot status | Running / Paused / Kill switch active |
| Market status | Open / Pre-market / After-hours / Closed |
| X stream status | Connected / Disconnected |
| Alpaca connection | Connected / Disconnected |
| Today's P&L | Live dollar and % value, color-coded green/red |
| Daily loss limit | Progress bar showing proximity to -X% kill switch |
| Last signal received | Timestamp of most recent X post parsed |

---

#### Panel 2 — Signal Feed (Left column)
Live stream of all incoming X posts from the target account.

Each entry shows:
- Post timestamp
- Raw post text (truncated)
- Parse result: ✅ Actionable / ⚠️ Incomplete / ❌ Skipped
- If actionable: extracted ticker, option type, strike, expiration, price
- Validation gate result: Pass / Fail + reason
- Execution result: Filled / Missed / Rejected

Color coding:
- Green = trade taken
- Yellow = parsed but skipped (price stale, validation failed)
- Gray = not a trade signal

---

#### Panel 3 — Active Positions (Center, largest panel)
One card per open position. Updates in real time.

Each position card shows:

**Header:** Ticker + Strike + Expiration + Call/Put + Side

**P&L section:**
- Entry price
- Current price
- P&L in dollars and %
- Progress bar from stop loss → entry → profit target

**Greeks (live, updating every 15s):**
- Delta / Gamma / Theta / Vega
- Color-coded: theta going red as it grows, delta shifting as price moves

**Indicators (underlying stock, live):**
- RSI — with overbought/oversold zones highlighted
- VWAP relationship (above / below)
- ATR (current volatility context)
- IV and IVR

**Stop loss tracker:**
- Current stop loss level
- Trailing stop history (visual step chart showing how stop has ratcheted up)
- Next trailing stop trigger level

**Time:**
- Time in trade
- Time to expiration
- Time to 15:55 ET hard close

---

#### Panel 4 — Market Context (Right column)
Broader market indicators, always visible.

| Indicator | Display |
|---|---|
| VIX | Current value + 5-day sparkline |
| SPY price + trend | Price, EMA 9/21 relationship |
| QQQ price + trend | Price, EMA 9/21 relationship |
| Put/Call Ratio | Current value, bullish/bearish label |
| Sector heatmap | Color-coded sector ETF performance for the day |

---

#### Panel 5 — Performance History (Bottom)
Rolling stats from the paper trading session.

| Metric | Display |
|---|---|
| Total trades | Count |
| Win rate | % with trend line |
| Average gain | $ and % |
| Average loss | $ and % |
| Profit factor | Ratio |
| Best trade | Details |
| Worst trade | Details |
| Cumulative P&L chart | Line chart, daily granularity |
| Trade log table | Sortable/filterable table of all trades with full indicator snapshot |

---

### WebSocket Events (Backend → Frontend)

| Event | Trigger | Payload |
|---|---|---|
| `signal.received` | New X post parsed | Post text, parse result |
| `signal.validated` | Validation gate completed | Pass/fail, reasons |
| `trade.entered` | Fill confirmed | Position details, entry Greeks |
| `trade.updated` | Greeks/indicator refresh | Full snapshot |
| `trade.stop_moved` | Trailing stop ratcheted | Old stop, new stop, trigger |
| `trade.exited` | Position closed | Exit details, P&L, reason |
| `killswitch.tripped` | Kill switch activated | Which switch, current P&L |
| `market.status` | Market open/close | Status change |
| `system.heartbeat` | Every 30s | Bot health, connection status |

---

### Tech Stack (Updated)

| Layer | Tool |
|---|---|
| Language | Python 3.12 |
| X Streaming | Tweepy or X API v2 filtered stream |
| Signal Parsing | Claude API (claude-sonnet-4-20250514) |
| Market Data | Alpaca Market Data API + Polygon.io (Greeks, IV) |
| Trade Execution | Alpaca Paper Trading API |
| Backend API | FastAPI with WebSocket support |
| Database | Supabase (Postgres) |
| Dashboard Frontend | React + Recharts (charts) + Tailwind CSS |
| Deployment | DigitalOcean droplet (bot + backend) + Vercel (frontend) |

---

## Next Steps

1. Set up X API filtered stream for target account
2. Build and test Claude parsing prompt with sample posts (share examples when ready)
3. Connect Alpaca Paper API and confirm auth
4. Build validation gate logic
5. Run first end-to-end dry run — logging only, no execution — confirm parsing works
6. Enable paper trade execution
7. Build FastAPI backend with WebSocket server
8. Build React dashboard
9. Monitor daily for 30–60 days

---

*Version 0.3 — Added Real-Time Dashboard Specification*
*Status: Ready for Claude Code handoff*
