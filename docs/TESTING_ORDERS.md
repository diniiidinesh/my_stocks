# Order / auto-trade test plan

Detailed cases for the test strategy: **BUY on +13% UP**, then **SL-Limit SELL** (trigger 2% below entry, limit a few ticks lower).

Related code: `orders.py`, `confirm_bot.py`, `cli.py` (`watch` / `order` / `confirm`).  
Related docs: [ORDERS.md](ORDERS.md).

---

## Modes under test

| Mode | What should happen on +13% UP |
|------|-------------------------------|
| `off` | Alert only — no order |
| `dry_run` | Fake entry + fake SL-Limit; Telegram/log messages; **no Kite call** |
| `confirm` | Pending id → wait for `CONFIRM <id>` / CLI → then place (live or dry depending on confirm force) |
| `auto` | Immediately place entry + SL-Limit via Kite |

---

## Automated cases (run in CI / this environment)

Command:

```bash
uv run pytest -q tests/test_orders.py tests/test_engine.py tests/test_report.py
```

| ID | Case | Expected | Covered by |
|----|------|----------|------------|
| TC-FILTER-01 | `TRADE_MODE=off` | `should_trade_alert` false | `test_tc_filter_off_mode_never_trades` |
| TC-FILTER-02 | UP/DOWN × threshold × sides matrix | Only matching combos true | `test_tc_filter_should_trade_matrix` |
| TC-FILTER-03 | Engine fires 4% then 13%; only 13% passes gate | 4% ignored for trade | `test_tc_filter_alert_engine_to_trade_gate` |
| TC-DRY-01 | dry_run entry + 2% SL-Limit | Both ok; position open; trigger+limit rounded | `test_tc_dry_entry_and_sl_and_position` |
| TC-DRY-02 | Second BUY same symbol | Blocked “Open position” | `test_tc_dry_blocks_second_entry_while_open` |
| TC-DRY-03 | Daily cap (entries only; stop excluded) | Further entries blocked; stop still places | `test_tc_dry_daily_cap_blocks_further_entries` |
| TC-DRY-04 | SELL entry | No SL attached | `test_tc_dry_sell_entry_skips_sl` |
| TC-DRY-05 | `place` with mode off | Failure message | `test_tc_mode_off_place_returns_false` |
| TC-LIVE-MOCK-01 | `auto` with fake Kite (cap=1) | MARKET BUY then SL SELL with trigger+limit | `test_tc_live_mock_auto_places_market_then_sl_limit` |
| TC-LIVE-MOCK-02 | Kite raises | Failure result, no crash | `test_tc_live_mock_kite_error_returns_failure` |
| TC-LIVE-MOCK-03 | Missing credentials | Failure mentioning keys | `test_tc_live_requires_credentials` |
| TC-SIZE-01 | Margin sizing via fake `order_margins` | qty = budget / margin_per_share | `test_tc_margin_sizing_uses_order_margins` |
| TC-SIZE-02 | Margins API unavailable | Fallback leverage × budget / price | `test_tc_margin_sizing_fallback_leverage` |
| TC-CONFIRM-01 | Pending add / cancel / list | Lifecycle ok | `test_tc_confirm_pending_lifecycle` |
| TC-CONFIRM-02 | Pending TTL expired | Dropped from list, status expired | `test_tc_confirm_pending_expires` |
| TC-CONFIRM-03 | Telegram CONFIRM/CANCEL regex | Parses ids; `/confirm@Bot`; ignores junk | `test_tc_confirm_telegram_regex` |
| TC-CONFIRM-04 | Group confirm vs DM chat id | Command from group is accepted | `test_tc_confirm_group_message_accepted_even_if_chat_id_differs` |
| TC-SIZE-03 | CLI `order` without `--qty` | Margin size, not TRADE_QTY=1 | `test_cli_order_without_qty_uses_margin_not_trade_qty` |
| TC-SIZE-04 | High margin/share with `TRADE_SIZING=margin` | qty=1 is calculated, note explains | `test_tc_margin_sizing_qty_one_when_margin_per_share_high` |
| TC-BOOK-01 | New calendar day | `placed_count` resets | `test_tc_book_resets_on_new_calendar_day` |
| TC-REPORT-01 | EOD close % + hold counts | Close section + held/fired | `tests/test_report.py` |

### Local mock end-to-end (no Kite money)

```bash
cp .env.example .env
# set at least:
# TRADE_MODE=dry_run
# TRADE_ON_THRESHOLDS=13
# TRADE_SIDES=up
# THRESHOLD_PCT=4,7,11,13
# FEED_MODE=mock

uv run nse-alert watch --feed mock --threshold 4,7,11,13
```

**Expect:** DEMO13 ramps past +13%; logs show `DRY RUN: would BUY` and `DRY RUN: would … SL trigger=… limit=…`; `.nse_alert/orders.json` has history + open position.

Optional CLI dry order:

```bash
uv run nse-alert order buy RELIANCE --qty 1 --dry-run
```

---

## Handoff — what this environment **cannot** fully test

You must run these on your machine / Lightsail VM with a real Kite session.

### Preconditions

1. Paid Kite Connect + valid **today’s** `KITE_ACCESS_TOKEN` (`nse-alert login`).
2. Static IP **whitelisted** on Kite (required for live orders from Apr 2026).
3. Funds / margin enough for about **`TRADE_MARGIN_INR`** of MIS (default ₹10k), or keep `dry_run` until ready. Use `TRADE_SIZING=fixed` + `TRADE_QTY=1` for the smallest live test.
4. Prefer market hours for live placement: before **15:12 IST** for F&O stocks and 15:25 for the rest. The watcher refuses new MIS entries after those times (Aug 2026 closing auction).
5. Start with **`TRADE_MODE=dry_run`** on the VM once, then **`confirm`**, only then **`auto`**.

### Manual / live cases

| ID | Steps | Pass criteria | Risk |
|----|-------|---------------|------|
| TC-HAND-01 | VM: `TRADE_MODE=dry_run`, live `FEED_MODE=kite`, small universe | Real +13% (or wait) produces dry-run Telegram/log only; **no** order in Kite; sizing note shows qty for ~₹10k margin | None |
| TC-HAND-02 | `TRADE_MODE=confirm`, force a pending via alert or simulate | Telegram shows `/confirm <id>` with qty + sizing; `pending` lists it; `/cancel` removes it. Group: slash command + privacy off. | None until confirm |
| TC-HAND-03 | Confirm a pending (margin-sized or fixed qty=1) on a liquid name | Kite shows MIS MARKET BUY + SL-Limit SELL; fill then SL; app records position | **Real money** |
| TC-HAND-04 | `TRADE_MODE=auto`, `TRADE_MAX_ORDERS_PER_DAY=1` | On first +13% UP: auto BUY+SL without confirm; second symbol blocked by cap/position rules | **Real money** |
| TC-HAND-05 | ASM-tagged name alerts | Tag appears; if you try live buy, Kite may **reject** ASM (expected broker behaviour) | Awareness |
| TC-HAND-06 | Wrong / expired token | Live place fails with clear error; watch may also fail auth | None |
| TC-HAND-07 | IP not whitelisted | Order API error from Zerodha; alerts may still work | None |
| TC-HAND-08 | Restart mid-day with open position in `orders.json` | Second BUY same symbol still blocked | None |
| TC-HAND-09 | SL trigger behaviour | If price falls to trigger, SL-Limit activates (observe in Kite) | Market |
| TC-HAND-10 | Cost-to-cost trail | After entry, LTP ≥ entry×1.02 → Telegram trail msg; Kite SL trigger ≈ entry | Market |
| TC-HAND-11 | EOD report | After alerts: `nse-alert report` shows close % per scrip + held/fired counts | None |

### Suggested live `.env` (minimal risk)

```env
FEED_MODE=kite
THRESHOLD_PCT=4,7,11,13
FO_ONLY_THRESHOLDS=4
TRADE_MODE=dry_run
TRADE_ON_THRESHOLDS=13
TRADE_SIDES=up
TRADE_STOP_LOSS_PCT=2
TRADE_SIZING=margin
TRADE_MARGIN_INR=10000
TRADE_FALLBACK_LEVERAGE=5
TRADE_QTY=1
TRADE_MAX_ORDERS_PER_DAY=10
TRADE_PRODUCT=MIS
TRADE_STOP_WAIT_SEC=20
TRADE_TRAIL_BREAKEVEN=true
TRADE_TRAIL_BREAKEVEN_PCT=2
CUSTOM_UNIVERSE_FILE=universes/liquid_sample.txt
```

For the tiniest live order instead:

```env
TRADE_SIZING=fixed
TRADE_QTY=1
```

Promote only after TC-HAND-01 passes:

```env
TRADE_MODE=confirm   # then later: auto
```

### Evidence to capture when you hand back results

- Screenshot / copy of Telegram dry-run messages (include sizing line)  
- Kite **Orders** page after confirm/auto (order ids, MIS, qty)  
- `.nse_alert/orders.json` snippet (redact tokens)  
- `report-YYYY-MM-DD.txt` close / hold sections  
- Any error text from failed place (IP / ASM / funds)

---

## Known behaviours to remember while testing

1. **Daily cap counts entries only** — a stop-loss does not consume `TRADE_MAX_ORDERS_PER_DAY` (so `=1` still gets BUY + SL).  
2. Stop is **SL-Limit** on **MIS**: trigger = 2% below **fill price**; limit = trigger − `TRADE_STOP_LIMIT_TICKS` × ₹0.05.  
3. Live auto waits for entry `COMPLETE` (up to `TRADE_STOP_WAIT_SEC`) before placing SL.  
4. When LTP ≥ entry × (1 + `TRADE_TRAIL_BREAKEVEN_PCT`/100), stop is modified to entry (cost-to-cost).  
5. **`TRADE_SIZING=margin`** sizes qty from Kite MIS margin ≈ `TRADE_MARGIN_INR` (not a fixed share count).  
6. Test strategy is **BUY + downside SL only**; DOWN alerts do not trade when `TRADE_SIDES=up`.  
7. Confirm path forces place mode `auto` when executor is not `dry_run` (see `_confirm_pending` in `cli.py`).  
8. Login/token flow is unchanged by order modes — refresh token daily before live tests.  
9. Screener tests: `uv run pytest -q tests/test_screener.py` (see [SCREENER.md](SCREENER.md)).
