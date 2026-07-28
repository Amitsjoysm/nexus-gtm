# NEXUS GTM — Commercial Operating System (Billing Platform Design)

> The complete monetization design for NEXUS GTM: a metadata-driven billing, entitlement,
> metering, pricing, subscription, and revenue-management platform in which **every capability is
> billable without code changes**, plans are pure configuration, and every tier holds a ≥50%
> gross margin (validated, not hoped — the rate card literally refuses to go live below floor).
>
> Status: **approved design set — implementation not yet started** (phases in
> [14-Implementation-Roadmap](14-Implementation-Roadmap.md)). No application code is touched by
> these documents. Discovery was grounded in the live codebase via the code-review-graph
> (26 subsystems, 373 files) — every billable resource maps to real modules.

## Reading order

**The engines**
1. [01-Billing-Architecture](01-Billing-Architecture.md) — principles, system overview, data model, the single enforcement seam
2. [02-Entitlement-Engine](02-Entitlement-Engine.md) — plan × capability policy, resolution, outcomes, backward-compat invariants
3. [03-Metering-Architecture](03-Metering-Architecture.md) — append-only usage events, idempotency, rollups, integrity guarantees
4. [04-Pricing-Engine](04-Pricing-Engine.md) — rate cards, credits, coupons, price books, the 50% margin guardrail
5. [05-Subscription-System](05-Subscription-System.md) — lifecycle, proration, dunning, PSP seam (Stripe/noop)

**The control plane**
6. [06-Admin-Portal](06-Admin-Portal.md) — staff roles, full portal map, revenue/cost/margin dashboards
7. [07-Enterprise-Licensing](07-Enterprise-Licensing.md) — sales-built custom bundles, contracts, minimum commits

**What we sell**
8. [08-Feature-Catalog](08-Feature-Catalog.md) — the 58-capability catalog (billing_capabilities seed)
9. [09-Billable-Resources](09-Billable-Resources.md) — Step-1 discovery: every resource mapped to code
10. [10-Usage-Tracking](10-Usage-Tracking.md) — capture layers, customer & CS views, privacy/retention

**The money**
11. [11-Profitability-Analysis](11-Profitability-Analysis.md) — personas, plan margins (79–87%), routing & guardrail policies
12. [12-Cost-Analysis](12-Cost-Analysis.md) — per-unit COGS from the real provider stack (Groq/Exa/Reacher/Apify/Twilio)
13. [13-Pricing-Recommendations](13-Pricing-Recommendations.md) — Free→Enterprise lineup, full credit rate card, packs, policy

**Getting there safely**
14. [14-Implementation-Roadmap](14-Implementation-Roadmap.md) — 10 phases, each shippable + reviewable
15. [15-Migration-Strategy](15-Migration-Strategy.md) — legacy-unlimited grandfathering, shadow-first, kill switch, zero regression
16. [16-Testing-Strategy](16-Testing-Strategy.md) — property-tested money math, simulated billing month, shadow-safety CI gate
17. [17-Production-Checklist](17-Production-Checklist.md) — go-live gates (config, rails, scale, audit, legal)
18. [18-Future-Expansion](18-Future-Expansion.md) — public API, marketplace, premium models, 10⁶-customer scale path

## The one-paragraph pitch

Everything monetizable is a row in a catalog. Application code asks one function — 
`check_and_meter(tenant, capability, qty)` — and never mentions a plan. Usage is an idempotent,
tenant-attributed event stream with COGS stamped at write time. Plans, rate cards, credits,
coupons, and enterprise contracts are pure data edited in an audited Admin portal, so Product
launches plans, Sales builds custom bundles, and Finance re-prices — all without engineering.
Existing tenants are grandfathered onto an unlimited legacy plan and the whole system runs in
shadow mode first, so shipping it changes nothing until a human flips a switch.

## Outcomes checklist (from the brief)

- PMs launch plans without engineering ✔ (Admin plan CRUD, [06](06-Admin-Portal.md))
- Sales builds enterprise bundles in minutes ✔ ([07](07-Enterprise-Licensing.md))
- Finance measures profitability by customer/feature/org/plan ✔ (COGS-stamped events, [11](11-Profitability-Analysis.md))
- CS spots upgrade opportunities ✔ (denial/soft-limit pipeline, [10](10-Usage-Tracking.md))
- Engineering registers a feature once ✔ (catalog row + one seam call, [08](08-Feature-Catalog.md))
- Executives see ARR/MRR/churn/margin live ✔ (dashboards, [06](06-Admin-Portal.md) §3)
- ≥50% gross margin on every tier ✔ enforced as validation ([04](04-Pricing-Engine.md) §5, proven in [11](11-Profitability-Analysis.md))
