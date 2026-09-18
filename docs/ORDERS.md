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
- Offline dry_run (no Kite credentials configured at all): `qty ≈ floor(TRADE_MARGIN_INR × TRADE_FALLBACK_LEVERAGE / price)`  
- `TRADE_SIZING=fixed`: always `TRADE_QTY`

**A rejected/expired token does not use the offline fallback — it refuses to
trade.** These look similar (both come from a failed `order_margins` call) but
are not the same: no-credentials-configured is expected in `dry_run`/offline
testing, while a token Kite itself rejects mid-session means auth is broken
*right now* and an approximate leverage guess could size a real order wrong
(this happened on 2026-09-17 — see
[RCA-2026-09-17.md](RCA-2026-09-17.md)). `watch` catches this, alerts
`🔑 <symbol> trade skipped: ...` to Telegram, and skips just that one trade —
alerts and the rest of the watcher keep running. `order`/`size` raise a plain
CLI error instead. `watch` also checks the token *before* connecting at all
(`kite.profile()`) and exits with a Telegram alert if it's already invalid at
startup, rather than discovering it deep inside a trade.

**Do not comment out `TRADE_QTY=1` hoping to unlock ₹10k size.** That variable is ignored when `TRADE_SIZING=margin` (the default). **`TRADE_SIZING=margin` can still place 1 share** — that is the calculated result when Kite’s margin for **1 share** is already most of `TRADE_MARGIN_INR`:

```text
qty = max(1, floor(10000 / margin_per_share))
```

So qty=1 whenever `margin_per_share > 5000` (e.g. a ₹8,000 name with **1x** / 100% margin, ASM, or a very expensive stock). That is **not** `TRADE_QTY`.

Diagnose on the VM (needs today’s token):

```bash
uv run nse-alert size SYMBOL
# or: uv run nse-alert size SYMBOL --price 1234.5
```

If a live/confirm order still looks wrong, paste:

1. The Telegram **`Sizing:`** line (or `size` command `note=` line)
2. `nse-alert size SYMBOL` full output
3. Symbol + LTP
4. Watch log line `sizing=margin budget=₹…` from process start
5. Optional: `.nse_alert/orders.json` `quantity` + `reason` (redact tokens)

Confirm Telegram messages include the sizing note (qty, ₹/share, leverage, notional). When qty is 1 because margin/share is large, the note now says so explicitly.

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
5. **Exactly one `watch` may run against a bot token.** See below — this is the
   failure mode that is hardest to spot.

### Only one watcher, ever (confirm silently dies otherwise)

Telegram allows **one** `getUpdates` consumer per bot token. Start a second
`watch` — a stray container, a systemd unit, a leftover `uv run` — and *every*
poll fails:

```
409 Conflict: terminated by other getUpdates request;
make sure that only one bot instance is running
```

The listener then receives **nothing**. Pending orders are never confirmed and
expire at `TRADE_CONFIRM_TTL_MINUTES`.

This is nasty because **outbound alerts keep working perfectly** — `sendMessage`
has no such restriction. The bot can talk to you; it cannot hear you. Nothing
looks broken from the Telegram side.

On 2026-09-17 this cost four orders and produced 6,062 `409`s in one day before
anyone noticed. Full write-up: [RCA-2026-09-17.md](RCA-2026-09-17.md).

Two independent guards now exist so this specific failure can't repeat
silently: a second `watch` refuses to start at all (single-instance lock —
see `nse_alert/lock.py`), and if the listener still goes unhealthy for any
other reason, it sends one Telegram alert after 3 consecutive `getUpdates`
failures (`⚠️ Confirm listener is down — ...`), backing off instead of
hammering a dead poll. You should never again have to notice this by counting
409s in a log.

A third guard covers the case even if the listener *is* healthy but you
simply don't reply in time: `watch` polls for newly-expired pending orders
(throttled to once per 30s) and sends `⏱ Order EXPIRED unconfirmed: <symbol>
<side> <qty> @ ~₹<ltp> (id <id>, <N> min TTL)` for each one — batched into a
single summary if more than 3 expire in the same sweep. This is the one that
would have surfaced the 2026-09-17 incident within 30 minutes instead of at
day's end.

**Check before you trust a confirm run:**

```bash
# on the VM — must print exactly one line
ps -eo cmd --no-headers | grep "[.]venv/bin/python .*nse-alert watch"

# and the listener must not be 409-ing
docker compose logs --tail 50 nse-alert | grep "409 Conflict"   # expect nothing
```

If a systemd unit is also installed, `sudo systemctl disable --now nse-alert`.
Pick one mechanism — see [../deploy/CLOUD.md](../deploy/CLOUD.md).

> `auto` mode starts **no** confirm listener, so it never shows a 409. A clean
> log in `auto` does not mean a second watcher is absent — check the process
> count instead.

Fallback: `uv run nse-alert confirm ABC123` on the VM (same dir as `watch`). If you see **No pending order**, run `nse-alert pending` — it prints the `orders.json` path. Docker watch and host `uv run` must share `./.nse_alert`.

Kite **IP is not allowed** with an `2406:…` address: the VM sent **IPv6**. Keep `KITE_FORCE_IPV4=true` and whitelist the Elastic IPv4. See [CLOUD.md](../deploy/CLOUD.md).

## CLI helpers

```bash
uv run nse-alert order buy RELIANCE --dry-run          # margin-sized (needs token)
uv run nse-alert order buy RELIANCE --qty 1 --dry-run  # explicit 1 share
uv run nse-alert order buy RELIANCE --qty 1 --live
uv run nse-alert size RELIANCE
uv run nse-alert pending
uv run nse-alert confirm ABC123
uv run nse-alert public-ip
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
