# Architecture

High-level map of the codebase. Prefer this over scrolling `src/` blindly.

## Runtime flows

### Intraday alerts (+ optional MIS orders)

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
      │                                            ├─► OrderExecutor (optional MIS + SL)
      │                                            ├─► manage_open_stops (cost-to-cost trail)
      │                                            └─► SignalMonitor (volume spike / 52w) ─► /confirm offers
      │
 SignalSeeder (background) ──Kite historical──► EMA warm-up + 52w/yesterday context
      ▼
 on exit / `report` ──► build_day_report (+ EOD closes) → file + optional Telegram
```

### EOD technical screener (separate)

```text
Nifty 500 ∪ Smallcap 250
      │
      ├─ market cap (Yahoo / cache)
      └─ turnover (Kite quotes when logged in)
      ▼
 daily OHLCV (Kite historical or Yahoo) + NSE bhavcopy delivery %
      ▼
 indicators (EMA stack, SuperTrend, volume EMA, ADX, RSI, MACD, 52w)
      ▼
 Excel (all-pass ranked first) + Telegram summary/attachment
```

## Package layout (`src/nse_alert/`)

| File / package | Responsibility |
|----------------|----------------|
| `cli.py` | Click commands; wires watch/report/screen/login/orders |
| `config.py` | `Settings` from env / `.env` |
| `engine.py` | `%` move, multi-threshold dedupe, FO-only gate, ASM/F&O flags |
| `session.py` | Per-symbol continuous close + MIS entry cutoff (F&O/CAS 15:15 / 15:12; others 15:30 / 15:25) |
| `signals.py` | Volume-spike candles + EMA, 52-week breakout, signal message, background history seeder ([SIGNALS.md](SIGNALS.md)) |
| `universe.py` | NSE EQ instruments + quote liquidity screen |
| `surveillance.py` | NFO underlyings + Zerodha ASM sheet |
| `feed.py` | `MockFeed`, `KiteFeed` WebSocket; alerts on repeated reconnects, exits the watcher if reconnection is abandoned |
| `report.py` | EOD UP/DOWN counts, close %, hold-level counts, gaps |
| `notify/telegram.py` | Console + Telegram text + document upload |
| `orders.py` | Order book, margin sizing, dry-run/confirm/auto, SL-Limit, trail; alerts once per pending-order expiry |
| `confirm_bot.py` | Telegram `/confirm` / `/cancel` listener (groups + DMs); alerts + backs off after 3 consecutive `getUpdates` failures |
| `lock.py` | Single-instance `flock` on `watch` so a second watcher can't 409-lock Telegram (see [RCA-2026-09-17.md](RCA-2026-09-17.md), Incident A) |
| `ipv4.py` | Optional IPv4-only DNS so Kite sees the Elastic IP |
| `login_ui.py` | Local OAuth / paste-token UI |
| `envfile.py` | `.env` read/write helpers |
| `screener/` | EOD TA screener (indicators, history, delivery, Excel export) |

## Deploy / ops

| Path | Role |
|------|------|
| `Dockerfile`, `docker-compose.yml` | Container run |
| `deploy/CLOUD.md` | VM install + screener cron |
| `deploy/COST.md` | Provider comparison |
| `universes/liquid_sample.txt` | Safe starter watchlist |
| `samples/sample-eod-screener-report.xlsx` | Dummy screener Excel layout |

## State on disk (`.nse_alert/` — gitignored)

| File / dir | Purpose |
|------------|---------|
| `watch.lock` | Single-instance lock (`flock`) — holds the PID of the running watcher |
| `fired.json` | Today’s dedupe keys + alert events |
| `signals.json` | Today’s 52-week breakout dedupe keys |
| `report-YYYY-MM-DD.txt` | EOD alert report |
| `orders.json` | Pending / placed / open SL tracks |
| `asm_symbols.txt` | Cached ASM list |
| `screener/history/` | Cached daily OHLCV CSVs |
| `screener/market_caps.csv` | Cached market caps (₹ Cr) |
| `screener/bhavcopy/` | Cached NSE full bhavcopy (delivery %) |
| `screener/screen-YYYY-MM-DD.xlsx` | Screener output |

## Tests

```text
tests/test_engine.py
tests/test_report.py
tests/test_surveillance.py
tests/test_orders.py
tests/test_signals.py
tests/test_session.py
tests/test_screener.py
tests/test_envfile.py
```

Run: `uv run pytest`.
