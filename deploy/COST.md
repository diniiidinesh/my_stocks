# Which cloud? Cost guide for nse-alert

You need a small always-on Linux box with a **fixed public IP** (for API orders from Apr 2026). Specs are tiny: 1 vCPU, 1 GB RAM is enough.

## What actually matters

| Need | Why |
|------|-----|
| Fixed public IPv4 | Zerodha static IP whitelist for **orders** |
| Always on 9:15–15:30 IST | `watch` process |
| Low latency to India | Nice-to-have for quotes/orders |
| Simple billing | Avoid surprise AWS invoices |

Market-data alerts alone can run anywhere; **orders** should run on the whitelisted IP machine.

## Rough monthly cost (approx. INR, 2026)

| Option | Region | Ballpark | Static IP | Verdict for this app |
|--------|--------|----------|-----------|----------------------|
| **AWS Lightsail** | Mumbai | **~$5–7 ≈ ₹450–600** (1 GB) | Included when attached | Good if you want AWS + India POP |
| **AWS EC2 t3.micro** | Mumbai | Similar + more setup | Elastic IP free while attached | Overkill vs Lightsail |
| **DigitalOcean** | Bangalore | **~$6 ≈ ₹500–650** (1 GB) | Included | Excellent docs; slightly farther than Mumbai |
| **Hetzner** | EU / Singapore | Often cheapest € | IPv4 ~€0.50 extra | Cheap, but **no India region** (higher latency) |
| **Oracle free tier** | Mumbai | ₹0 | Yes (limits apply) | Tempting; free-tier reliability is hit/miss |
| Home PC + ISP static IP | — | ISP fee varies | If ISP sells static | Possible; power/outages are the risk |

Plus your existing **Kite Connect ~₹500/mo**.

## Recommendation

**For your test run: AWS Lightsail Mumbai ($5–7/mo) or DigitalOcean Bangalore (~$6/mo).**

- Prefer **Lightsail Mumbai** if you care about Indian latency and a flat AWS bill with an easy static IP.
- Prefer **DigitalOcean** if you want the simplest beginner UX/docs.
- Skip full **EC2** unless you already know AWS networking.
- Skip **Hetzner** unless you accept EU/SG latency for NSE.
- Try **Oracle free** only if you’re comfortable with reclaim risk.

You do **not** need a large AWS account or many services — one small VM + Docker is enough.

## Total lean monthly stack

| Item | Cost |
|------|------|
| Kite Connect | ~₹500 |
| Lightsail / DO droplet | ~₹450–650 |
| Telegram | ₹0 |
| **Total** | **~₹950–1,150** |

See [CLOUD.md](CLOUD.md) for deploy steps once you pick a provider.
