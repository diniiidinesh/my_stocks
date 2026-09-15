# NSE Alert

Realtime watcher for **NSE cash stocks** that move a configurable **±%** from the previous close (e.g. **4%, 7%, and 11%**). Streams LTP via **Zerodha Kite Connect** WebSocket and notifies you on **Telegram** (with console fallback).

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
   - Put `KITE_API_KEY` and a fresh daily `KITE_ACCESS_TOKEN` in `.env`.  
   - Run `uv run nse-alert login-hint` for the token steps.

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
nse-alert watch [--threshold 4,7,11] [--feed mock|kite] [--max-ticks N]
nse-alert universe [--min-turnover-cr 25] [--min-price 20]
nse-alert login-hint
```

## How alerts work

- Day change: `(LTP / previous_close - 1) * 100`
- Fires when `|change|` crosses each configured level (default **13**, or e.g. **4, 7, 11**)
- **Once per symbol / direction / threshold per calendar day** (state in `.nse_alert/fired.json`)
- A jump that skips levels (e.g. +3% → +12%) still fires each newly crossed level in order

## Project layout

```text
src/nse_alert/
  cli.py              # click entrypoint
  config.py           # pydantic-settings / .env
  universe.py         # Kite instruments + quote liquidity screen
  feed.py             # MockFeed + KiteFeed (LTP)
  engine.py           # % move + dedupe
  notify/telegram.py  # Telegram + console
```

## Notes

- Kite `access_token` must be refreshed **daily**.
- Live data needs the **paid** Connect plan; Personal (free) WebSockets return 403 for market data.
- Large moves on liquid names are uncommon — tune `--threshold` / `THRESHOLD_PCT` or `MIN_TURNOVER_CR` if needed.
