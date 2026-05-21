-- x-alpaca-trading-bot — Postgres schema (Phase 1)
-- Idempotent: every CREATE uses IF NOT EXISTS, so this file can be applied
-- on every startup without error.
--
-- Reference: X_ALPACA_OPTIONS_HANDOFF.md §3

-- ============================================================
-- Raw X posts (every post received, actionable or not)
-- ============================================================
CREATE TABLE IF NOT EXISTS x_posts (
    id              BIGSERIAL PRIMARY KEY,
    posted_at       TIMESTAMPTZ NOT NULL,
    received_at     TIMESTAMPTZ NOT NULL,
    post_id         TEXT UNIQUE NOT NULL,
    post_text       TEXT NOT NULL,
    parse_result    JSONB,                              -- null if not a trade signal
    actionable      BOOLEAN NOT NULL DEFAULT false
);

-- ============================================================
-- Signals (parsed and validated trade signals)
-- ============================================================
CREATE TABLE IF NOT EXISTS signals (
    id              BIGSERIAL PRIMARY KEY,
    x_post_id       BIGINT REFERENCES x_posts(id),
    parsed_at       TIMESTAMPTZ NOT NULL,
    ticker          TEXT NOT NULL,
    option_type     TEXT NOT NULL,                      -- 'call' or 'put'
    strike          NUMERIC(10, 2) NOT NULL,
    expiration      DATE NOT NULL,
    posted_price    NUMERIC(10, 4) NOT NULL,
    live_ask        NUMERIC(10, 4),                     -- at time of validation
    taken           BOOLEAN NOT NULL,
    rejection_reason TEXT,
    gate_results    JSONB NOT NULL                      -- each gate pass/fail
);

-- ============================================================
-- Orders
-- ============================================================
CREATE TABLE IF NOT EXISTS orders (
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

-- ============================================================
-- Fills
-- ============================================================
CREATE TABLE IF NOT EXISTS fills (
    id              BIGSERIAL PRIMARY KEY,
    order_id        BIGINT REFERENCES orders(id),
    filled_at       TIMESTAMPTZ NOT NULL,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL,
    qty             INTEGER NOT NULL,
    fill_price      NUMERIC(10, 4) NOT NULL,
    commission      NUMERIC(8, 4) NOT NULL DEFAULT 0
);

-- ============================================================
-- Indicator snapshots (entry, every 15min, exit)
-- ============================================================
CREATE TABLE IF NOT EXISTS indicator_snapshots (
    id              BIGSERIAL PRIMARY KEY,
    signal_id       BIGINT REFERENCES signals(id),
    ts              TIMESTAMPTZ NOT NULL,
    snapshot_type   TEXT NOT NULL,                      -- 'entry', 'monitor', 'exit'
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
    bb_position     NUMERIC(8, 4),                      -- 0-1, position within bands
    -- Volume/structure
    options_volume  BIGINT,
    open_interest   BIGINT,
    put_call_ratio  NUMERIC(6, 4),
    bid_ask_spread_pct NUMERIC(6, 4),
    -- Market context
    vix             NUMERIC(8, 4),
    spy_vs_ema21    TEXT,                               -- 'above' or 'below'
    sector_etf_trend TEXT,
    -- Option price
    option_bid      NUMERIC(10, 4),
    option_ask      NUMERIC(10, 4),
    option_mid      NUMERIC(10, 4),
    underlying_price NUMERIC(12, 4)
);
CREATE INDEX IF NOT EXISTS idx_snapshots_signal_ts ON indicator_snapshots (signal_id, ts);

-- ============================================================
-- Trades (closed positions with full summary)
-- ============================================================
CREATE TABLE IF NOT EXISTS trades (
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
    exit_reason     TEXT NOT NULL,                      -- 'stop_loss', 'profit_target', 'time_stop', 'manual'
    hold_minutes    INTEGER NOT NULL,
    max_gain_pct    NUMERIC(8, 4),                      -- peak unrealized gain during hold
    max_loss_pct    NUMERIC(8, 4)                       -- trough unrealized loss during hold
);

-- ============================================================
-- P&L snapshots (every minute during market hours)
-- ============================================================
CREATE TABLE IF NOT EXISTS pnl_snapshots (
    ts              TIMESTAMPTZ PRIMARY KEY,
    equity          NUMERIC(14, 4) NOT NULL,
    day_pnl         NUMERIC(14, 4) NOT NULL,
    open_positions  JSONB NOT NULL,
    kill_switch_state JSONB NOT NULL
);

-- ============================================================
-- System events
-- ============================================================
CREATE TABLE IF NOT EXISTS events (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL,
    severity        TEXT NOT NULL,
    category        TEXT NOT NULL,
    message         TEXT NOT NULL,
    context         JSONB
);

-- ============================================================
-- Bot config (single-row, edited at runtime via /config endpoint)
-- ============================================================
-- One row, id=1 enforced. Read at the start of every signal so changes
-- made through the dashboard apply without restarting the service.
CREATE TABLE IF NOT EXISTS bot_config (
    id                     INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    max_position_spend_usd NUMERIC(12, 2) NOT NULL DEFAULT 500.00,
    max_qty_per_position   INTEGER        NOT NULL DEFAULT 10,
    daily_loss_kill_pct    NUMERIC(6, 4)  NOT NULL DEFAULT 0.03,
    disable_x_stream       BOOLEAN        NOT NULL DEFAULT FALSE,
    updated_at             TIMESTAMPTZ    NOT NULL DEFAULT NOW()
);

INSERT INTO bot_config (id) VALUES (1)
ON CONFLICT (id) DO NOTHING;

-- ============================================================
-- Signal price tracks — post-signal option-price movement study
-- ============================================================
-- For every signal (taken OR rejected), the orchestrator records the
-- option's mid price at fixed offsets after the bot received it. This
-- is the dataset that answers "is there capturable move after the
-- tweet" — i.e. whether copy-trading this source can work at all.
-- One row per (signal, offset); UNIQUE makes the capture idempotent.
CREATE TABLE IF NOT EXISTS signal_price_tracks (
    id              BIGSERIAL PRIMARY KEY,
    signal_id       BIGINT NOT NULL REFERENCES signals(id),
    offset_minutes  INTEGER NOT NULL,            -- 1, 5, 15, 30
    captured_at     TIMESTAMPTZ NOT NULL,
    option_mid      NUMERIC(10, 4),              -- null if no quote available
    UNIQUE (signal_id, offset_minutes)
);
CREATE INDEX IF NOT EXISTS idx_price_tracks_signal ON signal_price_tracks (signal_id);
