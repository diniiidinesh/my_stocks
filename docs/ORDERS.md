# Orders (test strategy)

Optional. Alerts work with `TRADE_MODE=off`.

**Login impact:** none for sizing/SL logic — same daily Kite access token as `watch`. Live margin sizing and live places need a valid token.

## Current defaults

| Setting | Value | Meaning |
|---------|-------|---------|
| `TRADE_MODE` | `off` / `dry_run` / `confirm` / `auto` | How aggressive |
| `TRADE_ON_THRESHOLDS` | `13` | Only act on this alert level |
| `TRADE_SIDES` | `up` | BUY on UP only |
| `TRADE_STOP_LOSS_PCT` | `2` | SL trigger ≈ fill × 0.98 |
| `TRADE_STOP_LIMIT_TICKS` | `2` | Limit = trigger − N×₹0.05 |
| `TRADE_SIZING` | `margin` | `margin` ≈ `TRADE_MARGIN_INR` of real MIS margin; `fixed` = `TRADE_QTY` |
| `TRADE_MARGIN_INR` | `10000` | Target capital / margin budget (₹) |
| `TRADE_FALLBACK_LEVERAGE` | `5` | Offline estimate when Kite margins API unavailable |
| `TRADE_QTY` | `1` | Used when `TRADE_SIZING=fixed` |
| `TRADE_PRODUCT` | `MIS` | Intraday (required for same-day sell-SL) |
| `TRADE_STOP_WAIT_SEC` | `20` | Wait for entry fill before SL (live) |
| `TRADE_TRAIL_BREAKEVEN` | `true` | Move SL to entry when LTP rises enough |
| `TRADE_TRAIL_BREAKEVEN_PCT` | `2` | Arm cost-to-cost trail at entry × 1.02 |
| `TRADE_MAX_ORDERS_PER_DAY` | `10` | Entry-order cap (stops excluded) |

## Flow

1. **+13% UP alert** (or whatever is in `TRADE_ON_THRESHOLDS`)
2. **Size** MIS BUY so required margin ≈ `TRADE_MARGIN_INR` (per-stock leverage via Kite `order_margins`)
3. Place **MARKET BUY** (MIS)
4. **Wait** for fill (`COMPLETE`, up to `TRADE_STOP_WAIT_SEC`); use **average_price** for the stop
5. Place **SL-Limit SELL** (trigger 2% under fill, limit a few ticks lower)
6. On each tick: if LTP ≥ entry × (1 + `TRADE_TRAIL_BREAKEVEN_PCT`/100), **modify** stop to entry (cost-to-cost)

Stop orders do **not** consume `TRADE_MAX_ORDERS_PER_DAY`. One open position per symbol/day.

### Sizing

```text
qty = floor(TRADE_MARGIN_INR / margin_per_share)
```

Example: ₹10,000 budget, stock needs ₹2,000 margin/share at its MIS leverage → **5 shares**. Notional = qty × price (can exceed ₹10k when leveraged).

- Live / dry_run with token: margin from Kite  
- Offline dry_run: `qty ≈ floor(TRADE_MARGIN_INR × TRADE_FALLBACK_LEVERAGE / price)`  
- `TRADE_SIZING=fixed`: always `TRADE_QTY`

**Do not comment out `TRADE_QTY=1` hoping to unlock ₹10k size.** That variable is ignored when `TRADE_SIZING=margin` (the default). If a live/confirm order still shows **1 share**, check:

1. `.env` has `TRADE_SIZING=margin` and `TRADE_MARGIN_INR=10000` (then **rebuild/restart** `watch`).
2. Watch logs: `sizing=margin budget=₹10000 | TRADE_QTY=1 unused`.
3. Confirm / dry-run Telegram line `Sizing: …` — it states why qty is what it is.
4. Expensive names can correctly size to **1** if one share’s MIS margin is already ≥ ₹10k.
5. `nse-alert order … --qty 1` is an explicit override; omit `--qty` to use margin sizing (needs a Kite token so LTP can be fetched).

Confirm Telegram messages include the sizing note (qty, ₹/share, leverage, notional).

### Why MIS?

CNC sell-SL right after a buy often fails (needs holdings). Same-day exit needs **MIS**.

### Stop-loss notes (root causes we fixed)

| Issue | Fix |
|-------|-----|
| CNC after same-day buy | Default `TRADE_PRODUCT=MIS` |
| SL before BUY complete | Wait for fill via `order_history` |
| SL from alert LTP | Use fill `average_price` |
| Sell SL trigger ≥ LTP | Clamp trigger below reference LTP |
| Cap=1 blocked SL | Stops excluded from daily entry cap |

`THRESHOLD_PCT` must include `13` (or your `TRADE_ON_THRESHOLDS`) or the trade trigger never fires.

## Modes

| Mode | Behaviour |
|------|-----------|
| `off` | Alerts only |
| `dry_run` | Log + Telegram pretend orders; nothing hits Kite |
| `confirm` | Telegram `/confirm <id>` / `/cancel <id>` before place |
| `auto` | Places immediately when filter matches |

Recommended path: `dry_run` → `confirm` → (maybe) `auto`.

### Telegram groups (confirm does nothing)

Default BotFather **privacy** means a group bot only sees **slash commands**, @mentions, and replies to itself. Typing `CONFIRM abc123` as a normal group message is silently dropped.

1. Use the slash command: `/confirm abc123` (also `/confirm@YourBot abc123`).
2. In [@BotFather](https://t.me/BotFather): `/setprivacy` → pick the bot → **Disable**. Then remove + re-add the bot to the group (or wait a bit).
3. `TELEGRAM_CHAT_ID` must be the **group** id (negative, often `-100…`) if you want **alerts** in that group. Discover it:

```bash
# stop watch first so this can peek at getUpdates
uv run nse-alert telegram-chats
```

Send any message in the group, re-run `telegram-chats`, copy the id into `.env`, restart `watch`. Confirm commands are accepted from the group even if `TELEGRAM_CHAT_ID` is still your DM — but then results/alerts still go to the DM.

4. `TRADE_MODE=confirm` and `watch` must be running; otherwise there is no listener.

Fallback: `uv run nse-alert confirm ABC123` on the VM.

## CLI helpers

```bash
uv run nse-alert order buy RELIANCE --dry-run          # margin-sized (needs token)
uv run nse-alert order buy RELIANCE --qty 1 --dry-run  # explicit 1 share
uv run nse-alert order buy RELIANCE --qty 1 --live
uv run nse-alert pending
uv run nse-alert confirm ABC123
uv run nse-alert telegram-chats
```

Alert-driven auto/confirm orders use margin sizing unless `TRADE_SIZING=fixed`. Manual `order` without `--qty` does the same (needs LTP via Kite). `--qty N` always wins.

## Compliance notes

- From **1 Apr 2026**, Zerodha requires a **whitelisted static public IP** for API **order** endpoints.
- ASM stocks may be blocked for API orders (broker/exchange rules) even if we only **tag** them in alerts.
- Not investment advice. Start with `dry_run`; lower `TRADE_MARGIN_INR` if you want smaller live size.

## Related

- [ALERTS.md](ALERTS.md) — when alerts fire  
- [TESTING_ORDERS.md](TESTING_ORDERS.md) — automated + live test cases  
- [../deploy/CLOUD.md](../deploy/CLOUD.md) — static IP + whitelist  
- Code: `orders.py`, `confirm_bot.py`
