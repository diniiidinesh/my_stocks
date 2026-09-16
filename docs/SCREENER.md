# EOD technical screener

Daily-chart screener, **separate** from intraday ±% alerts and MIS orders.

## What it does

1. Builds a universe from **Nifty 500 ∪ Smallcap 250**, then keeps names with
   **market cap ≥ `SCREEN_MIN_MARKET_CAP_CR`** (default ₹5000 Cr) and
   **turnover ≥ `SCREEN_MIN_TURNOVER_CR`** (default ₹10 Cr via Kite quotes).
2. Pulls **daily OHLCV** (Kite historical when logged in, else Yahoo) and caches under
   `STATE_DIR/screener/history/`.
3. Scores each name and writes an Excel workbook + Telegram summary **after 15:40 IST**.

## Rules

| Rule | Default | Role |
|------|---------|------|
| Price > SuperTrend (10, 3) | on | **Mandatory** |
| EMA20 > EMA50 > EMA200 | on | **Mandatory** |
| Volume > 1.5× volume EMA on any of last 20 sessions | on | Optional (`SCREEN_REQUIRE_VOLUME`) |
| ADX(14) > 25 | on | Optional |
| RSI(14) between 40–60 | on | Optional |
| MACD line > 0 | on | Optional |
| Within X% of 52-week high | 5% | Optional |

Volume spikes list **every** hit in the lookback window as `T-0` (latest bar), `T-1`, … with the multiple and absolute volume vs EMA.

## Ranking (Excel)

Sheets are ordered so **`all_pass` (every enabled filter) names sit at the top**:

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
15 16 * * 1-5  cd /opt/nse-alert && /usr/local/bin/uv run nse-alert screen >> /var/log/nse-screen.log 2>&1
```

Refresh the Kite access token on trading days if you prefer Kite history (`SCREEN_PREFER_KITE_HISTORY=true`).

## Config

All `SCREEN_*` keys are in [../.env.example](../.env.example). Toggle optional filters with
`SCREEN_REQUIRE_VOLUME`, `SCREEN_REQUIRE_ADX`, `SCREEN_REQUIRE_RSI`,
`SCREEN_REQUIRE_MACD`, `SCREEN_REQUIRE_NEAR_52W`.

Optional market-cap override CSV: `SCREEN_MARKET_CAP_FILE` with columns
`symbol,market_cap_cr`.

Optional symbol list for this command only: `SCREEN_CUSTOM_UNIVERSE_FILE`
(does **not** reuse the watcher’s `CUSTOM_UNIVERSE_FILE`).

## Related

- Intraday alerts: [ALERTS.md](ALERTS.md)  
- Orders: [ORDERS.md](ORDERS.md)  
- Code: `src/nse_alert/screener/`
