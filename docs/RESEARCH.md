# Research / backtests (VM)

Offline tools to turn ±4/7/11/13 alerts into **testable** intraday rules.

**Login impact:** none new — uses the same daily Kite access token as `watch` / `screen`. Run on the **IP-whitelisted VM** so `historical_data` works.

## Why the VM (not a laptop / this cloud agent)

| Approach | Verdict |
|----------|---------|
| One-off script scp’d to the VM | Works once, then drifts from `AlertEngine` rules |
| **`nse-alert research` in this repo, run on the VM** | **Preferred** — same thresholds/F&O gate, cached bars, git-pulled |
| Yahoo-only on a laptop | OK for coarse daily screens; weak for first-touch alert timing |
| Cloud agent backtest | Usually **not** on the Kite whitelist; no live `fired.json` history |

## Commands

```bash
# On the VM, after: uv run nse-alert login
cd /path/to/my_stocks
git pull
uv sync

# 1) Hypothesis from live alert history (fired.json + fired-YYYY-MM-DD.json)
uv run nse-alert research analyze
uv run nse-alert research analyze --date 2026-09-15 --date 2026-09-16

# 2) List built-in strategy ids
uv run nse-alert research strategies

# 3) Reconstruct alerts from Kite 5-minute bars + simulate strategies
# Start small — historical API is rate-limited (~3 req/s) and chunked.
uv run nse-alert research backtest --days 30 --max-symbols 20

# Wider run (still prefer CUSTOM_UNIVERSE_FILE or a capped max-symbols)
uv run nse-alert research backtest --days 60 --max-symbols 40 \
  --strategies a1,a2_7,a2_11,a_close_7,a_close_13
```

Outputs land under `.nse_alert/research/`:

- `backtest-START_END.txt` — summary table  
- `trades-START_END.csv` — one row per simulated trade  

## Built-in strategies (v1)

| Id | Rule |
|----|------|
| `a1` | Current bot: BUY on **+13 UP**, 2% SL, trail BE at +2%, flat 15:15 IST |
| `a2_7` | Same exits, enter at **+7** |
| `a2_11` | Same exits, enter at **+11** |
| `a_close_7` / `a_close_13` | Buy at alert, **hold to close** (no SL) — baselines |

Costs: default **10 bps** round-trip (`--cost-bps`).

## Archive live days (important)

`fired.json` is **today-only**. Before the next session overwrites it:

```bash
cp .nse_alert/fired.json .nse_alert/fired-$(date +%F).json
# or after EOD:
uv run nse-alert report   # also writes report-YYYY-MM-DD.txt
```

`research analyze` reads `fired.json` and `fired-YYYY-MM-DD.json`.

## How reconstruction works

1. Load universe (same filters / `CUSTOM_UNIVERSE_FILE` as watch).  
2. Pull **daily** history for prev-close; pull **5-minute** bars (chunked ≤100 calendar days per Kite call).  
3. Replay the same once-per-day threshold logic as `AlertEngine` (FO-only ±4 respected).  
4. UP entries use the **alert level price** (prev_close × (1+thr%)), not the wick high — avoids optimistic fills.  
5. Simulate each strategy; write the comparison table.

## Practical tips

1. First run: `--max-symbols 15 --days 20` and confirm the table looks sane.  
2. Prefer `CUSTOM_UNIVERSE_FILE=universes/liquid_sample.txt` (or your liquid list) over whole-market until the pipeline is trusted.  
3. Bars cache under `.nse_alert/research/intraday/` — re-runs are much faster.  
4. Two live days → use `analyze` only; trust **backtest** for strategy selection.  
5. IPO / multi-day hold is **not** in v1 — keep that as a separate CNC study (your “stay put” insight).

## Related code

| Module | Role |
|--------|------|
| `research/analyze.py` | Local `fired*.json` summary |
| `research/bars.py` | Kite historical chunking + cache |
| `research/synth.py` | Alert reconstruction from bars |
| `research/strategies.py` | Strategy catalog + MIS/hold sims |
| `research/runner.py` | End-to-end backtest + report text |
| `cli.py` → `research` | Click entrypoints |
