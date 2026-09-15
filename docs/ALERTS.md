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

## Telegram / console message shape

```text
▲ SYMBOL +14.00% (crossed ±13%)
LTP: … | Prev close: …
Direction: UP
Tags: F&O · ASM          # only when true
Time (UTC): …
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
- ASM-tagged alert count (if any)
- Multi-level time gaps (IST)

## Universe (what is watched)

Live default (no NSE website scrape):

1. NSE cash EQ from `kite.instruments("NSE")` (mainboard filter)
2. Liquidity: `volume × LTP` ≥ `MIN_TURNOVER_CR` (₹ crore)
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
| `report.py` | EOD UP/DOWN counts and gaps |
| `notify/telegram.py` | Message formatting |
