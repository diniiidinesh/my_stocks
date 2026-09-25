# Signals: volume spikes and 52-week breakouts

These run inside `watch`, alongside the ±% threshold alerts ([ALERTS.md](ALERTS.md)), on the same tick stream. Each alert includes `/confirm` commands for a BUY and a SELL order on that stock. Nothing is ever placed unless you confirm it.

**Login impact:** none. They use the same daily Kite token. They need `FEED_MODE=kite`, because the mock feed has no volume or history, so signals stay idle there.

## Volume spike

| Env | Default | Meaning |
|-----|---------|---------|
| `VOLUME_SPIKE_ENABLED` | `true` | Turn the rule on or off |
| `VOLUME_SPIKE_TIMEFRAMES` | `5` | Candle minutes, comma-separated. Allowed values: `1,3,5,10,15,30,60`, e.g. `5,15` |
| `VOLUME_SPIKE_EMA_PERIOD` | `21` | EMA length, in candles of the same timeframe |
| `VOLUME_SPIKE_MULT` | `2` | Fires when candle volume is **more than** N × EMA |
| `VOLUME_SPIKE_SKIP_OPENING_CANDLE` | `true` | Ignore the 09:15 candle |

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
