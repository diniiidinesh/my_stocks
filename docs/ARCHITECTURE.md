# Architecture

High-level map of the codebase. Prefer this over scrolling `src/` blindly.

## Runtime flow

```text
.env / Settings
      │
      ▼
 build_universe ──► Instrument[] (symbol, token, prev_close, …)
      │
      ├─ live: load_nfo_equity_underlyings (F&O set)
      └─ live: load_asm_symbols (ASM set)
      │
      ▼
 AlertEngine(prev_closes, thresholds, fo_*, asm_*)
      │
      ▼
 MockFeed / KiteFeed ──on_tick(symbol, ltp)──► engine.on_tick
      │                                            │
      │                                            ├─► Notifier.send (console / Telegram)
      │                                            └─► OrderExecutor (optional)
      ▼
 on exit / `report` ──► build_day_report → file + optional Telegram
```

## Package layout (`src/nse_alert/`)

| File | Responsibility |
|------|----------------|
| `cli.py` | Click commands; wires watch/report/login/orders |
| `config.py` | `Settings` from env / `.env` |
| `engine.py` | `%` move, multi-threshold dedupe, FO-only gate, ASM/F&O flags |
| `universe.py` | NSE EQ instruments + quote liquidity screen |
| `surveillance.py` | NFO underlyings + Zerodha ASM sheet |
| `feed.py` | `MockFeed`, `KiteFeed` WebSocket |
| `report.py` | EOD UP/DOWN counts and multi-level gaps |
| `notify/telegram.py` | Console + Telegram formatting |
| `orders.py` | Order book, dry-run/confirm/auto, SL-Limit |
| `confirm_bot.py` | Telegram CONFIRM/CANCEL listener |
| `login_ui.py` | Local OAuth / paste-token UI |
| `envfile.py` | `.env` read/write helpers |

## Deploy / ops

| Path | Role |
|------|------|
| `Dockerfile`, `docker-compose.yml` | Container run |
| `deploy/nse-alert.service` | systemd unit |
| `deploy/CLOUD.md` | VM install |
| `deploy/COST.md` | Provider comparison |
| `universes/liquid_sample.txt` | Safe starter watchlist |

## State on disk (`.nse_alert/` — gitignored)

| File | Purpose |
|------|---------|
| `fired.json` | Today’s dedupe keys + alert events |
| `report-YYYY-MM-DD.txt` | EOD report |
| `orders.json` | Pending / placed order state |
| `asm_symbols.txt` | Cached ASM list |

## Tests

```text
tests/test_engine.py
tests/test_report.py
tests/test_surveillance.py
tests/test_orders.py
tests/test_envfile.py
```

Run: `uv run pytest`.
