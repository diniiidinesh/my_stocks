# Backlog

Parked ideas with enough detail to pick up later. Newest items first.

---

## News explain follow-up on threshold alerts

**Status:** parked (design agreed; not implemented)  
**Related:** [ALERTS.md](ALERTS.md), `cli.py` `on_tick`, `notify/telegram.py`

### Goal

When a stock crosses a configured threshold, send the usual price alert immediately, then (async) search recent free RSS news for that company/industry and post a **Telegram follow-up** with a short “why it moved” note, sources, publish times, and links.

Rationale for follow-up (not inline): price alert stays fast so you can enter if you already know the story; news arrives when ready.

### Agreed product decisions

| Choice | Decision |
|--------|----------|
| Master switch | Env flag, **default off** |
| News provider | **Free RSS** (e.g. Google News RSS) — no paid news API |
| Summary styles | Support **both**: `headlines` (default) and `rewrite` (LLM) |
| Which alerts | **Configurable thresholds** (independent of which levels fire alerts) |
| Delivery | Separate Telegram **follow-up** after the move message |
| Links | Include article URLs so you can deep-read |
| Soft-fail | News failures never block alerts or trades (same pattern as ASM) |

### Proposed env / config

```bash
NEWS_EXPLAIN_ENABLED=false          # master switch
NEWS_EXPLAIN_THRESHOLDS=7,11,13     # only enrich these crosses (example)
NEWS_EXPLAIN_STYLE=headlines        # headlines | rewrite
NEWS_EXPLAIN_LOOKBACK_HOURS=24
NEWS_EXPLAIN_MAX_SOURCES=3

# rewrite only:
NEWS_EXPLAIN_LLM_API_KEY=
NEWS_EXPLAIN_LLM_MODEL=gpt-4o-mini
```

Notes:

- Thresholds for enrichment are separate from `THRESHOLD_PCT` (e.g. still alert at ±4% F&O-only, but only explain from ±7% up).
- Only run when Telegram is configured (or also echo to console if desired for parity).

### Message shape (follow-up)

```text
Why it moved?
• 1–2 sentence cause, or “No clear recent catalyst found.”

Sources:
• Headline — outlet — published time (IST preferred)
  https://…
• …
```

- **Headlines style:** bullets are the headlines themselves (optional one-line theme); still include links + times.
- **Rewrite style:** LLM writes the “Why” from the fetched articles; sources list stays the same with links.

Prefer Telegram `reply_to_message_id` threaded under the original alert if `sendMessage` returns a `message_id`.

### Costing (as of design discussion)

| Piece | Headlines only | Full rewrite (LLM) |
|-------|----------------|--------------------|
| News fetch (Google News RSS) | ₹0 | ₹0 |
| Summarize | ₹0 | API tokens only |
| Extra infra | none | LLM API key in `.env` |
| Latency after alert | ~1–3s (RSS) | ~2–6s (RSS + model) |

**Rewrite ballpark (gpt-4o-mini):** ~$0.15 / 1M input, ~$0.60 / 1M output → roughly **$0.0002–0.0004 per alert** (≈ ₹0.02–0.04). At 20–100 enriched alerts/day, monthly LLM cost is typically well under ₹100. Cost is not the main constraint; complexity and soft-fail behaviour are.

Default ship path: **headlines**; keep **rewrite** behind `NEWS_EXPLAIN_STYLE=rewrite`.

### Implementation sketch

1. **Config** — add fields to `Settings` in `config.py` + `.env.example` (mirror `ASM_ENABLED` style).
2. **News module** — e.g. `nse_alert/news/`:
   - RSS search by symbol / company name (map token→name from Kite instruments if available)
   - Parse title, link, published time, outlet; filter by lookback; cap at `MAX_SOURCES`
   - Soft-fail on network/parse errors
3. **Summarize**
   - `headlines`: format only
   - `rewrite`: call LLM with titles + snippets; fall back to headlines on API failure
4. **Wire** — in `cli.py` `on_tick`, after `notifier.send(alert)`:
   - If enabled and `alert.threshold_pct` in configured list → schedule background follow-up (do not block the tick / trade path)
   - Send via `TelegramNotifier.send_text` (extend to support `reply_to_message_id` if threading)
5. **Docs / tests**
   - Document in [ALERTS.md](ALERTS.md); keep this backlog item marked done or remove when shipped
   - Unit tests: disabled = no network; formatter with links; threshold gate; soft-fail

### Caveats to remember

- Correlation ≠ causation — prefer “possible drivers” language; allow “No clear recent catalyst found.”
- Only enrich real alerts (already deduped per symbol × direction × threshold × day).
- Free RSS quality/rate limits vary; keep lookback tight (24–48h).
- Login impact: **none** (news does not use Kite).

### Out of scope (unless revisited)

- Paid news APIs (NewsAPI, etc.)
- Slack/Discord (project is Telegram-only today)
- Blocking the price alert until news is ready
