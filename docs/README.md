# Documentation index

How to **find**, **read**, and **update** docs in this repository.

## Read path (by goal)

| I want to… | Open |
|------------|------|
| Run a demo without credentials | [../README.md](../README.md) → Quick start |
| Understand alert rules (4% F&O, ASM, EOD close report) | [ALERTS.md](ALERTS.md) |
| Set up / renew Kite login | [LOGIN.md](LOGIN.md) |
| Enable MIS test orders (sizing, SL trail) | [ORDERS.md](ORDERS.md) |
| Run the EOD TA screener (Excel + Telegram) | [SCREENER.md](SCREENER.md) |
| Test auto/dry-run/confirm orders | [TESTING_ORDERS.md](TESTING_ORDERS.md) |
| Pick a cloud VM / free tier | [../deploy/COST.md](../deploy/COST.md) |
| Install on Lightsail / Oracle | [../deploy/CLOUD.md](../deploy/CLOUD.md) |
| See where code lives | [ARCHITECTURE.md](ARCHITECTURE.md) |
| Change code safely | [../CONTRIBUTING.md](../CONTRIBUTING.md) |
| Update docs after a code change | [UPKEEP.md](UPKEEP.md) |

## Doc ownership (what belongs where)

| Location | Content |
|----------|---------|
| `README.md` | Short product overview + links only — no long procedures |
| `docs/*.md` | Feature behaviour, operator guides, architecture |
| `deploy/*.md` | Cloud/VPS install, networking, cost comparison |
| `.env.example` | Canonical list of env vars (with comments) |
| `CONTRIBUTING.md` | Dev workflow, tests, PR expectations |
| `docs/UPKEEP.md` | Checklist when docs must change with code |

## Writing rules (summary)

1. **One source of truth** — put procedural detail in one doc; link from elsewhere.
2. **Match `.env.example`** — every documented env var must exist there (or be removed from both).
3. **Login is stable** — if a feature doesn’t change token flow, say so explicitly (see UPKEEP).
4. **Prefer tables** for options/comparisons; prefer numbered lists for click-paths.
5. **No secrets** — never paste API keys, tokens, or live IPs into docs.

Full checklist: [UPKEEP.md](UPKEEP.md).
