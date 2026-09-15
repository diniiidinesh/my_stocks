# Cloud pick + intro offers (for nse-alert)

> **Docs map:** [../docs/README.md](../docs/README.md) · install steps: [CLOUD.md](CLOUD.md)

Your workload is tiny: one Python process, Docker optional, ~1 GB RAM, fixed public IP for orders.

## Short answer

| Goal | Pick |
|------|------|
| **₹0 forever if it works** | Try **Oracle Always Free** (Mumbai) first |
| **Reliable paid fallback** | **AWS Lightsail Mumbai** (~$5–7/mo) or **DigitalOcean Bangalore** (~$6/mo) |
| **Best “try free then stay”** | DigitalOcean **$200 / 60 days**, then keep a $6 droplet |

Oracle is **enough on specs**. The catch is signup/capacity/ARM friction — not CPU.

---

## Intro / free offers (approx., check at signup)

| Provider | Intro offer | After intro | India region | Notes |
|----------|-------------|-------------|--------------|-------|
| **Oracle Cloud** | **Always Free** forever + often **$300 trial / 30 days** | ₹0 on Always Free shapes | Mumbai / Hyderabad | Best long-term free; capacity & ARM caveats |
| **AWS** (new Free plan) | Up to **$200 credits / ~6 months** ($100 on signup + earn more) | Lightsail ~$5–7/mo | Mumbai | Old 90-day Lightsail trial is gone for new users |
| **DigitalOcean** | Often **$200 / 60 days** (card required) | ~$6/mo droplet | Bangalore | Easiest UX |
| **Vultr** | Campaigns **$200–300** (often ~30 days) | ~$5–6/mo | Delhi / Bangalore etc. | Credits expire faster |
| **Linode / Akamai** | Often **~$100 / 60 days** | ~$5/mo | Various Asia | Fine alternative |

Offers change — confirm on the signup page before committing a card.

---

## Is Oracle Free Tier enough?

**Yes, for this app**, if you get an instance:

| Spec | Oracle Always Free (typical) | What we need |
|------|------------------------------|--------------|
| CPU / RAM | Up to **2 OCPU + 12 GB** ARM (or 2× tiny AMD micros) | 1 vCPU / 1 GB is plenty |
| Disk | Part of **200 GB** free block storage | 50 GB boot is fine |
| Network | Public IP available | Needed for Zerodha IP whitelist |
| Cost | **₹0** ongoing | Ideal |

### Caveats (why Google oversells it)

1. **“Out of capacity”** — Mumbai ARM free VMs are often unavailable; you may retry for days or try Hyderabad / another AD.
2. **ARM (Ampere)** — images must be `arm64` / `aarch64`. Our Dockerfile (`python:3.12-slim`) works on ARM, but some wheels occasionally lag; usually fine for this stack.
3. **Signup friction** — card + identity checks; some Indian cards get rejected; support is slower than DO/AWS.
4. **Not a guarantee** — Always Free is capacity-constrained; Oracle can be hard to get, not hard to run once you have it.
5. **Console complexity** — VCN, subnet, ingress SSH rules — steeper than Lightsail/DO.
6. **Limits (2026)** — Always Free ARM is **2 OCPU / 12 GB** total (halved from the old 4/24). Still far more than we need.

**Fallback on Oracle if A1 is out of capacity:** two Always Free **AMD** micros (`VM.Standard.E2.1.Micro`, ~1 GB each). Possible without Docker; tight with Docker — prefer A1 or a paid $5–6 box instead.

**Verdict:** Worth **trying Oracle first** for ₹0. If you can’t launch a VM in ~1–2 days, don’t fight it — move to Lightsail or DigitalOcean.

---

## Recommended VMs (exact shapes)

### A) Oracle (free) — try this first
- **Shape:** `VM.Standard.A1.Flex` — **1–2 OCPU**, **6–12 GB** RAM (use 1 OCPU / 6 GB to start)
- **Image:** Ubuntu 22.04/24.04 **aarch64**
- **Region:** `ap-mumbai-1` (fail over to Hyderabad if capacity fails)
- **Networking:** assign a **public IPv4**, open SSH (your IP only); open `8765` only when logging in
- **Whitelist** that public IP in Kite developer Profile

### B) AWS Lightsail (paid, simple)
- **Plan:** **$5 or $7** Linux (1 GB RAM) in **Mumbai**
- **Static IP:** create and attach in Lightsail console (free while attached)
- Use AWS Free credits first if you’re on the new Free plan

### C) DigitalOcean (paid, simplest UX)
- **Droplet:** **Basic $6** — 1 vCPU / 1 GB / Bangalore
- Public IPv4 included — whitelist that in Kite
- Burn intro credit, then keep the $6 box

### Skip for now
- Full **EC2** (more knobs, same job as Lightsail)
- **Hetzner** (cheap, but no India POP → higher NSE latency)
- Big multi-service AWS accounts

---

## Decision tree

```text
Want ₹0 and okay troubleshooting ARM/capacity?
  → Oracle Always Free (Mumbai A1.Flex 1 OCPU / 6 GB)
  → If stuck >2 days getting a VM → Lightsail or DigitalOcean

Want “just works” and ~₹500/mo is fine?
  → DigitalOcean $6 Bangalore  OR  Lightsail $5–7 Mumbai
  → Prefer India latency → Lightsail Mumbai
  → Prefer easiest UI → DigitalOcean
```

---

## Lean monthly cost after intros

| Stack | Monthly |
|-------|---------|
| Oracle free + Kite Connect | **~₹500** (Kite only) |
| Lightsail/DO + Kite | **~₹950–1,150** |

---

## Practical next step

1. Sign up **Oracle Cloud Free Tier**, home region **Mumbai**.
2. Try creating **A1.Flex 1 OCPU / 6 GB**, Ubuntu ARM, public IP.
3. If it launches → we’ll deploy Docker/`nse-alert` there and whitelist the IP.
4. If capacity fails → create **Lightsail Mumbai $5** (or DO $6) the same day; don’t block trading setup on Oracle luck.

When you have a public IP (Oracle or otherwise), send it and we can do the exact deploy + Kite whitelist checklist.
