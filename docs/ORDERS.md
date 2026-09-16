# Orders (test strategy)

Optional. Alerts work with `TRADE_MODE=off`.

## Current test defaults

| Setting | Value | Meaning |
|---------|-------|---------|
| `TRADE_MODE` | `off` / `dry_run` / `confirm` / `auto` | How aggressive |
| `TRADE_ON_THRESHOLDS` | `13` | Only act on this alert level |
| `TRADE_SIDES` | `up` | BUY on UP only |
| `TRADE_STOP_LOSS_PCT` | `2` | SL trigger ≈ entry × 0.98 |
| `TRADE_STOP_LIMIT_TICKS` | `2` | Limit = trigger − N×₹0.05 |
| `TRADE_QTY` | `1` | Shares per order |
| `TRADE_PRODUCT` | `MIS` | Intraday (required for same-day sell-SL) |
| `TRADE_STOP_WAIT_SEC` | `20` | Wait for entry fill before SL (live) |
| `TRADE_TRAIL_BREAKEVEN` | `true` | Move SL to entry when LTP rises enough |
| `TRADE_TRAIL_BREAKEVEN_PCT` | `2` | Arm cost-to-cost trail at entry × 1.02 |
| `TRADE_MAX_ORDERS_PER_DAY` | `10` | Entry-order cap (stops excluded) |

Flow: **+13% UP alert** → **MIS** market **BUY** → wait for fill → attach **SL (stop-loss limit) SELL** with trigger at **2% below fill price** and limit a few ticks lower.  
On each tick, if LTP ≥ entry × (1 + `TRADE_TRAIL_BREAKEVEN_PCT`/100), the stop is **modified** to entry (cost-to-cost).  
Stop orders do **not** consume `TRADE_MAX_ORDERS_PER_DAY` (that cap is for entries only).  
One open position per symbol/day.

**Why MIS?** CNC sell-SL right after a buy often fails — you need holdings; same-day exit needs intraday product.

`THRESHOLD_PCT` must include `13` (or whatever you set in `TRADE_ON_THRESHOLDS`) or the trade trigger never fires.

## Modes

| Mode | Behaviour |
|------|-----------|
| `off` | Alerts only |
| `dry_run` | Log + Telegram pretend orders; nothing hits Kite |
| `confirm` | Telegram `CONFIRM <id>` / `CANCEL <id>` before place |
| `auto` | Places immediately when filter matches |

Recommended path: `dry_run` → `confirm` → (maybe) `auto`.

## CLI helpers

```bash
uv run nse-alert order buy RELIANCE --qty 1 --dry-run
uv run nse-alert order buy RELIANCE --qty 1 --live
uv run nse-alert pending
uv run nse-alert confirm ABC123
```

## Compliance notes

- From **1 Apr 2026**, Zerodha requires a **whitelisted static public IP** for API **order** endpoints.
- ASM stocks may be blocked for API orders (broker/exchange rules) even if we only **tag** them in alerts — do not assume tagged ASM names are orderable via API.
- Not investment advice. Use tiny size.

## Related

- [ALERTS.md](ALERTS.md) — when alerts fire  
- [TESTING_ORDERS.md](TESTING_ORDERS.md) — automated + live test cases  
- [../deploy/CLOUD.md](../deploy/CLOUD.md) — static IP + whitelist  
- Code: `orders.py`, `confirm_bot.py`
