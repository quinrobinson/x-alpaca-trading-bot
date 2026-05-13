# Production setup runbook

End-to-end deploy: Supabase (DB) + existing DigitalOcean droplet (bot+API) + Vercel (dashboard).

Estimated time: **30–45 minutes** the first time.

---

## 0. Prerequisites

- An Alpaca **paper** account with API key + secret
- An Anthropic API key (for the post parser)
- A Polygon.io API key (any plan that includes options snapshots)
- An X (Twitter) developer account with bearer token + numeric account id of the trader you want to follow
- A Supabase account (free tier is fine)
- An existing Ubuntu 22.04 or 24.04 DigitalOcean droplet with SSH access
- A Vercel account

You do **not** need a domain name. Vercel gives you `*.vercel.app` for free; the droplet's public IP works as the API base.

---

## 1. Supabase — provision the database

1. Sign in to <https://supabase.com> → **New project**.
2. Name: `x-alpaca-trading-bot`. Region: closest to your droplet. Set a strong DB password (store in 1Password / similar).
3. Wait ~2 minutes for the project to spin up.
4. **Project Settings → Database → Connection string → URI (Session pooler is fine).**
   Copy the connection string. It looks like:
   ```
   postgresql://postgres.[ref]:[password]@aws-0-[region].pooler.supabase.com:6543/postgres
   ```
   *Save this — it's your `DATABASE_URL` in step 3.*
5. Open **SQL Editor**, paste the contents of [`deploy/postgres_setup.sql`](./postgres_setup.sql), run. You should see "Success. No rows returned." 8 tables now exist; verify under **Table Editor**.

Smoke test from your laptop:
```bash
psql "postgresql://postgres.[ref]:[password]@aws-0-[region].pooler.supabase.com:6543/postgres" -c "\dt"
```
You should see all 8 tables (`x_posts`, `signals`, `orders`, `fills`, `indicator_snapshots`, `trades`, `pnl_snapshots`, `events`).

---

## 2. Droplet — install the bot + API

SSH into the droplet as a user with `sudo`:

```bash
# 1) Clone the repo somewhere temporary so we have the install script.
git clone https://github.com/YOURUSER/x-alpaca-trading-bot.git /tmp/x-alpaca-bot-clone
cd /tmp/x-alpaca-bot-clone

# 2) Run the installer. It will:
#    - install python3.12 if missing
#    - create the xalpaca system user
#    - clone the repo into /opt/x-alpaca-trading-bot
#    - build the venv + pip install
#    - write the systemd unit
sudo REPO_URL=https://github.com/YOURUSER/x-alpaca-trading-bot.git \
     API_PORT=8000 \
     bash deploy/install.sh
```

If port **8000** is in use on the droplet, pass a different one:
```bash
sudo REPO_URL=... API_PORT=8001 bash deploy/install.sh
```

The installer prints a next-steps block. Follow it:

```bash
# 3) Create the env file
sudo -u xalpaca cp /opt/x-alpaca-trading-bot/.env.example /opt/x-alpaca-trading-bot/.env
sudo -u xalpaca chmod 600 /opt/x-alpaca-trading-bot/.env
sudo -u xalpaca nano /opt/x-alpaca-trading-bot/.env
```

Fill in every required value. The critical ones:

| Var | Source |
|---|---|
| `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` | Alpaca paper account dashboard |
| `ALPACA_BASE_URL` | must be `https://paper-api.alpaca.markets` (anything else hard-fails) |
| `ANTHROPIC_API_KEY` | console.anthropic.com |
| `POLYGON_API_KEY` | polygon.io account |
| `X_BEARER_TOKEN` | developer.x.com (Project & Apps → bearer token) |
| `X_TARGET_ACCOUNT_ID` | the **numeric** id (use a username→id lookup tool; NOT the @handle) |
| `DATABASE_URL` | the Supabase connection string from step 1.4 |
| `SUPABASE_URL`, `SUPABASE_KEY` | Supabase project settings → API |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | optional; leave blank to disable alerts |
| `CORS_ORIGINS` | your Vercel dashboard URL once you have it (step 3.5) |

Then start it:

```bash
sudo systemctl start x-alpaca-bot
sudo journalctl -u x-alpaca-bot -f
```

You should see the orchestrator boot up, run migrations idempotently, attach to the X stream, and tick. Press Ctrl+C to detach from the log.

Sanity check the API:
```bash
curl http://localhost:8000/healthz
# → {"ok": true, ...}
```

If you have **Caddy or nginx** on this droplet, add a reverse-proxy block now so the API is reachable over HTTPS. Sample Caddyfile:
```
api.your-domain.com {
    reverse_proxy localhost:8000
}
```

If you **don't** have a reverse proxy and don't want TLS yet, you can hit the API directly at `http://YOUR_DROPLET_IP:8000`. Open the port in your droplet firewall (DO web UI or `ufw allow 8000/tcp`). **TLS warning:** Vercel will be `https://`, so it can't talk to a `http://` API from the browser (mixed content blocked). For Phase 10 dev/testing you can grant the dashboard an exception, but **you'll need TLS before going operational in Phase 11.** Caddy makes this a one-liner if you have a domain.

---

## 3. Vercel — deploy the dashboard

1. Push the repo to GitHub if it isn't already.
2. Sign in to <https://vercel.com> → **Add New → Project**. Select the repo.
3. Vercel will auto-detect Vite. Override the **Root Directory** to `dashboard/`.
4. **Environment Variables → Add**:
   - Name: `VITE_API_BASE`
   - Value: `https://api.your-domain.com` (if you set up Caddy/nginx + a domain)
     or `http://YOUR_DROPLET_IP:8000` (if you skipped TLS — see caveat above)
5. Click **Deploy**. First build takes ~30s.
6. Vercel gives you a URL like `https://x-alpaca-bot.vercel.app`. Copy it.
7. **Back on the droplet**, add the Vercel URL to your CORS allow-list:
   ```bash
   sudo -u xalpaca nano /opt/x-alpaca-trading-bot/.env
   # Edit/add:
   #   CORS_ORIGINS=https://x-alpaca-bot.vercel.app
   sudo systemctl restart x-alpaca-bot
   ```

---

## 4. Verification

In a browser, open `https://x-alpaca-bot.vercel.app`.

You should see:
- **StatusBar** at top: bot=running, market=open/closed, WS=open, Alpaca=connected. P&L $0.00, daily-loss bar empty.
- **Signal feed**: "No signals yet."
- **Center column**: "No open positions."
- **Market context**: VIX may show "—" (Polygon plan dependent) — that's fine.
- **Performance history**: 0 trades.

In the browser dev tools **Network** tab you should see successful 200s on `/healthz`, `/positions`, `/signals`, `/performance` and a `101 Switching Protocols` for `/ws`.

From the droplet:
```bash
sudo journalctl -u x-alpaca-bot -f
```
You should see periodic `risk_check_passed` info events and `system.heartbeat` log lines.

Reboot test (recommended):
```bash
sudo reboot
# wait 30s, SSH back in, then:
sudo systemctl status x-alpaca-bot
# → active (running) within a minute of reboot
curl http://localhost:8000/healthz
```

---

## 5. Operations

### Update the bot

```bash
sudo bash /opt/x-alpaca-trading-bot/deploy/install.sh --update
# → pulls, reinstalls deps, restarts the service.
```

### Tail logs

```bash
sudo journalctl -u x-alpaca-bot -f
sudo journalctl -u x-alpaca-bot --since '10 min ago'
```

### Restart cleanly

```bash
sudo systemctl restart x-alpaca-bot
```

### Stop for maintenance

```bash
sudo systemctl stop x-alpaca-bot
```

Note: stopping the service does NOT flatten open positions. If positions are open and you want them closed first, hit the 15:55 ET window or manually close on Alpaca's web UI before stopping.

### Inspect DB state quickly

From the droplet (or your laptop) using the Supabase connection string:

```bash
psql "$DATABASE_URL" <<'SQL'
SELECT count(*) AS posts FROM x_posts;
SELECT count(*) AS signals, sum(case when taken then 1 else 0 end) AS taken FROM signals;
SELECT count(*) AS open_orders FROM orders WHERE status IN ('new','accepted','partially_filled');
SELECT exit_reason, count(*) FROM trades GROUP BY exit_reason;
SQL
```

### Manual cleanup if positions get orphaned

If the orchestrator crashes mid-trade leaving an Alpaca position with no matching record:

```bash
# Inspect:
sudo -u xalpaca /opt/x-alpaca-trading-bot/.venv/bin/python -c "
from x_alpaca_trading_bot.config import Config
from x_alpaca_trading_bot import executor
cfg = Config.load()
ex = executor.Executor(
    alpaca_api_key=cfg.alpaca_api_key,
    alpaca_secret_key=cfg.alpaca_secret_key,
    alpaca_base_url=cfg.alpaca_base_url,
)
for p in ex.list_open_positions():
    print(p)
"

# Flatten everything (paper-only, but still confirm before running):
sudo -u xalpaca /opt/x-alpaca-trading-bot/.venv/bin/python -c "
from x_alpaca_trading_bot.config import Config
from x_alpaca_trading_bot import executor
cfg = Config.load()
ex = executor.Executor(
    alpaca_api_key=cfg.alpaca_api_key,
    alpaca_secret_key=cfg.alpaca_secret_key,
    alpaca_base_url=cfg.alpaca_base_url,
)
print(ex.flatten_all())
"
```

### Rotate the Anthropic / Alpaca / Polygon keys

```bash
sudo -u xalpaca nano /opt/x-alpaca-trading-bot/.env
sudo systemctl restart x-alpaca-bot
```

The bot reloads the env on restart. No code change needed.

---

## 6. Acceptance checklist (Phase 10 spec)

- [ ] Fresh droplet → `install.sh` → bot running within 20 minutes
- [ ] Both systemd services active and auto-restart on failure
       *(we ship one combined unit; the spec assumed two, this is the same idea)*
- [ ] Dashboard accessible via Vercel URL
- [ ] Reboot droplet → everything comes back automatically

---

## Troubleshooting

**`systemctl start x-alpaca-bot` exits with code 1.**
Tail the logs (`journalctl -u x-alpaca-bot -n 50`). Most likely cause: `.env` is missing a required var. The bot fails loudly on startup if any required env var is empty.

**Dashboard loads but shows everything as "—".**
Check the Network tab. If `/healthz` returns 200 but `/positions` etc. CORS-fail, you forgot to add the Vercel origin to `CORS_ORIGINS` and restart the bot. If `/ws` shows `failed`, your reverse proxy isn't upgrading the connection — Caddy's `reverse_proxy` handles it automatically; nginx needs `proxy_http_version 1.1` + `Upgrade` headers.

**Bot keeps tripping `alpaca_disconnected`.**
The orchestrator considers Alpaca "down" if no successful API call in 60s. If you're outside market hours this is expected during reduced quote traffic. Set the `is_market_open` check to True in the orchestrator's `_build_session_state` via Alpaca's clock — already the default; if you're seeing this during market hours, check Alpaca's status page.

**Polygon snapshots returning empty (`vix=None`, `greeks=None`).**
Your Polygon plan probably doesn't include real-time options data. Snapshot Greeks need the Options Starter+ plan or higher. The bot still functions — Greeks just land as NULL in `indicator_snapshots`.

**Dashboard WebSocket disconnects every few seconds.**
If you don't have a reverse proxy + TLS, browsers may block ws:// from https:// pages. Either:
- Open the Vercel URL with a self-signed exception, or
- Set up Caddy + a domain (10 minutes) and use wss://
