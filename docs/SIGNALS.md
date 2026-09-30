# Signals: volume spikes and 52-week breakouts

These run inside `watch`, alongside the ±% threshold alerts ([ALERTS.md](ALERTS.md)), on the same tick stream. Each alert includes `/confirm` commands for a BUY and a SELL order on that stock. Nothing is ever placed unless you confirm it.

**Login impact:** none. They use the same daily Kite token. They need `FEED_MODE=kite`, because the mock feed has no volume or history, so signals stay idle there.

## Volume spike

| Env | Default | Meaning |
|-----|---------|---------|
| `VOLUME_SPIKE_ENABLED` | `true` | Turn the rule on or off |
| `VOLUME_SPIKE_TIMEFRAMES` | `5,15` | Comma-separated candle minutes (`1,3,5,10,15,30,60`) and/or `D` for the [daily rule](#daily-volume-spike-d), e.g. `5,15`, `D` or `5,15,D` |
| `VOLUME_SPIKE_EMA_PERIOD` | `21` | EMA length, in candles of the same timeframe |
| `VOLUME_SPIKE_MULT` | `2.5` | Fires when volume is **more than** N × EMA. One number for every timeframe, or per timeframe: `5:2.5,15:2.5,D:2`. A bare number is the default for timeframes not listed, e.g. `2.5,D:2` |
| `VOLUME_SPIKE_MIN_EMA` | `5:200000,15:1000000` | Per timeframe, `TIMEFRAME:SHARES` (`D:1000000` for daily): the EMA itself must be at least this many shares per candle. A timeframe left out has no floor. |
| `VOLUME_SPIKE_MIN_MARKET_CAP_CR` | `1000` | Only stocks with market cap ≥ ₹N crore (`0` = off). Volume spikes only, not 52-week alerts. |
| `VOLUME_SPIKE_SKIP_OPENING_CANDLE` | `true` | Ignore the 09:15 candle |
| `VOLUME_SPIKE_MIN_AVG_VALUE_CR` | `10` | Only alert for stocks averaging at least ₹N crore traded **per day** (close × volume, last 20 sessions; `0` = off). Stocks whose daily history failed to load are treated as below the floor. |

```text
mult = volume(just-closed candle) / EMA21(volume of the candles before it)
fire when mult > VOLUME_SPIKE_MULT
```

- **Candles** are aligned to the 09:15 IST open, the same buckets Kite uses for its historical data (so 5m candles are 09:15, 09:20, …). A candle is evaluated when the first tick of the next candle arrives, which is normally within a second or two of the candle closing. Each stock's last candle is settled at its continuous close: **15:15 for F&O (closing-auction) stocks**, 15:30 for the rest. Only volume from *before* the close counts, so the auction print never shows up as a spike. See [ALERTS.md → Market close](ALERTS.md#market-close-cas-since-3-aug-2026).
- **Volume** comes from KiteTicker's day-cumulative `volume_traded`. When volume signals are on, the WebSocket subscribes in **quote** mode instead of LTP mode. The instrument limit is the same, but each tick is larger.
- **EMA warm-up**: at startup, a background thread loads the last ~7 days of candles per symbol from Kite historical data. Each symbol **arms** once its own history loads. Kite allows about 3 historical requests per second, so a large universe takes a few minutes. Start `watch` before 09:15 so the morning is covered.
- **No duplicate alerts**: each candle is evaluated once, so a spike fires once per symbol per timeframe per candle.
- **The spike is excluded from its own baseline**: the EMA the candle is compared with does not include that candle yet.
- **Candles we can't measure are skipped, not guessed**: this covers the candle `watch` joined part-way through, and the first candle after a feed gap (backlog volume would look like a false spike).
- **Opening candle**: pre-open auction volume lands in the 09:15 candle, so it is almost always above 2× for the whole universe. That would mean hundreds of alerts at 09:20. It is skipped by default; set `VOLUME_SPIKE_SKIP_OPENING_CANDLE=false` to include it.

### Filters and the cadence they give

Measured over 9 sessions (15–25 Sep 2026) on the 511-stock qualified universe (`MIN_TURNOVER_CR=25`), with the defaults above (2.5×, EMA ≥ 200k on 5m and ≥ 1M on 15m, market cap ≥ ₹1,000 Cr, value ≥ ₹10 Cr):

| Multiplier | 5m alerts/day | 15m alerts/day | Total/day | Stocks/day |
|------------|---------------|----------------|-----------|------------|
| 2× | 204 | 25 | 229 | 52 |
| **2.5×** | **125** | **16** | **~141** | **45** |
| 3× | 83 | 10 | 93 | 38 |

- **Without the EMA floors**, the same universe gives ~3,500 alerts a day. The floors are what make this usable.
- **Busiest times:** 09:20–10:00 and 15:00–15:30.
- **With a 15-minute digest**, that's about 23 messages a day with ~6 stocks each.

The EMA floors are **share counts**, so they favour low-priced stocks. SUZLON, YESBANK and IFCI qualify easily, while a ₹12,000 stock like MARUTI essentially never averages 200k shares per 5 minutes. A ₹-value floor would be price-neutral if that becomes a problem.

**Market caps** come from Yahoo Finance at startup, cached daily in `.nse_alert/screener/market_caps.csv` (shared with the screener).
- About 3% of stocks have no market cap; they get no volume alerts.
- If no market caps load at all (Yahoo down), the market-cap floor is skipped, with an error in the log, rather than silencing every volume alert.

### Choosing the value floor

It's a ₹ value, not a share count, so high-priced stocks aren't penalised: MARUTI trades only ~0.4M shares a day but ~₹500 Cr.

Rule of thumb: keep your order to about **1% of what trades in the time you'd need to get out**. A day has 75 five-minute candles.

| Style | Time to exit | Floor ≈ | At ₹50k position |
|-------|--------------|---------|------------------|
| Intraday (MIS) | one 5m candle | position × 75 ÷ 1% | ~₹40 Cr → **₹50 Cr** with margin |
| Swing (CNC) | a session | position ÷ 1% | ~₹0.5 Cr, so **₹10 Cr** (screener's `SCREEN_MIN_TURNOVER_CR`) for spread/circuit quality |

Raise the floor as your position size grows. This is a sizing heuristic, not investment advice.

## Daily volume spike (`D`)

For swing setups. Put `D` in `VOLUME_SPIKE_TIMEFRAMES`, alone or alongside minute candles.

```text
mult = today's volume so far / EMA21(daily volume, previous sessions)
fire the first time mult > VOLUME_SPIKE_MULT (or its D: value) — once per stock per day
```

- **It fires the moment the running total crosses**, not at the close. A stock that has already traded 2.5× a normal day's volume by 11:00 alerts at 11:00; one that builds up slowly may alert at 14:45, or never.
  - The message shows the multiplier at that moment, so it's always just above the threshold.
- **Needs no extra data.** The daily EMA comes from the same daily bars used for the 52-week figures, and today's volume from the live feed.
  - With only `D` configured, no intraday history is downloaded, so startup takes about a minute instead of ~10.
- **Continuous session only**: 09:15–15:15 for F&O stocks, 09:15–15:30 for the rest.
  - The F&O closing-auction volume after 15:15 would otherwise tip many stocks over at once.
  - Pre-open volume counts, since it is part of the day's volume, but nothing fires before 09:15.
- **Once per stock per day**, remembered in `.nse_alert/signals.json` like the 52-week alerts, so a restart doesn't repeat it.
- **Filters that still apply:**
  - Market-cap and traded-value floors, as for minute candles.
  - An optional `D:` entry in `VOLUME_SPIKE_MIN_EMA` (shares per day).
  - Stocks with fewer than 21 sessions of history are skipped.
- **Output** is sent as `📊 DAILY VOLUME SPIKE`, or pooled into the volume digest when `SIGNAL_DIGEST_VOLUME_MINUTES` is set, with the usual BUY/SELL `/confirm` offers.

## 52-week high / low breakout

| Env | Default | Meaning |
|-----|---------|---------|
| `BREAKOUT_52W_ENABLED` | `true` | Turn the rule on or off |

- The 52-week high and low are the max high and min low of **daily bars from the prior 365 days, excluding today**.
- An alert fires when LTP goes above the prior 52-week high (`🚀 52W HIGH BREAKOUT`) or below the prior 52-week low (`🔻 52W LOW BREAKDOWN`).
- It is checked only during the stock's continuous session (09:15–15:15 for F&O stocks, 09:15–15:30 for the rest), never on closing-auction prices.
- It fires **once per symbol, direction and day**. That state survives restarts and lives in `.nse_alert/signals.json`.
- Daily bars share the EOD screener's cache (`.nse_alert/screener/history/`), so an evening `screen` run means the next morning needs no daily fetches. Today's still-forming bar is never written to that cache.

## Alert message

```text
📊 VOLUME SPIKE — RELIANCE (5m)
Volume: 2.50x its 10:05–10:10 candle vs EMA (52,500 vs 21,000)
Price: 2,950.10 | Day: +2.35%
52w high: 3,100.00
Yesterday close: 2,882.40 | Yesterday: -1.12%
Tags: F&O
Time (IST): 2026-09-24 10:10:02

Order: BUY /confirm A1B2C3 · SELL /confirm D4E5F6
(qty sized at confirm; SL attached)
```

52-week alerts show the level that was broken, plus both the 52w high and the 52w low.

| Field | Source |
|-------|--------|
| Price | LTP at the moment the alert fired |
| Day % | `(LTP / yesterday close − 1) × 100` |
| Yesterday close | previous session close (same value the ±% engine uses) |
| Yesterday % | yesterday's close vs the close of the session before it (daily bars) |
| 52w high / low | daily bars, prior 365 days, excluding today |
| Volume multiplier | candle volume ÷ EMA of the previous candles |

## Digest (pooled alerts)

So these alerts don't bury the ±% move alerts, signals can be pooled into **one message per N-minute window**:

| Env | Default | Meaning |
|-----|---------|---------|
| `SIGNAL_DIGEST_52W_MINUTES` | `15` | Pool 52-week high/low alerts (`0` = send each at once) |
| `SIGNAL_DIGEST_VOLUME_MINUTES` | `0` | Pool volume-spike alerts (`0` = send each at once) |

- **Windows follow the IST clock** (10:00–10:15, 10:15–10:30, …). A window is sent on the first tick after it ends, so a breakout at 10:02 arrives around 10:15. That delay is the cost of pooling.
- **Empty windows send nothing.** If both types use the same minutes, they share one digest message.
- **What's visible:** one summary line per type, e.g. `🚀 52W highs (3): RELIANCE +2.1%, SBIN +1.4%, …`, capped at 12 names plus a count.
- **What's collapsed:** the per-stock details (price, level broken, day %, 52w high/low, yesterday's close and %) and the `/confirm` ids sit in a Telegram *expandable quote*. Tap it to open.
- **Offers are created when the digest is sent**, so the confirm TTL and the MIS cutoff count from then. A stock detected before the cutoff but sent after it shows `No order offer: …`.
- **Long digests** split into parts `(1/2)`, `(2/2)` under Telegram's 4,096-character limit.
- **On shutdown**, whatever is still waiting is sent straight away rather than dropped.

## Ordering from Telegram

When `SIGNAL_ORDERS_ENABLED=true` and `TRADE_MODE` is not `off`, each signal creates **two pending orders**, one per side in `SIGNAL_ORDER_SIDES` (default `buy,sell`). Reply with `/confirm <id>` for the side you want, or `/cancel <id>`.

| Env | Default | Meaning |
|-----|---------|---------|
| `SIGNAL_ORDERS_ENABLED` | `true` | Attach `/confirm` offers to signal alerts |
| `SIGNAL_ORDER_SIDES` | `buy,sell` | Which sides to offer: `buy`, `sell`, or `buy,sell` |

On confirm:

1. **Sizing** uses the **current** LTP, not the price when the alert fired, with the usual MIS margin sizing (`TRADE_MARGIN_INR`, see [ORDERS.md](ORDERS.md#sizing)).
2. A MARKET entry is placed (`TRADE_PRODUCT`, default MIS).
3. A protective **SL-Limit** is attached at `TRADE_STOP_LOSS_PCT`:
   - **BUY**: a SELL stop below the fill. The cost-to-cost trail and the upper-circuit exit apply as usual.
   - **SELL (short)**: a BUY stop above the fill. The trail and circuit-exit rules are long-only and skip shorts.
4. Guards:
   - No offers, and no confirms, after the MIS cutoff: **15:12** for F&O stocks, **15:25** for the rest. The alert then says `No order offer: past MIS entry cutoff …`.
   - Entries count toward `TRADE_MAX_ORDERS_PER_DAY`.
   - One open position per symbol, so once one side is confirmed, the other side's offer is refused.

`TRADE_MODE` sets what a confirm actually does:

| `TRADE_MODE` | Signal alerts | `/confirm` |
|--------------|---------------|------------|
| `off` | alerts only, no offers | — |
| `dry_run` | alerts + offers | pretend order (nothing reaches Kite) |
| `confirm` / `auto` | alerts + offers | **live** order |

Signal orders **always** need `/confirm`, even with `TRADE_MODE=auto`. `auto` only applies to the ±% threshold trades.

Offers expire after `TRADE_CONFIRM_TTL_MINUTES`. Unlike threshold orders, an expired signal offer sends **no** `⏱ EXPIRED` alert, because ignoring offers is the normal case.

When offers are on, the Telegram confirm listener runs in `dry_run`/`auto` mode as well as `confirm`. The one-watcher-per-bot-token rule in [ORDERS.md](ORDERS.md#only-one-watcher-ever-confirm-silently-dies-otherwise) now applies in every mode that has offers.

The CLI fallback works too: `uv run nse-alert pending` lists offers, and `uv run nse-alert confirm <id>` sizes the order off a fresh Kite quote.

## Related code

- `src/nse_alert/signals.py`: candle builder, EMA, 52w detector, message format, history seeder
- `src/nse_alert/cli.py`: `watch` wiring, `_confirm_pending` (lazy sizing)
- `src/nse_alert/orders.py`: `build_short_stop_request`, `place_entry_with_stop(protect_short=…)`
- `src/nse_alert/session.py`: per-symbol close / MIS cutoff (CAS)
- `src/nse_alert/feed.py`: `KiteFeed(mode="quote")`
- Tests: `tests/test_signals.py`
