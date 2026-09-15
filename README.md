# NSE Alert

Realtime watcher for **NSE cash stocks** that move a configurable **±%** from the previous close (e.g. **4%, 7%, and 11%**). Streams LTP via **Zerodha Kite Connect** WebSocket, notifies on **Telegram**, can optionally **place orders**, and is designed to run on a **cloud VM** with a static IP.

## What it watches

Not the full NSE list and not F&O-only. Default **live** universe (Kite only — no NSE website scrape):

1. All NSE **EQ** cash names from `kite.instruments("NSE")`
2. Session turnover proxy `volume × LTP` ≥ **₹25 crore** (`MIN_TURNOVER_CR`)
3. Prev close / price ≥ **₹20** (`MIN_PRICE`)

Override with `CUSTOM_UNIVERSE_FILE` (one ticker per line) for a fixed list (still quoted via Kite when live). **Recommended** for first live runs — use `universes/liquid_sample.txt` so you don’t quote the entire cash market (large `quote` URLs often hit Cloudflare).

## Quick start (mock — no credentials)

```bash
# Install (uv recommended)
curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync

# Demo: synthetic ticks; DEMO13 crosses each level
uv run nse-alert watch --feed mock --threshold 4,7,11

# List the mock universe
uv run nse-alert universe
```

## Live setup (Kite + Telegram)

1. Copy env template and fill in values:

```bash
cp .env.example .env
```

2. **Kite Connect (required for live quotes)**  
   - Paid **Connect** plan: **~₹500 / month / API key** (free Personal apps cannot stream WebSocket market data).  
   - In `.env` set permanent values: `KITE_API_KEY` and `KITE_API_SECRET`.  
   - In the Kite app, set **Redirect URL** to `http://127.0.0.1:8765/callback`.  
   - Each trading day, get a fresh access token via the local UI:
     ```bash
     uv run nse-alert login
     ```
     That opens a browser page where you can **Login with Kite** (auto-saves token) or **paste** an access token.  
     Or without the UI: `uv run nse-alert set-token YOUR_ACCESS_TOKEN`

3. **Telegram (recommended)**  
   - Create a bot with [@BotFather](https://t.me/BotFather), get the token.  
   - Message the bot, then get your chat id (e.g. via `@userinfobot` or the Bot API `getUpdates`).  
   - Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.  
   - If unset, alerts still print to the terminal.

4. Set alert levels in `.env` (comma-separated):

```env
THRESHOLD_PCT=4,7,11
```

5. Run during market hours:

```bash
# .env: FEED_MODE=kite
uv run nse-alert watch
# or override: uv run nse-alert watch --threshold 4,7,11
```

## Costs (approx. INR)

| Item | Type | Amount | Notes |
|------|------|--------|--------|
| Kite Connect (paid) | Recurring | **₹500 / month** | Required for live WebSocket + quotes |
| Telegram Bot API | Recurring | **₹0** | Primary alert channel |
| Run on your PC | Recurring | ₹0 | Keep process up 9:15–15:30 IST |
| Optional VPS | Recurring | ~₹300–800 | If you want cloud always-on |
| WhatsApp Cloud API | Optional | ~₹0.12–₹0.86 / message | Not in v1 — Meta Business onboarding; Telegram is faster to ship |

**Lean monthly total:** ~₹500 (Kite) if you run locally and use Telegram.

WhatsApp is feasible later via official Cloud API / a BSP, but it adds per-message fees and business verification. Unofficial WhatsApp scrapers are not used here.

## CLI

```text
nse-alert login [--port 8765]     # browser UI: Login with Kite OR paste token
nse-alert set-token ACCESS_TOKEN  # paste token into .env (no browser)
nse-alert watch [--threshold 4,7,11] [--feed mock|kite] [--max-ticks N]
nse-alert universe [--min-turnover-cr 25] [--min-price 20]
nse-alert report [--date YYYY-MM-DD] [--telegram]
nse-alert order buy|sell SYMBOL [--qty N] [--dry-run|--live]
nse-alert pending                 # list confirmations
nse-alert confirm ID              # confirm a pending order
nse-alert login-hint
```

## Orders (optional — test strategy)

Test run defaults (keep it simple; expand later):

```env
THRESHOLD_PCT=13
TRADE_MODE=dry_run          # off | dry_run | confirm | auto
TRADE_ON_THRESHOLDS=13      # BUY only on +13% UP alert
TRADE_SIDES=up
TRADE_STOP_LOSS_PCT=2       # SL-M sell trigger = entry × 0.98
TRADE_QTY=1
TRADE_PRODUCT=CNC
TRADE_MAX_ORDERS_PER_DAY=3
```

Flow: `+13% UP` → market BUY → place **SL-M SELL** at **2% below entry LTP**.
One open position per symbol/day. Start with `dry_run`, then `confirm`.

API **order** placement needs a **whitelisted static IP** (Zerodha / SEBI, from 1 Apr 2026). See [deploy/CLOUD.md](deploy/CLOUD.md).

## Cloud deploy

```bash
# on an Ubuntu VM (AWS Mumbai + Elastic IP recommended)
docker compose up -d --build
docker compose logs -f
```

Full guide (static IP, daily login, systemd): **[deploy/CLOUD.md](deploy/CLOUD.md)**.

## How alerts work

- Day change: `(LTP / previous_close - 1) * 100`
- Fires when `|change|` crosses each configured level (default **13**, or e.g. **4, 7, 11**)
- **Once per symbol / direction / threshold per calendar day** (state in `.nse_alert/fired.json`)
- A jump that skips levels (e.g. +3% → +12%) still fires each newly crossed level in order
- When `watch` stops (Ctrl+C / end of mock run), an **end-of-day report** is printed and saved under `.nse_alert/report-YYYY-MM-DD.txt` — counts per threshold, plus time gaps between multi-level crossings (IST). Re-run anytime with `uv run nse-alert report` (add `--telegram` to push it).

## Project layout

```text
src/nse_alert/
  cli.py              # click entrypoint
  config.py           # pydantic-settings / .env
  login_ui.py         # local browser UI for daily Kite token
  envfile.py          # .env read/write helpers
  report.py           # end-of-day crossings + multi-level time gaps
  universe.py         # Kite instruments + quote liquidity screen
  feed.py             # MockFeed + KiteFeed (LTP)
  engine.py           # % move + dedupe
  notify/telegram.py  # Telegram + console
```

## Notes

- Kite `access_token` must be refreshed **daily**.
- Live data needs the **paid** Connect plan; Personal (free) WebSockets return 403 for market data.
- Large moves on liquid names are uncommon — tune `--threshold` / `THRESHOLD_PCT` or `MIN_TURNOVER_CR` if needed.
