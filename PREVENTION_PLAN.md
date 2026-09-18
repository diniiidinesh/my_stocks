# Prevention plan — make failures loud (handoff)

Phase 2 of the [2026-09-17 RCA](docs/RCA-2026-09-17.md). Phase 1
([`FIX_PLAN.md`](FIX_PLAN.md), P0–P4) fixed the specific bugs. **This plan fixes
the reason nobody noticed them.**

> **Status.**
>
> | Item | Status |
> |------|--------|
> | P1 — single-instance lock | **Done** — PR #21, merged; verified live (second `watch` rejected while held, lock releases on process exit) |
> | P2 — alert on dead confirm listener | **Done** — `TelegramConfirmListener` alerts on the 3rd consecutive `getUpdates` failure, edge-triggered, with exponential backoff |
> | P3 — notify on order expiry | **Done** — `OrderBook.sweep_expired()` polled from `watch`, throttled to 30s, batches >3 expiries into one message |
> | P4 — validate Kite token at startup | **Done** — `watch` checks the token before any setup and exits+alerts on `TokenException`; mid-session sizing failures on a bad token now refuse+alert instead of silently falling back to an approximate estimate; `docker-compose.yml` restart policy changed to `on-failure:3` |
> | P5 — alert when the market feed drops | Open |
> | P6 — daily heartbeat | Open |

Every incident so far has the same shape: something stopped working, the app
logged it locally, and the operator found out hours later — or only because
they went looking. Four instances in two days:

| Failure | Logged | Alerted | Silent for |
|---------|--------|---------|------------|
| Confirm listener 409-locked | 6,062 × `WARNING` | no | ~10 h, cost 4 orders |
| 20:30 screener never ran | 2 lines in a log file | no | a full evening |
| Feed dead after token expiry | `ERROR`, then nothing | no | 6 h overnight |
| Token expired → crash loop | `TokenException` ×13 | no | until manually checked |

The app already has a reliable outbound channel — Telegram `sendMessage` — and
does not use it for its own health. That is the whole plan.

**Guiding rule:** a component that stops working must either (a) send one
Telegram message, or (b) exit non-zero so Docker restarts it. Never log-and-continue.

Ordered by (incident prevented ÷ effort). P1–P3 are small and would have caught
every row above.

---

## P1 — Single-instance lock on the watcher

**Prevents:** the 409 dual-poller outage (RCA Incident A), permanently and
regardless of how the process was started.

Disabling the systemd unit removed *today's* cause. It does not stop a stray
`uv run nse-alert watch`, a second `docker compose` project, or a future
supervisor from doing it again.

**Implement:** on `watch` startup, acquire an exclusive `flock` on
`<STATE_DIR>/watch.lock` and hold it for the process lifetime.

```python
# nse_alert/lock.py
import fcntl, os, sys
from pathlib import Path

def acquire_single_instance(state_dir: Path) -> "IO":
    lock_path = state_dir / "watch.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit(
            f"another nse-alert watch already holds {lock_path} — refusing to "
            "start a second watcher (see docs/RCA-2026-09-17.md)"
        )
    fh.write(str(os.getpid())); fh.flush()
    return fh  # keep referenced; closing releases the lock
```

Call it in `watch` before any Telegram or Kite setup. Exit **non-zero** with
that message; do not fall through to polling.

**Critical detail:** `STATE_DIR` must be the bind-mounted `.nse_alert`, which
both the container (`/data/.nse_alert`) and host `uv run` already share — that
is what makes the lock effective across both runtimes. A lock inside the
container's own filesystem would not see a host process.

**Test:** start `watch`, start a second one, assert the second exits non-zero
and the first is undisturbed. Also assert the lock is released on SIGTERM so a
normal `docker compose up --force-recreate` is not blocked.

---

## P2 — Alert when the confirm listener is failing

**Prevents:** the exact silent failure that cost four orders.

In `confirm_bot._run`, count consecutive `getUpdates` failures. On the 3rd,
send **one** Telegram message, then back off (exponential, cap ~5 min) and do
not repeat the alert until a poll succeeds.

Message must name the cause, because 409 is not self-explanatory:

```
⚠️ Confirm listener is down — order confirmations will NOT be received.
409 Conflict: another process is polling this bot token.
Check: exactly one `nse-alert watch` may run. See docs/ORDERS.md.
```

**Why this works even when broken:** `sendMessage` has no single-consumer
restriction. A 409-locked listener can still warn you. Use the existing
notifier, not a new HTTP call.

Reset the counter on any successful poll. Log at `ERROR`, not `WARNING` —
6,062 `WARNING`s scrolled past unnoticed.

---

## P3 — Notify on order expiry

**Prevents:** pending orders dying quietly at `TRADE_CONFIRM_TTL_MINUTES`.
Would have surfaced Incident A within 30 minutes instead of at day's end.

When the order book expires a pending order, send one Telegram line per expiry
(or one batched message per sweep if several expire together):

```
⏱ Order EXPIRED unconfirmed: ASTEC BUY 66 @ ~₹753 (id 4DE882, 30 min TTL)
```

Batch if >3 in one sweep to avoid a burst. Include the id so it is greppable
against `orders.json`.

---

## P4 — Validate the Kite token at startup, and fail loudly

**Prevents:** this morning's crash loop, and the 09:30 mis-sized order on
2026-09-17 (`margin-size fallback (Incorrect api_key or access_token)`).

Today the process discovers a bad token deep inside universe-building, raises
`TokenException`, and Docker's `restart: unless-stopped` loops it — 13 restarts
with no notification. `docker ps` shows `Restarting`, which nobody watches.

**Implement:** immediately after loading settings in `watch`, call a cheap
authenticated endpoint (`kite.profile()`). On `TokenException`:

1. Send Telegram: `🔑 Kite token expired or invalid — watcher cannot start. Run the daily login, then: docker compose up -d --build --force-recreate`
2. Exit **non-zero**.

**Also add a crash-loop guard** so a bad token cannot hammer Kite auth. In
`docker-compose.yml`:

```yaml
    restart: on-failure:3
```

`unless-stopped` retries forever, which is wrong for a credential error that
cannot self-heal. Three attempts then stop is the correct behaviour — and a
stopped container is far more visible than a restarting one.

**Separately:** never let sizing silently fall back when the token is bad. The
2026-09-17 09:30 order was sized by a fallback path while auth was failing.
Refuse to size, and alert.

---

## P5 — Alert when the market feed drops

**Prevents:** the 6-hour overnight silence on 2026-09-18 — WebSocket 403'd at
02:22 IST, watcher stopped retrying, process stayed alive looking healthy.

Two parts:

1. **Alert on disconnect.** After N consecutive reconnect failures (say 3),
   send one Telegram message naming the close code. A `403` during market hours
   means the token died and needs the daily login.
2. **Never sit idle.** If reconnection is abandoned, **exit non-zero** rather
   than leaving a live process with a dead socket. A process that cannot do its
   job must not look healthy to `docker ps`.

Only alert during market hours (09:15–15:30 IST, weekdays) to avoid overnight
noise — but still exit, so the state is never "running but useless".

---

## P6 — Daily heartbeat

**Prevents:** the general class. Every incident so far was an *absence* — a
message that never arrived — and absence is exactly what a person does not
notice.

One Telegram message per trading day, after the close (fold into the existing
15:40 IST `report` cron — no new scheduling):

```
📊 Daily heartbeat — 2026-09-18
Watcher:   up 6h12m | mode=confirm | feed=kite ✅
Alerts:    23 fired (4%:11  7%:6  11%:4  13%:2)
Orders:    2 placed, 1 expired unconfirmed, 0 rejected
Screener:  scheduled 20:30 IST
Warnings:  none
```

Include `mode=` explicitly — it would have caught the `auto`/`confirm` drift on
2026-09-17. If any component is unhealthy, say so on its own line.

**Then invert the habit:** no heartbeat by 16:00 IST is itself the alarm.

---

## Cross-cutting

- **One notifier path.** Route all health alerts through the existing
  `TelegramNotifier` so `TELEGRAM_CHAT_ID` and formatting stay consistent.
- **Deduplicate.** Health alerts must be edge-triggered (on transition), never
  per-iteration. The 6,062-warning flood is the anti-pattern.
- **Prefix health alerts** (`⚠️`, `🔑`, `📊`) so they are visually distinct from
  trade alerts in the same group.
- **Env toggles**, defaulting to enabled: `HEALTH_ALERTS_ENABLED=true`,
  `HEALTH_ALERT_COOLDOWN_MIN=15`. Document in `.env.example`.
- **Tests:** each alert path needs a unit test with the notifier mocked,
  asserting exactly-once-per-transition. Follow the `CliRunner` pattern from
  PR #17's `tests/test_cli.py`.

## Suggested sequencing

P1 + P4 first — both are small, and together they close the two failure modes
that can still silently cost money (duplicate watchers, dead token). P2 + P3
next. P5 and P6 after.

P1–P4 are each a self-contained PR. Do not batch them.

## Docs to update (per `docs/UPKEEP.md`)

- `.env.example` — new `HEALTH_*` vars
- `docs/ORDERS.md` — the expiry notification and the confirm-listener alert
- `deploy/CLOUD.md` — `restart: on-failure:3`, and what a stopped container means
- `docs/ARCHITECTURE.md` — new `lock.py` / health module
- `docs/RCA-2026-09-17.md` — tick off items in the Actions section as they land
