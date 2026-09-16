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
uv run pytest -q tests/test_orders.py tests/test_engine.py
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
| TC-CONFIRM-01 | Pending add / cancel / list | Lifecycle ok | `test_tc_confirm_pending_lifecycle` |
| TC-CONFIRM-02 | Pending TTL expired | Dropped from list, status expired | `test_tc_confirm_pending_expires` |
| TC-CONFIRM-03 | Telegram CONFIRM/CANCEL regex | Parses ids; ignores junk | `test_tc_confirm_telegram_regex` |
| TC-BOOK-01 | New calendar day | `placed_count` resets | `test_tc_book_resets_on_new_calendar_day` |

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
3. Funds / holdings enough for **1 share** CNC of a liquid name (or keep `dry_run` until ready).
4. Prefer market hours (9:15–15:30 IST) for live placement.
5. Start with **`TRADE_MODE=dry_run`** on the VM once, then **`confirm`**, only then **`auto`**.

### Manual / live cases

| ID | Steps | Pass criteria | Risk |
|----|-------|---------------|------|
| TC-HAND-01 | VM: `TRADE_MODE=dry_run`, live `FEED_MODE=kite`, small universe | Real +13% (or wait) produces dry-run Telegram/log only; **no** order in Kite order book | None |
| TC-HAND-02 | `TRADE_MODE=confirm`, force a pending via alert or simulate | Telegram shows `CONFIRM <id>`; `pending` lists it; `CANCEL` removes it | None until confirm |
| TC-HAND-03 | Confirm a pending with **qty=1** on a cheap liquid name | Kite shows MARKET BUY + SL (limit) SELL; app records position | **Real money** |
| TC-HAND-04 | `TRADE_MODE=auto`, qty=1, `TRADE_MAX_ORDERS_PER_DAY=1` | On first +13% UP: auto BUY+SL without confirm; second symbol blocked by cap/position rules | **Real money** |
| TC-HAND-05 | ASM-tagged name alerts | Tag appears; if you try live buy, Kite may **reject** ASM (expected broker behaviour) | Awareness |
| TC-HAND-06 | Wrong / expired token | Live place fails with clear error; watch may also fail auth | None |
| TC-HAND-07 | IP not whitelisted | Order API error from Zerodha; alerts may still work | None |
| TC-HAND-08 | Restart mid-day with open position in `orders.json` | Second BUY same symbol still blocked | None |
| TC-HAND-09 | SL trigger behaviour | If price falls to trigger, SL-Limit activates (observe in Kite) — app does not manage fill beyond placement | Market |

### Suggested live `.env` (minimal risk)

```env
FEED_MODE=kite
THRESHOLD_PCT=4,7,11,13
FO_ONLY_THRESHOLDS=4
TRADE_MODE=dry_run
TRADE_ON_THRESHOLDS=13
TRADE_SIDES=up
TRADE_STOP_LOSS_PCT=2
TRADE_QTY=1
TRADE_MAX_ORDERS_PER_DAY=1
TRADE_PRODUCT=CNC
CUSTOM_UNIVERSE_FILE=universes/liquid_sample.txt
```

Promote only after TC-HAND-01 passes:

```env
TRADE_MODE=confirm   # then later: auto
```

### Evidence to capture when you hand back results

- Screenshot / copy of Telegram dry-run messages  
- Kite **Orders** page after confirm/auto (order ids)  
- `.nse_alert/orders.json` snippet (redact tokens)  
- Any error text from failed place (IP / ASM / funds)

---

## Known behaviours to remember while testing

1. **Daily cap counts entries only** — a stop-loss does not consume `TRADE_MAX_ORDERS_PER_DAY` (so `=1` still gets BUY + SL).  
2. Stop is **SL-Limit**: trigger = 2% below entry; limit = trigger − `TRADE_STOP_LIMIT_TICKS` × ₹0.05.  
3. Test strategy is **BUY + downside SL only**; DOWN alerts do not trade when `TRADE_SIDES=up`.  
4. Confirm path forces place mode `auto` when executor is not `dry_run` (see `_confirm_pending` in `cli.py`).  
5. Login/token flow is unchanged by order modes — refresh token daily before live tests.
