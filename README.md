# NSE Alert

Realtime watcher for **NSE cash stocks** that move **±13%** (configurable) from the previous close. Streams LTP via **Zerodha Kite Connect** WebSocket and notifies you on **Telegram** (with console fallback).

## What it watches

Not the full NSE list and not F&O-only. Default universe:

1. NSE **EQ** cash names from Kite instruments (when live)
2. Prior-session **NSE bhavcopy** turnover ≥ **₹25 crore** (`MIN_TURNOVER_CR`)
3. Close/price ≥ **₹20** (`MIN_PRICE`)

Override with `CUSTOM_UNIVERSE_FILE` (one ticker per line) if you want a fixed list.

## Quick start (mock — no credentials)

```bash
# Install (uv recommended)
curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync

# Demo: synthetic ticks until DEMO13 crosses ±13%
uv run nse-alert watch --feed mock --threshold 13

# List the mock universe
uv run nse-alert universe
```

## Live setup (Kite + Telegram)

1. Copy env template and fill in values:

```bash
# Prefer the visible template (no leading dot):
cp env.template .env
# Or: cp .env.example .env
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

4. Run during market hours:

```bash
# .env: FEED_MODE=kite
uv run nse-alert watch --threshold 13
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
nse-alert watch [--threshold 13] [--feed mock|kite] [--max-ticks N]
nse-alert universe [--min-turnover-cr 25] [--min-price 20]
nse-alert login-hint
```

## How alerts work

- Day change: `(LTP / previous_close - 1) * 100`
- Fires when `|change| >= threshold` (default **13**)
- **Once per symbol per calendar day** (state in `.nse_alert/fired.json`)

## Project layout

```text
src/nse_alert/
  cli.py              # click entrypoint
  config.py           # pydantic-settings / .env
  universe.py         # bhavcopy liquidity screen + Kite mapping
  feed.py             # MockFeed + KiteFeed (LTP)
  engine.py           # % move + dedupe
  notify/telegram.py  # Telegram + console
```

## Notes

- Kite `access_token` must be refreshed **daily**.
- Live data needs the **paid** Connect plan; Personal (free) WebSockets return 403 for market data.
- ±13% on liquid names is uncommon — tune `--threshold` or `MIN_TURNOVER_CR` if needed.
