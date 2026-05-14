# Production setup runbook

End-to-end deploy: Supabase (DB) + existing DigitalOcean droplet (bot + API + dashboard) + Cloudflare Tunnel + Cloudflare Access.

Estimated time: **40–60 minutes** the first time.

The dashboard is served by FastAPI at the same origin as the API. There is no Vercel; one origin means one Cloudflare Access policy gates the whole surface in a single login.

---

## 0. Prerequisites

- An Alpaca **paper** account with API key + secret
- An Anthropic API key (for the post parser)
- A Polygon.io API key (any plan that includes options snapshots)
- An X (Twitter) developer account with bearer token + numeric account id of the trader you want to follow
- A Supabase account (free tier is fine)
- An existing Ubuntu 22.04 or 24.04 DigitalOcean droplet with SSH access
- A Cloudflare account with a domain on it (used for the Tunnel hostname + Access policy)

---

## 1. Supabase — provision the database

1. Sign in to <https://supabase.com> → **New project**.
2. Name: `x-alpaca-trading-bot`. Region: closest to your droplet. Set a strong DB password.
3. Wait ~2 minutes for the project to spin up.
4. **Project Settings → Database → Connection string → URI (Session pooler is fine).** Copy the connection string:
   ```
   postgresql://postgres.[ref]:[password]@aws-0-[region].pooler.supabase.com:6543/postgres
   ```
   *Save this — it's your `DATABASE_URL` in step 2.*
5. Open **SQL Editor**, paste the contents of [`deploy/postgres_setup.sql`](./postgres_setup.sql), run. You should see "Success. No rows returned." 8 tables now exist; verify under **Table Editor**.

Smoke test from your laptop:
```bash
psql "postgresql://postgres.[ref]:[password]@aws-0-[region].pooler.supabase.com:6543/postgres" -c "\dt"
```

---

## 2. Droplet — install the bot, API, and dashboard

SSH into the droplet as a user with `sudo`:

```bash
git clone https://github.com/YOURUSER/x-alpaca-trading-bot.git /tmp/x-alpaca-bot-clone
cd /tmp/x-alpaca-bot-clone

sudo REPO_URL=https://github.com/YOURUSER/x-alpaca-trading-bot.git \
     API_PORT=8000 \
     bash deploy/install.sh
```

The installer:
- installs `python3.12` and Node 20 (Vite 7 requires Node ≥ 20)
- creates the `xalpaca` system user
- clones the repo into `/opt/x-alpaca-trading-bot`
- builds the Python venv and pip-installs
- builds the dashboard (`npm ci && npm run build` → `dashboard/dist/`)
- writes the systemd unit

If port **8000** is in use, pass a different one: `sudo API_PORT=8001 bash deploy/install.sh`.

Then fill in `.env` and start the service:

```bash
sudo -u xalpaca cp /opt/x-alpaca-trading-bot/.env.example /opt/x-alpaca-trading-bot/.env
sudo -u xalpaca chmod 600 /opt/x-alpaca-trading-bot/.env
sudo -u xalpaca nano /opt/x-alpaca-trading-bot/.env
```

| Var | Source |
|---|---|
| `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` | Alpaca paper account dashboard |
| `ALPACA_BASE_URL` | must be `https://paper-api.alpaca.markets` |
| `ANTHROPIC_API_KEY` | console.anthropic.com |
| `POLYGON_API_KEY` | polygon.io account |
| `X_BEARER_TOKEN` | developer.x.com |
| `X_TARGET_ACCOUNT_ID` | the **numeric** id of the trader to follow |
| `DATABASE_URL` | the Supabase connection string from step 1.4 |
| `SUPABASE_URL`, `SUPABASE_KEY` | Supabase project settings → API |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | optional |
| `CORS_ORIGINS` | unused now that the dashboard is same-origin; leave default or unset |

```bash
sudo systemctl start x-alpaca-bot
sudo journalctl -u x-alpaca-bot -f
```

Sanity check the API and dashboard locally:
```bash
curl http://localhost:8000/healthz          # → {"ok": true, ...}
curl -I http://localhost:8000/              # → 200, text/html (the SPA)
```

---

## 3. Cloudflare Tunnel — expose the droplet

Either reuse an existing tunnel or create a new one. Assuming you have `cloudflared` installed and authenticated:

```bash
cloudflared tunnel create x-alpaca-bot
cloudflared tunnel route dns x-alpaca-bot x-alpaca-bot.your-zone.dev
```

Edit `/etc/cloudflared/config.yml` (or the tunnel's config file) to route the hostname to `localhost:8000`:

```yaml
tunnel: <TUNNEL_ID>
credentials-file: /root/.cloudflared/<TUNNEL_ID>.json
ingress:
  - hostname: x-alpaca-bot.your-zone.dev
    service: http://localhost:8000
  - service: http_status:404
```

`cloudflared` supports WebSocket upgrades transparently — `/ws` Just Works.

```bash
sudo systemctl restart cloudflared
```

Open `https://x-alpaca-bot.your-zone.dev` in a browser. You should see the dashboard. **It is currently public** — anyone with the URL can read your positions and (once Phase B ships) change your settings. That's the next step.

---

## 4. Cloudflare Access — gate the dashboard

1. In the Cloudflare dashboard → **Zero Trust** (free for up to 50 users).
2. **Settings → Authentication → Login methods**: enable **One-time PIN** (email OTP) as a minimum. Add Google / GitHub as additional providers if you want.
3. **Access → Applications → Add an application → Self-hosted**.
4. Configure:
   - **Application name**: `x-alpaca-bot`
   - **Session duration**: 24 hours (or longer for personal use)
   - **Application domain**: `x-alpaca-bot.your-zone.dev` (path: leave empty so the whole site is gated)
   - **Identity providers**: tick the ones from step 2
5. **Add a policy**:
   - **Policy name**: `me`
   - **Action**: Allow
   - **Include**: Emails → `you@example.com`
6. **Save**.

Visit `https://x-alpaca-bot.your-zone.dev` again — you'll be bounced to an Access login page, get a one-time PIN by email, and after authenticating, the dashboard loads. The Access cookie is set on the hostname; all subsequent `/positions`, `/timeline`, `/ws`, and (Phase B) `/config` requests inherit it automatically.

To verify protection: open the URL in an incognito window. You should get the login screen, NOT the dashboard.

---

## 5. Verification

In the authenticated browser, open `https://x-alpaca-bot.your-zone.dev`:
- Header shows the brand mark + wordmark, status pill `running`
- Timeline empty (or populated if signals have arrived)
- Network tab: `/healthz`, `/positions`, `/timeline`, `/performance` all return 200; `/ws` returns `101 Switching Protocols`

On the droplet:
```bash
sudo journalctl -u x-alpaca-bot -f       # orchestrator heartbeats, risk checks
```

Reboot test:
```bash
sudo reboot
# wait 30s, SSH back, then:
sudo systemctl status x-alpaca-bot       # active (running)
curl -I http://localhost:8000/           # 200, dashboard HTML
```

---

## 6. Operations

### Update the bot + dashboard

```bash
sudo bash /opt/x-alpaca-trading-bot/deploy/install.sh --update
```

`--update` pulls, reinstalls Python deps, **rebuilds the dashboard** (`npm ci && npm run build`), re-stamps the systemd unit, and restarts the service.

### Tail logs

```bash
sudo journalctl -u x-alpaca-bot -f
```

### Stop / restart

```bash
sudo systemctl restart x-alpaca-bot
sudo systemctl stop x-alpaca-bot
```

Stopping the service does NOT flatten open positions; use the 15:55 ET window or close on Alpaca's web UI first if needed.

### Inspect DB state

```bash
psql "$DATABASE_URL" <<'SQL'
SELECT count(*) AS posts FROM x_posts;
SELECT count(*) AS signals, sum(case when taken then 1 else 0 end) AS taken FROM signals;
SELECT exit_reason, count(*) FROM trades GROUP BY exit_reason;
SQL
```

### Rotate keys

```bash
sudo -u xalpaca nano /opt/x-alpaca-trading-bot/.env
sudo systemctl restart x-alpaca-bot
```

---

## 7. Troubleshooting

**`systemctl start x-alpaca-bot` exits with code 1.**
Tail logs (`journalctl -u x-alpaca-bot -n 50`). Most common cause: `.env` is missing a required var.

**Browser loads dashboard but the WebSocket reconnects every few seconds.**
With Cloudflare Tunnel this is usually a session timeout on `/ws`. Increase the Access app session duration, or check that the tunnel config doesn't have a `connectTimeout` set lower than expected.

**Dashboard shows everything as "—" after login.**
Open Network tab. If `/healthz` returns 200 but other endpoints return HTML (the Access login page), your session cookie expired mid-fetch. Reload the page.

**Local dev can't hit the same-origin API.**
That's expected — local dev uses Vite's proxy. Run `cd dashboard && npm run dev` and visit `http://localhost:5173`. The proxy in `vite.config.js` forwards to `localhost:8000` where you should also be running `uvicorn api.main:build_production_app --factory --reload`.

**Polygon snapshots returning empty.**
Your plan probably doesn't include real-time options data. Snapshot Greeks need Options Starter+. Bot still functions; Greeks land as NULL.
