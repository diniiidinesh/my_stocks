# Fix plan — nse-alert (handoff)

> **Status as of 2026-09-17 23:10 IST.** P0–P4 are **done**.
> This file is a point-in-time handoff. The durable write-up — including the
> prevention controls that are still unbuilt — is
> [`docs/RCA-2026-09-17.md`](docs/RCA-2026-09-17.md).
>
> | Item | Status |
> |------|--------|
> | P0 — missing `Settings` import | **Done** — PR #17, merged |
> | P1 — container as `ubuntu` | **Done** — PR #18, merged; verified live |
> | P2 — cron PATH + UTC docs | **Done** — PR #18, merged |
> | P3 — VM drift | **Done** — VM pulled to `a78cc67`, image rebuilt |
> | P4 — bot token in logs | **Done** — `httpx` quieted to WARNING; VM log truncated + chmod 640 |
>
> `deploy/nse-alert.service` has been **deleted** and Docker is now documented
> as the only production path.
>
> Still unbuilt, tracked in the RCA's Actions section: single-instance lock,
> alert on repeated `getUpdates` failure, and order-expiry notification.

Context: the 20:30 IST screener never delivered. Root-caused on 2026-09-17.
Cron timing was always correct (`0 15` UTC = 20:30 IST); the command failed.
Two live issues were hotfixed directly on the VM and must now be made durable
in the repo. One P0 repo bug is still unfixed everywhere.

VM: AWS Lightsail, ap-south-1, static IP 3.108.18.140, user `ubuntu`,
app at `/opt/nse-alert`, box timezone is **UTC** (all cron is UTC).

---

## P0 — `main` is broken: missing `Settings` import

**Symptom:** every CLI command that builds settings dies with
`NameError: name 'Settings' is not defined` (`screen`, `watch`, `report`,
`size`, `order`, ...). Only `--help` survives.

**Cause:** commit `b2c6e9f` ("Fix Kite IPv6 order rejects and missing pending
ids") replaced the import instead of adding to it:

```diff
-from nse_alert.config import Settings
+from nse_alert.ipv4 import force_ipv4
```

`src/nse_alert/cli.py` uses `Settings` 17 times; nothing imports it.

**Affected:** `origin/main` and every `cursor/*` branch except
`cursor/alert-research-backtest-f163` (which coincidentally re-added it).

**Fix:**
1. Branch off `main` (e.g. `fix/cli-settings-import`).
2. In `src/nse_alert/cli.py`, restore alongside the existing imports:
   `from nse_alert.config import Settings`
   Keep `from nse_alert.ipv4 import force_ipv4`. Keep import ordering
   consistent with the file's existing style.
3. Verify locally — this must exit 0:
   `uv run nse-alert screen --force --max-symbols 5 --no-telegram`
4. Open a PR against `main` and merge. This is urgent: a fresh clone or a
   VM rebuild from `main` is completely non-functional.

**Regression guard (same PR):** add a smoke test that imports
`nse_alert.cli` and invokes each command with `--help` via click's
`CliRunner`, plus at least one test that actually constructs `Settings()`
through a command path. The existing suite did not catch a repo-wide
`NameError`, which is the real gap here.

---

## P1 — Docker-as-root vs cron-as-ubuntu fight over `.nse_alert/`

**Symptom:** `PermissionError: [Errno 13] Permission denied:
'.nse_alert/screener/screen-<date>.xlsx'` — the screener completes the entire
scan, then fails on the final Excel write.

**Cause:** `Dockerfile` sets no `USER`, so the container runs as root and
writes into the bind-mounted `./.nse_alert` (see `docker-compose.yml`).
Any manual `docker compose run` leaves root-owned files that the 20:30 cron —
running as `ubuntu` — cannot overwrite.

**Already done on the VM (temporary):** `sudo chown -R ubuntu:ubuntu .nse_alert`
This unblocks tonight but WILL recur on the next Docker run.

**Durable fix:** make the container write as `ubuntu` so host and container
interoperate, which is the stated intent of the bind-mount comment in
`docker-compose.yml`.

In `docker-compose.yml`, under the `nse-alert` service:

```yaml
    user: "1000:1000"
```

Confirm `ubuntu` is uid/gid 1000 on the VM (`id ubuntu`) before hardcoding;
prefer `user: "${UID:-1000}:${GID:-1000}"` if you want it portable.

Then on the VM:

```bash
cd /opt/nse-alert
sudo chown -R ubuntu:ubuntu .nse_alert
docker compose up -d --force-recreate
```

Do this while the market is closed. Verify afterwards that newly written
files in `.nse_alert/` are `ubuntu`-owned, not root.

**Consider also:** the crontab mixes two runtimes — `docker compose run` for
`report`, host `uv run` for `screen`. Picking one runtime for all three lines
would remove this whole class of bug. Docker for everything is the more
consistent choice since the watcher already runs that way.

---

## P2 — Codify the cron PATH fix

**Symptom:** `/bin/sh: 1: uv: not found` in `/var/log/nse-screen.log`.

**Cause:** cron does not source `.profile`, so `uv` at
`/home/ubuntu/.local/bin/uv` was not on PATH.

**Already done on the VM:** a `PATH=` line was prepended to the crontab.
Backup at `~/crontab.backup.20260917-160936`.

Current active crontab (all times UTC; box is UTC):

```cron
PATH=/home/ubuntu/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
5 10 * * 1-5   cd /opt/nse-alert && docker compose stop                       # 15:35 IST
10 10 * * 1-5  cd /opt/nse-alert && docker compose run --rm nse-alert nse-alert report --telegram >> /var/log/nse-report.log 2>&1   # 15:40 IST
0 15 * * 1-5   cd /opt/nse-alert && uv run nse-alert screen >> /var/log/nse-screen.log 2>&1   # 20:30 IST
```

**Fix:** this currently lives only in the VM's crontab and would be lost on a
rebuild. Update `deploy/CLOUD.md` and `docs/SCREENER.md`:
- Replace the `15 16 * * 1-5` example, which is misleading — it reads as
  16:15 IST but the box is UTC, so it actually fires at 21:45 IST.
- State explicitly that Lightsail is UTC and show IST alongside each UTC entry.
- Include the `PATH=` line in the documented crontab, or use an absolute
  `/home/ubuntu/.local/bin/uv` in the command.

---

## P3 — Reconcile VM drift

`/opt/nse-alert` is at `9389b13` with an uncommitted edit to
`src/nse_alert/cli.py` — that edit is exactly the P0 fix. Once P0 is merged:

```bash
cd /opt/nse-alert
git stash            # or: git checkout -- src/nse_alert/cli.py
git pull
docker compose build && docker compose up -d
```

The running image predates the P0 fix, so rebuild rather than just restarting.
Check for any other uncommitted VM-side edits at the same time
(`git status --short`) — there may be more stranded hotfixes.

---

## Verification

After P0 + P1, run as cron would, with Telegram suppressed:

```bash
ssh -i ~/.ssh/LightsailDefaultKey-ap-south-1.pem ubuntu@3.108.18.140
env -i HOME=/home/ubuntu PATH=/home/ubuntu/.local/bin:/usr/bin:/bin \
  /bin/sh -c "cd /opt/nse-alert && uv run nse-alert screen --force --max-symbols 8 --no-telegram"
```

Must exit 0 and write `.nse_alert/screener/screen-<date>.xlsx` owned by `ubuntu`.

Next scheduled run: weekdays 20:30 IST. Confirm with
`grep CRON /var/log/syslog | grep nse-alert` and `tail /var/log/nse-screen.log`.

Note `/var/log/nse-report.log` does not exist yet — the 15:35/15:40 lines were
added at 17:42 IST on 2026-09-17, after they had already passed that day. They
fire for the first time on 2026-09-18. Not a bug, but worth confirming.

---

## P4 — Telegram bot token leaking into logs

**Symptom:** `/var/log/nse-screen.log` contains the full bot token in
plaintext, because `httpx` logs request URLs at INFO and the Telegram API
carries the token in the URL path:

```
INFO httpx: HTTP Request: POST https://api.telegram.org/bot<TOKEN>/sendMessage "HTTP/1.1 200 OK"
```

The log is world-readable (`-rw-r--r--`). Anyone with read access to the box
can take over the bot: post to the alert group, or read it.

**Fix (repo):** quiet the httpx request logger where logging is configured
(`src/nse_alert/cli.py`, near `logging.basicConfig`):

```python
logging.getLogger("httpx").setLevel(logging.WARNING)
```

That removes the per-request URL lines entirely. If those lines are wanted for
debugging, instead add a logging filter that redacts `/bot<token>/` — but
raising the level is simpler and loses little.

**Fix (VM), after the code change is deployed:**

```bash
sudo truncate -s 0 /var/log/nse-screen.log
sudo chmod 640 /var/log/nse-screen.log
```

Consider `logrotate` for both `nse-screen.log` and `nse-report.log` while
here; neither is currently rotated.

**Rotate the token if the VM was ever shared or snapshotted publicly.**
Regenerate via @BotFather and update `.env` on the VM. Not required if the box
has only ever been yours.
