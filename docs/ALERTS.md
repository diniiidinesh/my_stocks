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

## Related code

| Module | Role |
|--------|------|
| `engine.py` | Thresholds, FO-only gate, ASM/F&O flags on `Alert` |
| `surveillance.py` | NFO underlyings + ASM sheet fetch |
| `universe.py` | Cash EQ instruments + quote screen |
| `report.py` | EOD UP/DOWN counts, per-scrip close %, hold-level counts, gaps |
| `notify/telegram.py` | Message formatting |
