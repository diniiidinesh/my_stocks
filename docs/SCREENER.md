# EOD technical screener

Daily-chart screener, **separate** from intraday ±% alerts and MIS orders.

**Login impact:** none required for Yahoo history/market-cap; Kite token improves history + turnover filter. Delivery % comes from NSE bhavcopy (no Kite).

All indicators use the **daily** chart only (Kite `"day"` / Yahoo `1d`).

## What it does

1. Builds a universe from **Nifty 500 ∪ Smallcap 250**, then keeps names with
   **market cap ≥ `SCREEN_MIN_MARKET_CAP_CR`** (default ₹5000 Cr) and
   **turnover ≥ `SCREEN_MIN_TURNOVER_CR`** (default ₹10 Cr via Kite quotes).
2. Pulls **daily OHLCV** (Kite historical when logged in, else Yahoo) and caches under
   `STATE_DIR/screener/history/`.
3. Loads NSE **full bhavcopy** for delivery % on volume-spike days
   (`STATE_DIR/screener/bhavcopy/`).
4. Scores each name and writes an Excel workbook + Telegram summary **after 15:40 IST**.

## Rules

| Rule | Default | Role |
|------|---------|------|
| Price > SuperTrend (10, 3) | on | **Mandatory** |
| EMA20 > EMA50 > EMA200 | on | **Mandatory** |
| Volume > 1.5× **volume EMA** on any of last 20 sessions | on | Optional (`SCREEN_REQUIRE_VOLUME`) |
| ADX(14) > 25 | on | Optional |
| RSI(14) between 40–60 | on | Optional |
| MACD line > 0 | on | Optional |
| Within X% of 52-week high | 5% | Optional |
| Delivery ≥ Y% on a volume-spike day | 40% | Optional (`SCREEN_REQUIRE_DELIVERY`) |

### Volume spikes

- Compare each session’s volume to that day’s **volume EMA** (`SCREEN_VOLUME_EMA_PERIOD`, default 20).
- Spike if `volume > SCREEN_VOLUME_MULT × volume_EMA` (default **1.5×**).
- Look back `SCREEN_LOOKBACK_DAYS` sessions; list **every** hit as `T-0`, `T-1`, … with multiple and vol vs EMA.

### Delivery (optional)

- NSE full bhavcopy `DELIV_PER` for each spike date.
- **Pass if any** spike day with known delivery is ≥ `SCREEN_MIN_DELIVERY_PCT`.
- Days with missing delivery are **ignored**.
- Excel: `deliv_pct_max_on_spikes`, `deliv_spike_days`, `deliv_spike_detail`, `pass_delivery`.

Same-day (`T-0`) delivery may be unavailable until the evening bhavcopy is published.

## Ranking (Excel)

**Values first; pass/fail flags on the right.** Rows sorted:

1. `all_pass` first  
2. then `mandatory_pass`  
3. then higher optional score  
4. then closer to 52-week high  
5. then stronger best volume spike  

Sheets: `Summary`, `AllPass`, `MandatoryHits`, `AllRanked`, `Config`.

**Sample (dummy data):** [../samples/sample-eod-screener-report.xlsx](../samples/sample-eod-screener-report.xlsx)

## Run

```bash
# After 15:40 IST (or pass --force)
uv run nse-alert screen

# Smoke test on a few names
uv run nse-alert screen --force --max-symbols 20 --no-telegram
```

Telegram (when `TELEGRAM_*` is set) receives a short summary **and** the `.xlsx` attachment.

### Cron (Lightsail, IST)

```cron
15 16 * * 1-5  cd /opt/nse-alert && uv run nse-alert screen >> /var/log/nse-screen.log 2>&1
```

Refresh the Kite access token on trading days if you prefer Kite history (`SCREEN_PREFER_KITE_HISTORY=true`).

## Config

All `SCREEN_*` keys are in [../.env.example](../.env.example). Toggle optional filters with
`SCREEN_REQUIRE_VOLUME`, `SCREEN_REQUIRE_ADX`, `SCREEN_REQUIRE_RSI`,
`SCREEN_REQUIRE_MACD`, `SCREEN_REQUIRE_NEAR_52W`, `SCREEN_REQUIRE_DELIVERY`
(`SCREEN_MIN_DELIVERY_PCT`).

Optional market-cap override CSV: `SCREEN_MARKET_CAP_FILE` with columns
`symbol,market_cap_cr`.

Optional symbol list for this command only: `SCREEN_CUSTOM_UNIVERSE_FILE`
(does **not** reuse the watcher’s `CUSTOM_UNIVERSE_FILE`).

## Related

- Intraday alerts: [ALERTS.md](ALERTS.md)  
- Orders: [ORDERS.md](ORDERS.md)  
- Deploy cron: [../deploy/CLOUD.md](../deploy/CLOUD.md)  
- Code: `src/nse_alert/screener/`
