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

Use an **IAM user** for daily Lightsail/EC2 work. Do **not** use the **root** user except for billing, account recovery, and creating that first IAM user.

Root working only in **incognito** is usually the **browser**, not AWS randomly locking you: leftover `aws.amazon.com` cookies, a password-manager fill, or an extension. Clearing site cookies for Amazon/AWS in the normal profile often fixes it. It is common enough to be annoying; it is not a reason to keep using root.

You cannot create the IAM user from this repo. Do it once in the AWS console while root still works (incognito is fine). After that, bookmark the **IAM** sign-in URL and stop opening the root form.

AWS does **not** ship a managed policy named `LightsailFullAccess`. Searching “Lightsail” on the user-creation **Permissions** step finds nothing. You create that policy yourself (or paste it as an **inline** policy on the user). Without it, `nse-alert-ops` can sign in but Lightsail looks empty — the instance is still there; the user is not allowed to list it.

### If `nse-alert-ops` already exists (attach Lightsail now)

Sign in as **root** (incognito). Then use **either** path A (no JSON) or path B (paste JSON). Both are done in **IAM**, not in the Lightsail console.

**A — visual editor (easiest)**

1. Open [IAM → Policies](https://console.aws.amazon.com/iam/home#/policies) → **Create policy**.
2. Stay on **Visual**.
3. **Select a service** → type `Lightsail` → choose **Lightsail**.
4. **Actions allowed** → tick **All Lightsail actions** (the Lightsail console needs full Lightsail access; a smaller set shows a blank instance list).
5. **Resources** → **All**.
6. **Next** → Policy name `LightsailFullAccessPolicy` → **Create policy**.
7. Open [IAM → Users](https://console.aws.amazon.com/iam/home#/users) → **`nse-alert-ops`** → **Permissions** tab.
8. **Add permissions** → **Add permissions** → **Attach policies directly**.
9. Search `LightsailFullAccessPolicy` → tick it → **Next** → **Add permissions**.
10. Confirm the **Permissions** tab now lists `LightsailFullAccessPolicy`.

**B — inline JSON on the user (one screen)**

1. [IAM → Users](https://console.aws.amazon.com/iam/home#/users) → **`nse-alert-ops`** → **Permissions**.
2. **Add permissions** dropdown (right side) → **Create inline policy** (not “Attach policies directly”).
3. Choose **JSON**. Delete the sample and paste:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "lightsail:*",
      "Resource": "*"
    }
  ]
}
```

4. **Next** → name `LightsailFullAccess` → **Create policy**.

Sign out of root. Sign in again as `nse-alert-ops`. Open [Lightsail instances](https://lightsail.aws.amazon.com/ls/webapp/home/instances) and set the region (top-right) to **Mumbai (ap-south-1)**. Lightsail is regional: Virginia / Singapore will look empty even with the policy. The AWS console **home** page also does not list Lightsail VMs — use that Lightsail URL, not EC2.

If it is still empty: on the IAM user **Permissions** tab (as root) there should be at least one policy containing `lightsail:*`. If the only attached policy is `IAMUserChangePassword` or nothing, the attach did not stick — repeat A or B.

Full paste files (Lightsail + optional IAM self-manage, or EC2): [iam/nse-alert-ops-lightsail.json](iam/nse-alert-ops-lightsail.json) · [iam/nse-alert-ops-ec2.json](iam/nse-alert-ops-ec2.json). Do **not** attach `AdministratorAccess`.

### Create the IAM user (click path)

Skip this if `nse-alert-ops` already exists; use the attach steps above.

Do this as **root**, in **incognito**, on [https://console.aws.amazon.com/](https://console.aws.amazon.com/). Sign in with the **root email**, not “IAM user”.

1. Top-right → copy the **Account ID** (12 digits). You need it to sign in later.
2. Optional but nicer: **IAM → Dashboard → Create account alias** (e.g. `nse-alert`). Then the sign-in URL is `https://nse-alert.signin.aws.amazon.com/console` instead of the numeric account id.
3. **Create the Lightsail policy first** (path A or B above) so it exists before the user wizard. There is nothing useful to search for on the wizard’s policy list until you do this.
4. **IAM → Users → Create user**.
   - User name: `nse-alert-ops`
   - Tick **Provide user access to the AWS Management Console**
   - **I want to create an IAM user** (not Identity Center, unless you already use it)
   - Autogenerate a password **or** set one; tick **Users must create a new password at next sign-in**
   - Do **not** create access keys. Console-only is enough for Lightsail/EC2. Access keys are for CLI/API and are a common leak.
5. **Permissions** → **Attach policies directly** → search `LightsailFullAccessPolicy` → tick it. Do **not** attach `AdministratorAccess`.
6. Create the user. Save the **console sign-in URL**, username, and one-time password in your password manager.
7. Still as root: open `nse-alert-ops` → **Security credentials** → **Assign MFA device** → **Authenticator app** (Google Authenticator / Authy). Scan the QR, enter two successive codes. Also enable **MFA on the root user** (IAM dashboard → **Add MFA** under root) if it is not already on.
8. **Sign out** of root. Do not leave root logged in on a daily browser profile.

### Sign in as the IAM user (daily)

Use this URL, **not** the root email form:

```text
https://ACCOUNT_ID.signin.aws.amazon.com/console
```

(or `https://YOUR-ALIAS.signin.aws.amazon.com/console` if you set an alias)

- **Account ID** (or alias) + **IAM user name** `nse-alert-ops` + password + MFA
- Then open [Lightsail instances](https://lightsail.aws.amazon.com/ls/webapp/home/instances), region **Mumbai (ap-south-1)** — browser SSH, static IP, firewall, start/stop. Do not use the EC2 instance list; Lightsail VMs are not shown there.

If the page still asks for an **email**, you are on the root form. Switch via “Sign in using a different account” / “IAM user”.

Keep root in the password manager + MFA. Use it only for: billing/payment method, closing the account, or recovering this IAM user.

### After first IAM login

1. Change the one-time password when prompted.
2. Confirm [Lightsail → Instances](https://lightsail.aws.amazon.com/ls/webapp/home/instances) in **Mumbai** still shows `nse-alert` (same account; IAM does not create a second AWS account). If the list is empty, the Lightsail policy is not attached yet — go back to **If nse-alert-ops already exists**.
3. Bookmark the IAM sign-in URL. Forget the root bookmark for daily use.

If you later need AWS CLI on a laptop: create **one** access key on `nse-alert-ops`, store it in `~/.aws/credentials`, never in git or `.env`. The Lightsail/EC2 policies above already cover that CLI. Delete the key if you stop using CLI.

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

Cron example (weekdays 16:15 IST — adjust TZ on the box):

```cron
15 16 * * 1-5  cd /opt/nse-alert && uv run nse-alert screen >> /var/log/nse-screen.log 2>&1
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
