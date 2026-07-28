# 13 — Pricing Recommendations

> The launch price book: plan lineup, the credit rate card, packs, and policy. Anchored against
> Apollo.io ($49–119/seat), ZoomInfo (enterprise $$$), Clay ($134+/mo credits-based) — NEXUS
> bundles signals + AI SDR workflows + relationship graph, which none of them combine.
>
> Related: [04-Pricing-Engine](04-Pricing-Engine.md) · [11-Profitability-Analysis](11-Profitability-Analysis.md) ·
> [12-Cost-Analysis](12-Cost-Analysis.md)

## 1. Plan lineup (USD, per seat per month; annual = pay 10/12)

| | **Free** | **Starter $39** | **Growth $79** | **Professional $129** | **Business $199** | **Enterprise custom** |
|---|---|---|---|---|---|---|
| Seats | 1 | ≤5 | ≤25 | ≤100 | ≤250 | unlimited |
| Credits /seat/mo | 100 (hard cap) | 750 | 2,000 | 4,000 | 8,000 | contract |
| Credit rollover | — | — | 1 mo | 2 mo | 3 mo | contract |
| Modules | core CRM-lite, inbox, signals view | + outreach, cadences, discovery-lite | + network graph, calling, plays, lookalikes | + API access, SEP push, priority support | + advanced controls, audit export, sandbox | everything + SLA, dedicated infra, custom models |
| ICP discovery | — | 5 accts/day | 20/day | 50/day | 100/day | contract |
| CRM connections | — | 1 | 1 | 2 | unlimited | unlimited |
| Network sources /seat | — | — | 2 | 3 | 5 | contract |
| Email verifications /mo | 50 | 1,000 | 5,000 | 15,000 | 40,000 | contract |
| Storage | 0.5 GB | 2 GB | 10 GB | 25 GB | 100 GB | contract |
| Support | community | email | email | priority | priority + onboarding | dedicated CSM, SLA |
| Overage | blocked | credit packs | packs/auto | packs/auto | packs/auto | contract rates |

Also in the schema, not on the public page: `Trial` (14-day, Growth entitlements, 1,000
credits), `Partner`, `Internal`, `Usage-Based` ($0 base, packs only, all modules, for PLG
experiments), `Unlimited` (negotiated), `Grandfathered` (auto-created at migration,
[15](15-Migration-Strategy.md)).

## 2. Credit rate card (1 credit = $0.01 list; margins from [12](12-Cost-Analysis.md))

| Capability | Credits | List $ | Unit COGS | Gross margin |
|---|---|---|---|---|
| `ai.email_draft` | 2 | $0.02 | $0.0012 | 94% |
| `outreach.cadence_touch` | 2 | $0.02 | $0.0013 | 94% |
| `ai.account_qa` | 3 | $0.03 | $0.012 | 60% |
| `ai.research_brief` | 3 | $0.03 | $0.012 | 60% |
| `ai.call_script` | 2 | $0.02 | $0.0016 | 92% |
| `ai.contact_rank` | 1 | $0.01 | $0.0009 | 91% |
| `ai.chat_turn` | 1 | $0.01 | $0.0010 | 90% |
| `ai.icp_from_website` | 5 | $0.05 | $0.010 | 80% |
| `ai.personalization_fetch` | 8 | $0.08 | $0.030 | 62% |
| `discovery.account_added` | 5 | $0.05 | $0.015 | 70% |
| `discovery.lookalike_company` (run) | 25 | $0.25 | $0.10 | 60% |
| `discovery.lookalike_contact` (run) | 2 | $0.02 | $0.0005 | 97% |
| `enrich.account` | 3 | $0.03 | $0.010 | 67% |
| `enrich.contact` | 4 | $0.04 | $0.012 | 70% |
| `enrich.source_committee` | 15 | $0.15 | $0.05 | 67% |
| `verify.email` | 0.25 | $0.0025 | $0.0002 | 92% |
| `outreach.email_send` | 1 | $0.01 | $0.0001 | 99% |
| `outreach.sep_push` / `integration.crm_sync` | 0.5 | $0.005 | $0.0001 | 98% |
| `network.source_sync` (run) | 2 | $0.02 | $0.002 | 90% |
| `network.search` | 0.5 | $0.005 | $0.0002 | 96% |
| `network.linkedin_import` (run) | 5 | $0.05 | $0.0005 | 99% |
| `calling.brief` | 2 | $0.02 | $0.001 | 95% |
| `calling.minutes` | 4 /min | $0.04 | $0.014 | 65% |
| `notify.webhook` / `notify.slack` | 0.1 | $0.001 | $0.0001 | 90% |
| `workflow.orchestration_run` | 5 + step charges | $0.05+ | compute | ≥80% |
| `platform.storage` overage | 25 /GB-mo | $0.25 | $0.10 | 60% |
| `data.export` (run) | 5 | $0.05 | $0.001 | 98% |

Every SKU ≥ 60% — comfortably above the 50% floor; the weighted blend is ~85%
([11](11-Profitability-Analysis.md) §2). Volume tiers (10%/20% off at 50k/250k credits-month)
keep even discounted floors ≥ 50%.

## 3. Credit packs (self-serve overage)

| Pack | Price | $/credit |
|---|---|---|
| 1,000 | $12 | $0.012 |
| 10,000 | $110 | $0.011 |
| 100,000 | $1,000 | $0.010 |

Packs expire in 12 months; burn order in [04](04-Pricing-Engine.md) §2. Auto-top-up opt-in
(threshold + monthly cap) for Growth+.

## 4. Policy recommendations

- **Annual default in checkout** (2 months free) — cash-flow + churn.
- **Regional books at launch:** USD, EUR (×0.95 psychological parity), INR (×0.35 PPP-adjusted,
  Starter/Growth only) — data-only additions.
- **Trials:** card-less 14-day Growth trial; conversion nudged by usage recap email at day 10.
- **Grandfathering:** never reprice a live cohort silently; new price = new plan version, old one
  → `grandfathered`.
- **Enterprise floor:** $24k ACV minimum for custom contracts; below that, sell Business + packs.
