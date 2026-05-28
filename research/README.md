# research/

Standalone research scripts. **Read-only relative to the live bot.**

## What lives here

Scripts that explore alternative trading signals, run historical backtests,
or test market hypotheses. They are NOT part of the running bot. The
production trading system lives in `x_alpaca_trading_bot/` and `api/`
and is gated by `dashboard/`.

Current scripts:

- `backtest_momentum.py` — momentum-breakout signal (breakout above
  prior-day high). Result: no edge survives options friction.
- `backtest_failed_breakout.py` — failed-breakout / bull-trap signal
  (the put-side hypothesis). Result: top-quintile-volume events on
  small/mid-cap names show ~+0.62% put edge at to-close, -0.23%/-0.33%
  at +30m/+60m. Worth deeper investigation; not yet wired into the bot.

## Rules

1. **Never import from `x_alpaca_trading_bot/`.** Research scripts
   stand alone. If you find yourself wanting to reuse strategy logic,
   copy the function — don't import. Coupling research to production
   creates accidental dependencies and makes it impossible to delete
   a research thread without risk.

2. **Never write to the bot's tables.** Research reads `signals`,
   `trades`, `signal_price_tracks`, etc. for analysis. It never
   inserts, updates, or deletes rows in any production table. If
   research needs persistence, use a CSV or a separate `research_*`
   table.

3. **Never call Alpaca's trading endpoints.** Read-only market data
   only (`StockHistoricalDataClient`, `OptionHistoricalDataClient`).
   No `TradingClient` order submission from this directory.

4. **No `from research import ...` in production code.** The
   dependency arrow points one way: research can read public
   primitives that exist anywhere, but `x_alpaca_trading_bot/` and
   `api/` must never depend on a research file.

## Moving research to production

If a research signal proves out and we decide to integrate it into
the live bot, that's an explicit, planned change — not a drift.
The pattern is:

1. Discuss and agree on scope (new signal source? replace the tweet
   flow? run alongside?).
2. Write the production version inside `x_alpaca_trading_bot/`,
   following the existing module contracts. Reuse research code by
   copy, not import.
3. Paper-trade the new path for a meaningful period (weeks, not days)
   before declaring it production-ready.
4. Add a kill switch for the new path independent of the existing
   ones, so it can be disabled without taking the bot down.

## Tests

Unit tests for research scripts live in `tests/test_backtest_*.py`
and import via `sys.path.insert(0, ...research dir...)`. Same
read-only rule applies — tests don't touch production tables.
