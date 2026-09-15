# Contributing

Guidelines for **reading** and **changing** this repository.

## How to read the repo

1. Start at **[README.md](README.md)** for what the product does.
2. Use **[docs/README.md](docs/README.md)** as the map to feature docs.
3. For code orientation, open **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** before diving into `src/`.
4. Env var source of truth: **[.env.example](.env.example)** ↔ `src/nse_alert/config.py`.

Suggested first-read order for newcomers:

```text
README → docs/ALERTS → docs/LOGIN → docs/ARCHITECTURE → engine.py + cli.py
```

## Dev setup

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync
cp .env.example .env
uv run pytest
uv run nse-alert watch --feed mock --threshold 4,7,11
```

Requires Python **3.12+** (see `pyproject.toml`).

## Making changes

| Do | Don’t |
|----|--------|
| Keep PRs focused on one concern | Mix unrelated refactors with feature work |
| Add/adjust tests under `tests/` for behaviour changes | Rely only on manual “it worked once” |
| Update docs per [docs/UPKEEP.md](docs/UPKEEP.md) | Leave `.env.example` out of sync with `Settings` |
| Prefer small, named modules | Stuff new domains into `cli.py` |
| Soft-fail optional enrichment (e.g. ASM sheet) | Crash `watch` because a side channel failed |

### Feature areas → primary files

| Area | Touch first |
|------|-------------|
| Thresholds / dedupe | `engine.py`, `tests/test_engine.py` |
| F&O / ASM | `surveillance.py`, `cli.py` (wiring), `tests/test_surveillance.py` |
| Reports | `report.py`, `tests/test_report.py` |
| Telegram text | `notify/telegram.py` |
| Orders | `orders.py`, `confirm_bot.py`, `tests/test_orders.py` |
| Login | `login_ui.py`, `envfile.py` |
| Universe | `universe.py` |
| Cloud install text | `deploy/CLOUD.md`, `deploy/COST.md` |

## Tests

```bash
uv run pytest
uv run pytest -q tests/test_engine.py
```

Mock feed needs no Kite credentials. Do not commit `.env` or `.nse_alert/`.

## Commits & PRs

- Clear commit messages that say *why* (what behaviour changed).
- PR description should note:
  - user-visible behaviour
  - whether **login/token flow** changed
  - which docs were updated
- Link related issues when applicable.

## Security / compliance

- Never commit API keys, access tokens, or customer chat IDs.
- Treat order placement as dangerous: default examples stay on `dry_run` / tiny qty.
- Document static-IP requirements when touching order paths.

## Questions

If behaviour is unclear, check `docs/` first, then the module listed in ARCHITECTURE. Prefer updating the doc once you learn the answer (see UPKEEP).
