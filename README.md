# NSE Alert

Realtime watcher for **NSE cash stocks** that move a configurable **±%** from the previous close. Streams LTP via **Zerodha Kite Connect** WebSocket, notifies on **Telegram**, can optionally **place MIS orders**, runs an **EOD technical screener**, and is designed for a **cloud VM** with a static IP.

| Feature | Default / note |
|---------|----------------|
| Alert levels | `4,7,11,13` (`THRESHOLD_PCT`) |
| ±4% scope | **F&O underlyings only** (`FO_ONLY_THRESHOLDS=4`) |
| ASM tag | Zerodha RMS sheet (not a Kite API) |
| Test trades | BUY on **+13% UP** (MIS) + **2% SL-Limit**, margin-sized ≈ **₹10k** |
| Cost-to-cost SL | Trail stop to entry after +2% from fill |
| EOD alert report | Close % per scrip + hold-level counts |
| EOD TA screener | Daily chart → Excel + Telegram (after 15:40 IST) |
| Cloud | AWS Lightsail Mumbai (Oracle free if capacity allows) |

## Documentation map

| Doc | What it’s for |
|-----|----------------|
| **[docs/README.md](docs/README.md)** | Full index — start here to navigate the repo |
| **[docs/ALERTS.md](docs/ALERTS.md)** | Thresholds, F&O filter, ASM tags, EOD reports |
| **[docs/LOGIN.md](docs/LOGIN.md)** | Daily Kite token (`login` / `set-token`) |
| **[docs/ORDERS.md](docs/ORDERS.md)** | Dry-run / confirm / auto, sizing, SL trail |
| **[docs/SCREENER.md](docs/SCREENER.md)** | EOD TA screener → Excel + Telegram |
| **[docs/TESTING_ORDERS.md](docs/TESTING_ORDERS.md)** | Order test cases + live handoff checklist |
| **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** | Code layout and data flow |
| **[docs/UPKEEP.md](docs/UPKEEP.md)** | How to keep docs accurate when code changes |
| **[CONTRIBUTING.md](CONTRIBUTING.md)** | How to read, change, and test this repo |
| **[deploy/CLOUD.md](deploy/CLOUD.md)** | VM install (Lightsail / Oracle / EC2) |
| **[deploy/COST.md](deploy/COST.md)** | Free tiers, intro credits, which VM to pick |
| **[.env.example](.env.example)** | All env vars with comments |

## Quick start (mock — no credentials)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync
cp .env.example .env

uv run nse-alert watch --feed mock --threshold 4,7,11,13
uv run nse-alert universe
uv run nse-alert report
uv run nse-alert screen --force --max-symbols 20 --no-telegram
```

## Live setup (short)

1. Copy `.env.example` → `.env` and fill Kite + Telegram values.
2. Kite app **Redirect URL:** `http://127.0.0.1:8765/callback`
3. Each trading day: `uv run nse-alert login` (or `set-token …`)
4. Set `FEED_MODE=kite`, then `uv run nse-alert watch`
5. After close: `uv run nse-alert report` and/or `uv run nse-alert screen`

Details: [docs/LOGIN.md](docs/LOGIN.md) · alerts: [docs/ALERTS.md](docs/ALERTS.md) · orders: [docs/ORDERS.md](docs/ORDERS.md)

## CLI

```text
nse-alert login [--port 8765]     # browser UI: Login with Kite OR paste token
nse-alert set-token ACCESS_TOKEN  # write token into .env
nse-alert watch [--threshold 4,7,11,13] [--feed mock|kite] [--max-ticks N]
nse-alert universe [--min-turnover-cr 25] [--min-price 20]
nse-alert report [--date YYYY-MM-DD] [--telegram]
nse-alert screen [--force] [--telegram/--no-telegram] [--max-symbols N]
nse-alert order buy|sell SYMBOL [--qty N] [--dry-run|--live]
nse-alert pending
nse-alert confirm ID
nse-alert login-hint
```

## Costs (approx. INR)

| Item | Amount | Notes |
|------|--------|--------|
| Kite Connect (paid) | **~₹500 / month** | Required for live WebSocket |
| Telegram | ₹0 | Primary alert channel |
| Local run | ₹0 | Keep process up 9:15–15:30 IST |
| Cloud VM | ₹0 (Oracle free) or ~₹400–600 | See [deploy/COST.md](deploy/COST.md) |

## Notes

- Access token expires **daily** — login does not change when we add F&O/ASM/screener features.
- API **orders** need a **whitelisted static IP** (Zerodha / SEBI from 1 Apr 2026). Alerts-only do not.
- Not investment advice. Start with `TRADE_MODE=dry_run`; default entry margin budget is `TRADE_MARGIN_INR=10000`.
