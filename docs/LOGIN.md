# Kite login and access tokens

Kite **access tokens expire every trading day**. API key + secret stay in `.env`; only the token is refreshed.

**Features like F&O filtering and ASM tagging do not change this flow.**

## One-time app setup

1. Create a **paid** Kite Connect app (~₹500/month) — free Personal apps cannot stream market data WebSockets.
2. In `.env`:

```env
KITE_API_KEY=...
KITE_API_SECRET=...
```

3. App **Redirect URL** (local login UI):

```text
http://127.0.0.1:8765/callback
```

For cloud login on a VM, either:

- keep using **laptop login + `set-token` on the server**, or  
- temporarily open port **8765** and set Redirect URL to `http://STATIC_IP:8765/callback`.

## Daily login (recommended: local UI)

```bash
uv run nse-alert login
```

Opens a small browser page:

- **Login with Kite** — completes OAuth and writes `KITE_ACCESS_TOKEN` to `.env`, or  
- **Paste token** — if you already have one.

On success, the page shows the **access token with a Copy button** (handy for pasting onto a cloud VM via `set-token`).

Without opening a browser on the machine:

```bash
uv run nse-alert login --no-browser
# then open http://127.0.0.1:8765/ on a machine that can reach it
```

## Paste-only (no UI)

```bash
uv run nse-alert set-token YOUR_ACCESS_TOKEN
```

Useful when `watch` runs on a cloud VM: login on your laptop, paste token onto the server.

## Cloud pattern

```text
Laptop:  nse-alert login          → copy access token
Server:  nse-alert set-token …    → restart / rely on .env for next watch
```

Order API calls must originate from a **whitelisted static IP**. Market-data WebSocket alone does not require whitelist — but if `watch` also places orders, run it on that IP. See [../deploy/CLOUD.md](../deploy/CLOUD.md).

## Troubleshooting

| Symptom | Likely fix |
|---------|------------|
| Redirect “connection refused” on 127.0.0.1 | Expected if the login UI isn’t running — start `nse-alert login` first, or copy `request_token` from the URL and use the UI paste / official exchange flow |
| WebSocket 403 | Paid Connect plan required; check API key |
| Empty universe / quote HTML errors | Use `CUSTOM_UNIVERSE_FILE=universes/liquid_sample.txt` |
| Orders rejected for IP | Add VM static IP under Kite Profile → IP Whitelist |

## Related code

- `login_ui.py` — local HTTP callback UI  
- `envfile.py` — safe `.env` token updates  
- `cli.py` — `login`, `set-token`, `login-hint`
