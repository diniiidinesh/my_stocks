# Paperkite — browser paper trading

Paperkite uses **live Kite prices and simulated orders**, starting with ₹2 lakh cash,
long-only NSE equities, no leverage, intraday only. It has a watchlist search,
1/5/15-minute candles, draggable entry/stop/target planning lines, market/limit/stop
paper buys, editable stop and target levels on existing positions, a trade journal,
net daily P&L, itemised charges, a daily loss cap (off by default), and journal export.

The existing daily Kite login/token workflow is **unchanged**. Paperkite reads the
same `.env` and universe settings. Its startup validates the token. After the daily
login, recreate the paper container too so it receives the new token.

## Test locally without credentials

```sh
uv sync
uv run nse-alert paper --feed mock
```

Open `http://localhost:8787`. This is a separate mock ledger, labelled MOCK in the UI.
Mock candles accumulate while the process runs; it does not invent historical data.
Mock days and live-price paper days never share a streak or account.

## On the AWS Ubuntu VM

Use Docker Compose for production, as with the alert watcher. From the repository
folder after pulling the paper feature:

```sh
docker compose --profile paper up -d --build paper
docker compose --profile paper logs --tail 50 paper
```

This starts **only** the additional `paper` service. It does not start another
`watch`, poll Telegram, or change `TRADE_MODE`. The original alert watcher remains
independent. This service opens an additional Kite market-data socket; it needs
available capacity within your app's connection allowance. Do not run the bare
paper command and the paper container simultaneously against the same ledger.

On your Mac, in a separate terminal, using your usual SSH host/alias:

```sh
ssh -N -L 8787:127.0.0.1:8787 YOUR_VM_ALIAS
```

Then open `http://localhost:8787` in your browser. Use local port **8787** as shown:
the HTTP Host check intentionally permits only localhost/127.0.0.1 on that port.
The AWS security group does not need any new inbound rule. Compose publishes the
service to the VM's loopback interface only. The standalone command also defaults
to loopback; `--host 0.0.0.0` is provided for the container's internal binding only.
Never publish that host-bound port to the public internet: the SSH tunnel is the
authentication layer. State-changing requests also require a same-origin Origin
and an ephemeral CSRF token. Credentials are never returned by the paper API.

Daily login, then:

```sh
docker compose --profile paper up -d --force-recreate paper
```

Keep your existing watcher refresh procedure too. A stale token never falls back
to mock data. Check the mode badge, quote timestamps, and any feed warning.

## Trading and charts

Select a stock, click **Entry**, **Stop**, or **Target**, then click/drag on the
chart. **Set 3% stop** calculates 3% below the planned entry. Market orders use the
server's current quote, not the browser's entered price. Limit buys wait for a
quote at or below the limit; stop buys wait for a quote at or above the trigger.
Every fill checks cash, the session gate, and the stop/target bounds.

Drawings persist per stock. They plan a new order; they do not change existing
positions until you choose **Edit levels** and **Update paper position levels**.
Protection can be trailed above entry to protect profit, provided the stop remains
below the current quote. The entry fill and quantity remain unchanged.

Historical charts request five calendar days of Kite minute candles, cached for
60 seconds per symbol, merged with live candles. The UI refreshes prices every
two seconds and charts every ten seconds. Changing timeframe aggregates by clock
buckets. Historical request failures are shown, not replaced with synthetic data.
Only symbols in the configured cash-equity universe are available.

## Execution and data limits

- Quotes expire **15 seconds after receipt**. Entries, manual exits and protection
  edits require a fresh quote. These are receipt timestamps, not trade timestamps.
- The default simulated slippage is **5 basis points per fill**, configurable.
  Limit fills respect the limit. Gaps through stops fill at the next observed quote
  with slippage, never retroactively at the stop price.
- Liquidity, spreads, depth, queues, partial fills, and locked circuits are not
  modelled. A paper fill is not evidence that a real order would have filled.
- Intraday entries are allowed on weekdays from 09:15 IST until
  `SQUAREOFF_HHMM` (existing setting, default **1505**). The paper service uses
  this earlier cutoff for every stock. It does not independently load exchange
  holidays or special sessions; it additionally requires a fresh Kite quote.
- At cutoff, pending orders are cancelled and open positions are squared off on
  fresh quotes. A stale position is marked **exit waiting for fresh quote**; the
  service never invents a fill. It must remain running to execute protections.
- If a VM outage leaves positions open overnight, the next fresh quote exits
  them and both affected days are ineligible for the streak. Entries on the next
  day are blocked until overdue positions resolve. A restored position's symbol
  must remain in the universe; startup fails with the missing symbols otherwise.
- Pending orders expire at the end of the date, including after a restart.
- A breached daily cap triggers square-off and latches the account against new
  entries for that day, even if the cap is later disabled. It cannot guarantee a
  maximum loss: gaps, stale feeds and costs can exceed it.

## P&L, costs and the seven-day streak

Open-session net P&L includes realized gross P&L, marked-to-market open P&L with
simulated exit slippage, paid entry/exit charges, and estimated remaining exit
charges. The ledger separates gross P&L and paid costs. Completed-day results are
realized and include all estimated charges. No-trade days pause the streak. A zero
or negative net traded day resets it. Days with unresolved/overnight positions do
not qualify. A live-price day finalizes after cutoff once positions are closed;
manually squaring off does not create extra sessions on the same date.

The seven-day counter is a personal practice milestone, not a real-order unlock.
There is no real-trading path in Paperkite.

Rates checked against [Zerodha's schedule](https://zerodha.com/charges/) on
**19 October 2026**: brokerage min(₹20, 0.03% of turnover) per filled order;
NSE exchange 0.00307%; SEBI ₹10/crore; STT 0.025% on sells; stamp 0.003% on buys;
IPFT ₹0.01/crore; GST 18% on applicable fees. Each component is rounded per order
to paise. Actual broker contract-note aggregation and statutory rounding may
produce differences. Special fees, income tax and subscription costs are excluded.
Rates are explicit code constants, not automatically updated. Past fills retain
their recorded charge breakdown.

## Persistence, backup and review

Live paper state: `STATE_DIR/paper-live/ledger.sqlite3`; mock state:
`STATE_DIR/paper-mock/ledger.sqlite3`. Both use a separate single-instance lock and
never write the real `orders.json`, alert dedupe files, or Telegram state. The
SQLite account updates atomically and the ledger file is created with mode 0600.
Use **Export journal** for a readable JSON backup. For a restorable database
backup, stop only the paper container before copying its ledger file. Starting
capital is fixed at ₹2 lakh in this initial VM version; there is intentionally no
browser button to delete your history.

Tests:

```sh
uv run pytest -q
```

Related code: `paper.py` (accounting), `paper_server.py` (HTTP/feed wiring),
`paper_web/` (browser), `tests/test_paper.py`, `tests/test_paper_server.py`.
