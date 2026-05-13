# dashboard

React + Vite + Tailwind v4 dashboard for the x-alpaca-trading-bot.

Five panels per the architecture brief:
- **StatusBar** — bot/market/X/Alpaca/WS health, today's P&L, daily-loss-limit progress bar
- **SignalFeed** — live X-post stream with parse + validation status
- **PositionCard** — open positions with live P&L, Greeks, indicators, stop tracker
- **MarketContext** — VIX, SPY trend, sector heatmap
- **PerformanceHistory** — sortable trade log, stats, cumulative-P&L sparkline

## Run locally

The FastAPI backend must be running on `localhost:8000` (Vite proxies `/healthz`, `/positions`, `/signals`, `/performance`, `/ws` to it).

```bash
# In one terminal — the bot + API:
cd ..
.venv/bin/uvicorn api.main:build_production_app --factory --reload

# In another — the dashboard:
cd dashboard
npm install
npm run dev
# → http://localhost:5173
```

## Test

```bash
npm test
```

## Build for production

```bash
npm run build
# → dist/ is a static bundle, ready for any static host (Vercel, S3, nginx).
```

## Layout

```
src/
├── App.jsx
├── main.jsx
├── index.css
├── util.js
├── components/
│   ├── StatusBar.jsx
│   ├── SignalFeed.jsx
│   ├── PositionCard.jsx
│   ├── MarketContext.jsx
│   └── PerformanceHistory.jsx
└── hooks/
    ├── useWebSocket.js
    └── useWebSocket.test.js
```

## Stack notes

- **Tailwind v4** is configured via `@tailwindcss/vite` plus `@import "tailwindcss"` in `index.css`. No `tailwind.config.js` needed.
- **`useWebSocket`** does its own auto-reconnect with exponential backoff (500ms → 8s cap, resets on successful open).
- **REST + WS combo:** `/positions` and `/performance` are polled every 30s. WS events trigger an immediate re-fetch of affected endpoints so the UI updates instantly without waiting for the next poll.
- **No external chart lib** for v1; the equity-curve sparkline is hand-rolled SVG. Add Recharts later if needed.
