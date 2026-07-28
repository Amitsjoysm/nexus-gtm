# 11 — Profitability Analysis

> Proof that the 50% gross-margin floor holds under real usage — per SKU, per plan, per persona —
> plus the routing and guardrail policies that keep it true as costs move.
>
> Related: [12-Cost-Analysis](12-Cost-Analysis.md) · [13-Pricing-Recommendations](13-Pricing-Recommendations.md) ·
> [04-Pricing-Engine](04-Pricing-Engine.md) §5 (margin guardrail)

## 1. Usage personas (per seat per month, from product mechanics)

| Capability (credits ea.) | Light | Average | Heavy | Enterprise seat |
|---|---|---|---|---|
| Email drafts (2) | 40 | 150 | 400 | 600 |
| Cadence touches (2) | 30 | 120 | 400 | 800 |
| QA / research (3) | 10 | 40 | 120 | 200 |
| Discovery accounts (5) | 25 | 100 | 400 | 600 |
| Contact enriches (4) | 30 | 100 | 300 | 500 |
| Verifications (0.25) | 100 | 400 | 1,500 | 3,000 |
| Sends (1) | 60 | 250 | 800 | 1,500 |
| Network searches (0.5) | 10 | 40 | 150 | 250 |
| Misc (scripts, syncs, briefs) | 30 cr | 100 cr | 300 cr | 500 cr |
| **Credits consumed** | **~465** | **~1,700** | **~5,600** | **~9,800** |
| **COGS (blended $0.0016/cr)** | **$0.74** | **$2.7** | **$9.0** | **$15.7** |

Blended COGS/credit ≈ $0.0016 (weighted by the mix above against
[12](12-Cost-Analysis.md) §2) — i.e. ~16% of the $0.01 list credit → **~84% blended gross margin
on consumption**.

## 2. Plan-level gross margin (revenue vs. COGS + infra allocation)

| Plan | Price/seat | Persona fit | COGS+infra/seat | **Gross margin** |
|---|---|---|---|---|
| Free | $0 | light-capped (100 cr) | ~$0.30 | — (CAC, capped ~$0.35 worst case) |
| Starter $39 | avg-light (750 cr) | $1.2 COGS + $1 infra + $6 support | **~79%** |
| Growth $79 | average (2,000 cr) | $3.2 + $1 + $6 | **~87%** |
| Professional $129 | heavy (4,000 cr) | $6.4 + $1 + $15 | **~83%** |
| Business $199 | very heavy (8,000 cr) | $12.8 + $1.5 + $15 | **~85%** |
| Enterprise | contract | contract mix | floor-validated per line | **≥50% enforced** |

Worst-case stress: a seat that burns its **entire** grant on the lowest-margin SKU (60%,
e.g. QA) still leaves plan margin ≥ 60% on consumption + the base fee covering fixed costs —
the floor cannot be breached by mix alone. Overage is priced from the same card, so heavy usage
*raises* absolute profit at ≥60% marginal margin.

## 3. Net margin bridge (planning model)

`Gross (≈84%) − support (≈8% blended) − infra growth reserve (2%) − eng/maintenance (15%) −
payment fees (2.9% + 30¢ on self-serve) → target net ≈ 55–60%` at 500+ paid seats. Payment fees
on annual invoices via ACH/invoice (Enterprise) drop to <1%.

## 4. The three policies that protect margin permanently

1. **Model routing:** Groq is primary (config today: `llm_provider=auto`, Groq keyed). If
   Anthropic is enabled as primary, `ai.email_draft` COGS rises ~10× ($0.0012→$0.011) — margin
   falls to ~45% at 2 credits. Policy: premium-model routing is an **entitlement**
   (`ai.premium_model`, Business+/Enterprise, 3× credit multiplier on the rate card) — model
   choice becomes a priced feature, not a silent COGS change.
2. **Guardrail validation:** rate-card activation refuses < 50% margin at current
   `billing_cost_rates` ([04](04-Pricing-Engine.md) §5). When a provider reprices, Finance
   updates the cost rate and the dashboard immediately flags every SKU that fell below floor.
3. **Spike containment:** burst limits/cooldowns per entitlement + anomaly watch
   ([10](10-Usage-Tracking.md) §3) cap runaway automation COGS at the seam, not in incident
   response.

## 5. Executive KPIs (Margin dashboard, [06](06-Admin-Portal.md) §3)

MRR/ARR, blended gross margin, margin by plan / by tenant / by capability, COGS per customer,
credits-consumed vs granted ratio (expansion predictor), below-floor SKU count (target: 0),
margin-exception contracts list.
