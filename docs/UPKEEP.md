# Documentation upkeep

Use this checklist whenever you change behaviour users (or future-you) need to know about.

## When you must update docs

| Code change | Update these |
|-------------|--------------|
| New / renamed env var | `.env.example` + any doc that lists env vars ([ALERTS](ALERTS.md), [ORDERS](ORDERS.md), [SCREENER](SCREENER.md), [LOGIN](LOGIN.md)) |
| Alert rules (thresholds, F&O, ASM, report shape) | [ALERTS.md](ALERTS.md), short blurb in [../README.md](../README.md) |
| Login / redirect / token flow | [LOGIN.md](LOGIN.md) — if **unchanged**, say so in the PR description |
| Trading modes / SL / sizing / caps | [ORDERS.md](ORDERS.md), [TESTING_ORDERS.md](TESTING_ORDERS.md), `.env.example` |
| EOD TA screener filters / Excel | [SCREENER.md](SCREENER.md), `.env.example`, [../deploy/CLOUD.md](../deploy/CLOUD.md) cron if needed |
| Order test plan / handoff checklist | [TESTING_ORDERS.md](TESTING_ORDERS.md) |
| Deploy / ports / IP whitelist | [../deploy/CLOUD.md](../deploy/CLOUD.md) |
| Provider cost / free tier advice | [../deploy/COST.md](../deploy/COST.md) |
| New module or CLI command | [ARCHITECTURE.md](ARCHITECTURE.md), [../README.md](../README.md) CLI section, [README.md](README.md) index if new doc |
| New top-level doc file | Add a row to [README.md](README.md) (this folder’s index) **and** [../README.md](../README.md) doc map |

## How to write here

1. **Link, don’t duplicate** — put the full procedure in one file; other pages get 1–3 sentences + a link.
2. **README stays short** — overview + table of links + quick start only.
3. **Match reality** — run or read the code path before documenting defaults (especially `THRESHOLD_PCT`, `FO_ONLY_THRESHOLDS`, trade defaults).
4. **Tables for options; numbered lists for click-paths** (AWS/Oracle consoles).
5. **Call out login impact** — every feature doc should state whether daily Kite login changes (`none` is a valid answer).
6. **No secrets** — examples use placeholders (`…`, `YOUR_ACCESS_TOKEN`, `STATIC_IP`).
7. **Date-sensitive rules** — note years for SEBI/Zerodha IP rules when mentioning compliance.

## PR checklist (docs)

- [ ] `.env.example` still matches `Settings` in `config.py`
- [ ] `docs/README.md` index lists any new doc
- [ ] Root `README.md` doc map updated if needed
- [ ] Fake/stale thresholds or modes removed
- [ ] `uv run pytest` still passes if behaviour changed

## Creating a new guide

1. Add `docs/YOUR_TOPIC.md` with a clear H1 and “Related code” section at the bottom.
2. Link it from `docs/README.md` and the root README doc map.
3. Keep CONTRIBUTING focused on *how to change code*; put product behaviour in `docs/`.
