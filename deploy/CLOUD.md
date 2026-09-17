# Cloud deploy (AWS / any VPS) + API orders

> **Docs map:** [../docs/README.md](../docs/README.md) · costs: [COST.md](COST.md) · alerts: [../docs/ALERTS.md](../docs/ALERTS.md) · login: [../docs/LOGIN.md](../docs/LOGIN.md)

This app can run 24×5 on a small cloud VM so you don’t keep a laptop terminal open.
For **API order placement**, SEBI/NSE rules (from **1 Apr 2026** at Zerodha) require a
**whitelisted static public IP**.

**Alerts-only** (no orders) do not need the whitelist. **F&O / ASM alert features** do not change cloud networking or daily login — same token + same static IP rules as before.

## Recommended shape

```text
AWS ap-south-1 (Mumbai)  t3.micro / t3.small
  + Elastic IP  (this is your static IP)
  + Docker Compose running `nse-alert watch`
  + Security group: outbound all; inbound SSH (your IP) only
       (open 8765 temporarily only when running `nse-alert login`)
```

You do **not** need a big AWS footprint. A ₹500–1500/mo VPS with a fixed IP also works
(DigitalOcean, Hetzner, Lightsail, etc.).

## One-time Oracle checklist (Always Free)

1. Console → **Compute → Instances → Create instance**.
2. **Name:** `nse-alert` (anything).
3. **Image:** Canonical Ubuntu **22.04 or 24.04** (ARM / aarch64).
4. **Shape:** Change shape → **Ampere** → `VM.Standard.A1.Flex` → **1 OCPU**, **6 GB** RAM.
5. **Networking:** use the default VCN/subnet; tick **Assign a public IPv4 address**.
6. **SSH keys:** paste your public key (or download Oracle’s generated key and keep it safe).
7. Create. If it says **out of capacity**, retry later / other AD / Hyderabad — or switch to Lightsail/DO (see `COST.md`).
8. After Running: open the instance → copy **Public IP**.
9. **Security list / NSG** for that subnet: allow **SSH (22)** from *your home IP only* (not `0.0.0.0/0` if you can avoid it). Leave **8765** closed until you run `login` on the server.
10. On [developers.kite.trade](https://developers.kite.trade) → **Profile → IP Whitelist**, add that public IP.
11. SSH in: `ssh -i /path/to/key ubuntu@PUBLIC_IP` then follow **Install on the VM** below.

Optional: make the IP sticky via **Networking → IP Management → Reserved public IPs** and attach it (so whitelist doesn’t break if you recreate the VM).

## One-time AWS Lightsail checklist (recommended)

1. Open [Lightsail](https://lightsail.aws.amazon.com/) → set region **Mumbai (ap-south-1)** (top-right).
2. **Create instance**:
   - Platform: **Linux/Unix**
   - Blueprint: **OS Only → Ubuntu 24.04** (or 22.04)
   - Plan: **$5** or **$7** (1 GB RAM)
   - Name: `nse-alert`
3. Wait until status is **Running**.
4. Instance → **Networking** → **Create static IP** → attach to `nse-alert` → note the IP.
5. **Networking → IPv4 firewall**: keep **SSH (22)**; optionally restrict to your home IP. Do **not** open 8765 yet.
6. Connect: use Lightsail **browser SSH**, or download the default key / use your own key:
   `ssh -i ~/.ssh/lightsail.pem ubuntu@STATIC_IP`
7. On [developers.kite.trade](https://developers.kite.trade) → **Profile → IP Whitelist**, add the **static IP**.
8. On the VM, follow **Install on the VM** below.

## One-time AWS EC2 checklist (optional; Lightsail is simpler)

1. Launch Ubuntu 22.04+ in **Mumbai**.
2. Allocate an **Elastic IP** and associate it to the instance.
3. Note the public IP, e.g. `13.232.x.x`.
4. On [developers.kite.trade](https://developers.kite.trade) → **Profile → IP Whitelist**,
   add that Elastic IP (up to 2 IPs; 1 change/week).
5. Kite app **Redirect URL** for login from the server:
   `http://13.232.x.x:8765/callback`
   (or keep login local and only `set-token` on the server — see below).

## AWS console login (root vs IAM)

Use an **IAM user** (or IAM Identity Center) for daily Lightsail/EC2 work. Do not use the **root** user except for billing, account recovery, and creating that first IAM user.

Root working only in **incognito** is usually the **browser**, not AWS randomly locking you: leftover `aws.amazon.com` cookies, a password-manager fill, or an extension. Clearing site cookies for Amazon/AWS in the normal profile often fixes it. It is common enough to be annoying; it is not a reason to keep using root.

Create:

1. IAM → **Users** → Create user (e.g. `nse-alert-ops`).
2. Attach a tight policy (Lightsail **or** EC2 + Elastic IP + the instance SG — not `AdministratorAccess`).
3. Enable **MFA** on that user **and** on root.
4. Sign in at the **account IAM sign-in URL** (Account ID + IAM user), not the root email form.

Keep root in a password manager + MFA; use it rarely.

## Install on the VM

```bash
sudo apt update && sudo apt install -y git docker.io docker-compose-v2
sudo usermod -aG docker $USER   # re-login after this

git clone https://github.com/diniiidinesh/my_stocks.git /opt/nse-alert
cd /opt/nse-alert
cp .env.example .env
nano .env   # fill Kite + Telegram + TRADE_* 
```

### `.env` for cloud (example)

```env
FEED_MODE=kite
THRESHOLD_PCT=4,7,11,13
FO_ONLY_THRESHOLDS=4
ASM_ENABLED=true
CUSTOM_UNIVERSE_FILE=universes/liquid_sample.txt

KITE_API_KEY=...
KITE_API_SECRET=...
KITE_ACCESS_TOKEN=   # filled daily via login / set-token

TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...

# Orders — start with dry_run, then confirm, only then auto
TRADE_MODE=dry_run
TRADE_SIZING=margin
TRADE_MARGIN_INR=10000
TRADE_FALLBACK_LEVERAGE=5
TRADE_QTY=1
TRADE_PRODUCT=MIS
TRADE_STOP_WAIT_SEC=20
TRADE_TRAIL_BREAKEVEN=true
TRADE_TRAIL_BREAKEVEN_PCT=2
TRADE_ORDER_TYPE=MARKET
TRADE_MARKET_PROTECTION=2
TRADE_MAX_ORDERS_PER_DAY=10
TRADE_ON_THRESHOLDS=13
TRADE_SIDES=up
TRADE_STOP_LOSS_PCT=2

# Optional EOD screener (see docs/SCREENER.md)
SCREEN_MIN_MARKET_CAP_CR=5000
SCREEN_MIN_TURNOVER_CR=10
SCREEN_REQUIRE_DELIVERY=true
SCREEN_MIN_DELIVERY_PCT=40
SCREEN_AFTER_HHMM=1540
```

Full env reference: [../.env.example](../.env.example). Alert semantics: [../docs/ALERTS.md](../docs/ALERTS.md). Orders: [../docs/ORDERS.md](../docs/ORDERS.md).

## Daily login (access token)

Kite tokens expire every trading day. Pick one:

**A. Login on the server (opens port 8765 briefly)**

```bash
# SG: allow inbound TCP 8765 from your home IP only
docker compose run --rm --service-ports nse-alert nse-alert login --no-browser
# Open http://ELASTIC_IP:8765/ in your laptop browser
```

**B. Login on laptop, paste token onto server**

```bash
# laptop
uv run nse-alert login
# copy access token

# server
docker compose run --rm nse-alert nse-alert set-token PASTE_TOKEN_HERE
```

Note: **order** API calls must originate from the **whitelisted static IP**.
Market-data WebSocket can come from anywhere, but if watch+orders run together,
run them on the cloud box.

### IPv6 vs Elastic IPv4 (Kite “IP is not allowed”)

AWS dual-stack instances often send `place_order` over **IPv6** even when you
whitelisted the Elastic **IPv4**. The error shows the address Kite actually saw,
e.g. `2406:da1a:…`. IPv4 and IPv6 are not interchangeable. You may change the
Kite whitelist **once per calendar week**.

Preferred fix (this repo default): `KITE_FORCE_IPV4=true` so orders use the
Elastic IPv4. Then whitelist that IPv4 only.

```bash
uv run nse-alert public-ip
# IPv4 egress must match Kite Profile → IP Whitelist
```

Alternatively add the IPv6 from the error as the **secondary** IP (max 2).
Do not burn the weekly change if IPv4-forcing will do.

### Same STATE_DIR for watch and confirm

`watch` writes pending ids to `STATE_DIR/orders.json`. `nse-alert confirm` /
`pending` must open **that same file**. Mixing Docker (old named volume) with
`uv run` on the host produced `No pending order …`. Compose now bind-mounts
`./.nse_alert`. Always:

```bash
cd /opt/nse-alert
uv run nse-alert pending          # prints the file path
uv run nse-alert confirm ABC123
```

If watch is Docker-only: `docker compose exec nse-alert nse-alert pending`.

## Run the watcher

```bash
cd /opt/nse-alert
docker compose up -d --build
docker compose logs -f
```

Stop:

```bash
docker compose down
```

## Orders — modes

| `TRADE_MODE` | Behavior |
|--------------|----------|
| `off` | Alerts only (default / safest) |
| `dry_run` | Pretend to place; logs + Telegram, no real order |
| `confirm` | Alert → Telegram `CONFIRM abc123` → then places |
| `auto` | Places immediately when `TRADE_ON_THRESHOLDS` is crossed |

Manual order from the server:

```bash
docker compose run --rm nse-alert nse-alert order buy RELIANCE --qty 1 --dry-run
docker compose run --rm nse-alert nse-alert order buy RELIANCE --qty 1 --live
```

Confirm a pending id:

```bash
docker compose run --rm nse-alert nse-alert pending
docker compose run --rm nse-alert nse-alert confirm ABC123
# in Telegram (slash command, especially in groups): /confirm ABC123
docker compose run --rm nse-alert nse-alert telegram-chats
```

## EOD TA screener (Excel + Telegram)

Separate from the intraday watcher. After market close (≥ 15:40 IST):

```bash
cd /opt/nse-alert
uv run nse-alert screen
# smoke: uv run nse-alert screen --force --max-symbols 30 --no-telegram
```

Also useful after the session:

```bash
uv run nse-alert report --telegram   # close % + hold-level counts from today's alerts
```

Cron example — **the Lightsail box's clock is UTC, not IST**; the times
below are UTC with the IST equivalent noted. Cron does not source
`.profile`, so `uv` is not on PATH by default — prepend a `PATH=` line
(as below) or use `uv`'s absolute path (`/home/ubuntu/.local/bin/uv`):

```cron
PATH=/home/ubuntu/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
5 10 * * 1-5   cd /opt/nse-alert && docker compose stop                                                              # 15:35 IST
10 10 * * 1-5  cd /opt/nse-alert && docker compose run --rm nse-alert nse-alert report --telegram >> /var/log/nse-report.log 2>&1   # 15:40 IST
0 15 * * 1-5   cd /opt/nse-alert && uv run nse-alert screen >> /var/log/nse-screen.log 2>&1                          # 20:30 IST
```

See [docs/SCREENER.md](../docs/SCREENER.md) for filters, delivery %, and ranking.

## SEBI / Zerodha reminders

- Static IP whitelist required for **order** endpoints from **1 Apr 2026**.
- WebSocket quotes / alerts alone do **not** need the static IP.
- Stay under ~10 orders/sec (this app is far below that).
- Market orders need non-zero **market protection** (`TRADE_MARKET_PROTECTION`).
- This is not investment advice; start with `dry_run`, `TRADE_MARGIN_INR` you can afford (or `TRADE_SIZING=fixed` + tiny `TRADE_QTY`), and `confirm`.

## Alternative: systemd (no Docker)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
cd /opt/nse-alert && uv sync
sudo cp deploy/nse-alert.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now nse-alert
```
