# Alerts, F&O filter, ASM tags, and reports

## Day-move formula

```text
change_pct = (LTP / previous_close - 1) * 100
```

An alert fires when `|change_pct|` reaches a configured threshold, **once per**  
`symbol × direction (UP|DOWN) × threshold × calendar day`.

State file: `.nse_alert/fired.json` (events + dedupe keys).

### Multi-level jumps

If price jumps past several levels in one tick (e.g. +3% → +12% with `4,7,11`), each newly crossed level fires in ascending order. Already-fired levels are skipped.

Volume-spike and 52-week breakout alerts are a separate rule set: see [SIGNALS.md](SIGNALS.md).

## Market close (CAS, since 3 Aug 2026)

SEBI's Closing Auction Session changed the NSE cash-market close **per stock**:

| Stocks | Continuous trading | After that | Zerodha MIS square-off |
|--------|--------------------|------------|------------------------|
| **F&O stocks** (CAS) | 09:15–**15:15** | Closing auction 15:15–15:35 (orders 15:20–15:30, random close 15:28–15:30, ±3% band); close price = auction price | **15:12** |
| Non-F&O stocks | 09:15–15:30 (unchanged) | VWAP close as before | **15:25** |

Post-close session for both is now 15:50–16:00. F&O *contracts* trade until 15:40.

What the watcher does with it (`session.py`):

- **No fresh intraday entries after the MIS cutoff.** This applies to auto trades, `confirm` prompts, signal offers, and `/confirm` replies. Alerts still fire, and a `⏰ … past MIS entry cutoff` note explains why no order was made. The cutoffs are configurable (`MIS_CUTOFF_CAS_HHMM`, `MIS_CUTOFF_NON_CAS_HHMM`) in case Zerodha moves them. CNC entries are allowed until the stock's continuous close.
- **Volume candles and 52-week checks stop at each stock's continuous close.** That's 15:15 for F&O stocks, so auction prints never count as a spike or a breakout.
- The F&O list comes from Kite `instruments("NFO")`, which is now always loaded in live mode. If it fails to load, **every** stock gets the earlier CAS times.
- Unchanged: ±% threshold *alerts* still fire until 15:30 (and on auction prints), feed-health alerts cover 09:15–15:30, and the EOD report/screener still run at 15:40, after the auction has matched.
- The mock feed ignores the cutoffs, so demo trades still work at any hour.

Sources: [Zerodha market timings](https://support.zerodha.com/category/trading-and-markets/trading-faqs/market-sessions/articles/what-are-the-market-timings), [Zerodha auto square-off timings](https://support.zerodha.com/category/trading-and-markets/trading-faqs/market-sessions/articles/intraday-auto-square-off-timings).

## Thresholds

| Env | Example | Meaning |
|-----|---------|---------|
| `THRESHOLD_PCT` | `4,7,11,13` | All alert levels (absolute %) |
| `FO_ONLY_THRESHOLDS` | `4` | Levels that apply **only** to F&O underlyings |

CLI override for one run:

```bash
uv run nse-alert watch --threshold 4,7,11,13
```

### F&O-only levels (e.g. ±4%)

**Source:** Kite `instruments("NFO")` → equity **FUT** underlyings (`name` field).

- Names **in** that set → all of `THRESHOLD_PCT` apply (including 4%).
- Names **not** in that set → levels listed in `FO_ONLY_THRESHOLDS` are skipped; higher levels (7/11/13) still apply.

If NFO load fails at startup, FO-only filtering is disabled for that run (warning in logs) so alerts keep working.

**Login impact:** none — uses the same daily access token as quotes/WebSocket.

## ASM tagging

**Not available in Kite Connect APIs.** Zerodha documents ASM/GSM lists on their Utilities RMS spreadsheet instead.

| Env | Default | Meaning |
|-----|---------|---------|
| `ASM_ENABLED` | `true` | Fetch & tag ASM symbols |
| `ASM_SHEET_URL` | (built-in Zerodha sheet URL) | Optional override |

Behaviour:

- On `watch` start (live), fetch long-term + short-term ASM symbols.
- Cache to `.nse_alert/asm_symbols.txt` for the session/day.
- Telegram / console alerts show `Tags: ASM` (and `F&O` when applicable).
- Soft-fail: if the sheet is unreachable, alerts still run **without** ASM tags.

**Login impact:** none — ASM does not use Kite.

Reference: [Zerodha Resources → Utilities](https://zerodha.com/resources/) → consolidated RMS list.

## Telegram destination (DM vs group)

`TELEGRAM_CHAT_ID` is where **alerts** are sent. A private chat id is a positive number; a group/supergroup id is **negative** (often `-100…`).

If you added the bot to a group but confirms do nothing, see [ORDERS.md](ORDERS.md) (slash `/confirm <id>`, BotFather privacy, `nse-alert telegram-chats`).

**Login impact:** none — Telegram is independent of Kite.

## Telegram / console message shape

```text
▲ SYMBOL +14.00% (crossed ±13%)
LTP: … | Prev close: …
Direction: UP
Tags: F&O · ASM          # only when true
Time (IST): …
```

## End-of-day report

Emitted when `watch` exits, and anytime via:

```bash
uv run nse-alert report
uv run nse-alert report --date 2026-09-15 --telegram
```

File: `.nse_alert/report-YYYY-MM-DD.txt`

Includes:

- Total alerts + unique symbols
- **Positive movers (UP):** unique symbols + alert count
- **Negative movers (DOWN):** unique symbols + alert count
- Per-threshold **UP / DOWN** counts
- **Closed still at/above alert level:** per threshold, how many unique scrips still held the level into the close (UP close≥thr / DOWN close≤−thr), shown as `held/fired`
- **Per-scrip close after alert:** each alerted name’s day close % vs prev close (and the % when it last alerted)
- ASM-tagged alert count (if any)
- Multi-level time gaps (IST)

Close prices come from **Kite quotes** when `KITE_*` is set; otherwise the report falls back to the last alert LTP (noted in the text). For a true EOD close, run `nse-alert report` after market close with a valid token.

**Login impact:** none for report formatting — same daily token; Kite closes need a valid token.

### Daily heartbeat

`nse-alert report --telegram` sends a second message right after the day
report: a `📊 Daily heartbeat` summarizing mode, feed, alerts fired (by
threshold), orders placed/expired, and whether the screener has run yet.
This folds into the existing 15:40 IST report cron — no separate schedule.
The content matters less than the fact that it arrives: every incident in
[RCA-2026-09-17.md](RCA-2026-09-17.md) was something that silently *didn't*
happen, so **a missing heartbeat by ~16:00 IST is itself the signal to go
check on the VM**, not the message content.

### Example snippets

```text
Closed still at/above alert level (unique scrips):
  ±7%  →  UP closed≥level: 3/5  DOWN closed≤-level: 1/2
  (close from Kite quote LTP/OHLC)

Per-scrip close after alert:
  AAA UP  alerted ±4,7% (at +8.20%)  →  close +6.10% (prev=100.00 close=106.10)
```

## Universe (what is watched)

Live default (no NSE website scrape):

1. NSE cash EQ from `kite.instruments("NSE")` (mainboard filter)
2. Liquidity: **prior-session** turnover ≥ `MIN_TURNOVER_CR` (₹ crore), read
   from the last completed NSE session's bhavcopy — not today's live volume,
   which is 0 before the open and would leave the universe empty. Falls back
   to live `volume × LTP` if the bhavcopy is unavailable; the startup log
   line `Liquidity screen: prior-session|live` says which was used.
3. Prev close ≥ `MIN_PRICE`

For first live runs, prefer a fixed list:

```env
CUSTOM_UNIVERSE_FILE=universes/liquid_sample.txt
```

Large whole-market `quote` batches often hit Cloudflare; the sample file avoids that.

## Feed health (silence ≠ safety)

KiteTicker retries a dropped WebSocket connection on its own — that retry
loop is invisible unless someone is reading logs, and a token that dies
mid-session (a 403 close) can leave `watch` "running" for hours while
producing nothing (6h overnight on 2026-09-18, see
[RCA-2026-09-17.md](RCA-2026-09-17.md)). Two guards now exist:

- **Alert on repeated reconnects.** After 3 consecutive reconnect attempts,
  one Telegram alert fires naming the last close code — a `403` during
  market hours means the Kite token died; run the daily login. Edge-
  triggered: it won't alert again until the feed actually reconnects, then
  goes bad again.
- **Exit if reconnection is abandoned.** If KiteTicker exhausts all its
  retries, `watch` alerts and exits non-zero rather than sit idle with a
  dead socket that `docker ps` still shows as healthy.

Both alerts are suppressed outside **Mon–Fri 09:15–15:30 IST** to avoid
overnight/weekend noise — the exit itself still happens regardless of the
hour, so the process never lingers "running but useless."

## Related code

| Module | Role |
|--------|------|
| `engine.py` | Thresholds, FO-only gate, ASM/F&O flags on `Alert` |
| `feed.py` | `KiteFeed`/`MockFeed`; reconnect + dead-feed alerting |
| `surveillance.py` | NFO underlyings + ASM sheet fetch |
| `universe.py` | Cash EQ instruments + quote screen |
| `report.py` | EOD UP/DOWN counts, per-scrip close %, hold-level counts, gaps |
| `notify/telegram.py` | Message formatting |
