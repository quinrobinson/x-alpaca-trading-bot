# x-alpaca-trading-bot

Options trading bot that monitors a target X (Twitter) account for trade signals, validates against live market data, and executes paper trades on Alpaca with trailing-stop position management.

**Paper trading only.** `executor.py` hard-fails on startup if a non-paper Alpaca endpoint is configured.

## Quickstart

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env
# Fill in credentials (Alpaca paper, Anthropic, X, Polygon, Supabase, Telegram)

python -m x_alpaca_trading_bot.main
```

## Tests

```bash
pytest
```

## Docs

- [CLAUDE.md](CLAUDE.md) — project guide, current phase, non-negotiable rules
- [X_ALPACA_OPTIONS_HANDOFF.md](X_ALPACA_OPTIONS_HANDOFF.md) — full build spec and phase plan
- [x-alpaca-trading-bot-architecture.md](x-alpaca-trading-bot-architecture.md) — strategy logic, indicators, dashboard layout
