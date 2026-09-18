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

### Add swap (required on 1 GB boxes)

The recommended Lightsail plans ship **1 GB RAM and zero swap**. That is enough
to run the watcher, but not enough to also build a Docker image or run the EOD
screener, which loads ~490 symbols × 400 days into pandas. With no swap the
kernel's only option under pressure is to kill something — on 2026-09-15 it
killed `docker-compose` three times and `networkd-dispatcher` once.

Do this once, before the first `docker compose build`:

```bash
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile

# persist across reboots — skipping this is the classic mistake
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab

# prefer RAM; use swap only under real pressure
echo 'vm.swappiness=10' | sudo tee /etc/sysctl.d/99-swappiness.conf
sudo sysctl -q vm.swappiness=10
```

Verify — `Swap:` must be non-zero, and the `fstab` line must be present:

```bash
free -m
swapon --show
grep swapfile /etc/fstab
```

Swap is **insurance, not capacity**: it lets short spikes page out cold memory
instead of triggering the OOM killer. It costs nothing — the disk is already
part of the instance plan.

> A `/swapfile` that exists but shows `Swap: 0` was created without the
> `/etc/fstab` line and disappeared at the last reboot. Re-run `swapon` and add
> the line.

### Log rotation

`/var/log/nse-screen.log` and `/var/log/nse-report.log` (see the cron entries
below) grow forever otherwise, and — until the `httpx` logging fix in
`src/nse_alert/cli.py` — used to contain the Telegram bot token in plaintext
(see [../docs/RCA-2026-09-17.md](../docs/RCA-2026-09-17.md) P4). Install once:

```bash
sudo cp deploy/nse-alert.logrotate /etc/logrotate.d/nse-alert
sudo chmod 644 /etc/logrotate.d/nse-alert
sudo logrotate -d /etc/logrotate.d/nse-alert   # dry run, checks syntax
```

If either log file already exists with looser permissions than `640`, or a
token may have been logged to it before the `httpx` fix was deployed, purge it:

```bash
sudo truncate -s 0 /var/log/nse-screen.log /var/log/nse-report.log
sudo chmod 640 /var/log/nse-screen.log /var/log/nse-report.log
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

## Daily runbook (trading day)

Run this on the VM before the 09:15 IST open. The whole thing is one SSH
session; close the tab when done — the container keeps running detached.

```bash
cd /opt/nse-alert

git pull origin main          # optional — only when you want updates

# 1. refresh the daily Kite token (expires every morning)
docker compose run --rm nse-alert nse-alert set-token <ACCESS_TOKEN>

# 2. (re)start the watcher — idempotent, never creates a second one
#    --build is required whenever you pulled code above; without it the
#    container is recreated from the OLD image and your pull does nothing
docker compose up -d --build --force-recreate

# 3. verify before you walk away
docker compose ps
docker compose logs --tail 30
```

The startup banner is the check that matters. Confirm `trade=` is the mode you
intend and `feed=kite`:

```
Watching 518 symbols | thresholds=±4,7,11,13% | … | feed=kite | telegram=yes | trade=auto | …
```

Then confirm **exactly one watcher** is running — this must print one line:

```bash
ps -eo cmd --no-headers | grep "[.]venv/bin/python .*nse-alert watch"
```

Two lines means something else is also running (a systemd unit, a stray
`uv run`). Stop it before trading: a second watcher silently breaks order
confirmation. See [../docs/ORDERS.md](../docs/ORDERS.md) and
[../docs/RCA-2026-09-17.md](../docs/RCA-2026-09-17.md).

### Gotchas

- **`docker compose up -d --build --force-recreate` is idempotent.** Running it
  twice replaces the container; it does not create a second watcher. Re-run it
  freely.
- **`--force-recreate` alone does not rebuild the image.** After a `git pull`
  you must pass `--build`, or the new container starts from the old image and
  silently runs the code you just replaced. Confirm what is actually running:

  ```bash
  docker compose exec nse-alert git -C /app rev-parse --short HEAD 2>/dev/null \
    || docker inspect -f '{{.Created}}' nse-alert   # image build time
  ```
- **Changing `.env` does nothing until you recreate.** A running watcher never
  re-reads `.env`, so a `TRADE_MODE` edit needs step 2 again. The banner in step
  3 is how you confirm the change actually took.
- **`Conflict. The container name … is already in use`** during a recreate is
  Docker cleaning up a renamed intermediate. Check `docker compose ps` before
  assuming it failed — it usually succeeded.
- **The 15:35 IST cron runs `docker compose stop`.** The watcher stays down
  until your next morning run. That is intentional.

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

## Deployment mechanism: Docker only

**Docker Compose is the only supported way to run the long-running watcher.**

There is deliberately no systemd unit in this repo. `deploy/nse-alert.service`
was removed on 2026-09-17 because having two installable supervisors is what
caused that day's outage: systemd and Docker each ran a watcher, both held a
long-poll on the same Telegram bot token, and **Telegram allows exactly one
`getUpdates` consumer per token**. The second poller does not crash — it
silently `409`s forever, which killed order confirmation for hours with no
alert. See [../docs/RCA-2026-09-17.md](../docs/RCA-2026-09-17.md) (Incident A).

If you are upgrading a host that still has the old unit installed, remove it:

```bash
sudo systemctl disable --now nse-alert
sudo rm -f /etc/systemd/system/nse-alert.service
sudo systemctl daemon-reload
```

**Verify exactly one watcher is running — do this every trading day:**

```bash
ps -eo cmd --no-headers | grep "[.]venv/bin/python .*nse-alert watch"
```

Exactly one line. Note that `pgrep -f "nse-alert watch"` is unreliable here —
it matches its own command line and over-counts.

> Running on a host without Docker is not supported. `uv run nse-alert watch`
> is for local testing only: it is not supervised, does not restart, and will
> conflict with the container if both are up.

Also note: the watcher does not reload `.env` on change. After editing
`TRADE_MODE` or any other setting, recreate the container
(`docker compose up -d --force-recreate`) — otherwise the process keeps running
on stale config indefinitely. Confirm the change took effect by checking the
`trade=` value in the startup banner:

```bash
docker compose logs nse-alert | grep -m1 "Watching"
```
