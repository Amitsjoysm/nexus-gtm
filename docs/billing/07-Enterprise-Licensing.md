# 07 — Enterprise Licensing & Bundling

> Sales builds custom enterprise deals in minutes — seats, credits, modules, rates, SLAs — as
> configuration on top of the same engines. Zero engineering per deal.
>
> Related: [04-Pricing-Engine](04-Pricing-Engine.md) · [06-Admin-Portal](06-Admin-Portal.md)

## 1. The contract object

```
billing_contracts (TenantScoped)
  name · status(draft|pending_finance|active|expired) · start/end · auto_renew
  base_fee_usd · billing_interval · minimum_commit_usd · payment_terms(net30…)
  bundle jsonb:                 # everything a deal can include, all optional
    seats: {included, extra_price}
    credits: {monthly_grant, rollover_months}
    storage_gb, api_calls, searches, ai_tokens, signals, alerts, exports…   # quota overrides
    modules: [module.calling, module.network, …]                            # enable/disable
    rate_overrides: {capability_id: credits_price}                          # negotiated rates
    support_level: standard|priority|dedicated
    sla: {uptime_pct, response_hours}
    dedicated_infra: bool       # ops-fulfilled; billed as line item
    custom_ai_models: [{provider, model}]        # routes via existing LLM provider chain config
    custom_branding: bool
  entitlements jsonb            # compiled full override map (generated from bundle)
  approved_by · approved_at
```

The **bundle** is what sales edits; the **entitlements map** is compiled from it (deterministic)
and is what the entitlement engine resolves first ([02](02-Entitlement-Engine.md) §2). SLAs,
support level, dedicated infra, and branding are fulfillment metadata — they price into the base
fee and surface in Admin/CS views, and (for branding/models) map to feature flags and provider
config the platform already supports.

## 2. Deal workflow

```
Sales (role: sales) → Contract Builder in Admin:
  1. pick tenant (or create) → template (Enterprise / Partner / Custom)
  2. adjust bundle sliders; live margin preview per line (rate vs billing_cost_rates)
  3. margin floor check: any line < 50% flags the deal → requires finance margin_exception
  4. save draft → status pending_finance
Finance (role: finance) → review → approve ⇒ status active:
  - subscription switched to plan-class `enterprise`, contract linked
  - entitlement cache version bumped → effective immediately
Renewal: lifecycle job alerts CS at T-60/T-30; expiry without renewal ⇒ configured fallback plan
         (never hard-off; same "never hold data hostage" rule as [05](05-Subscription-System.md)).
```

## 3. Billing mechanics for contracts

- Base fee invoices on the contract interval; **minimum commit** implemented as: if rated usage
  < commit at period close, invoice tops up to commit (line: "committed minimum true-up").
- Usage above included bundle rates at `rate_overrides` (else standard card).
- Multi-year: price-book pinning + scheduled uplifts (`bundle.uplift_pct_yearly`).
- Partner class: same object, `billed=false` + revenue-share metadata (phase 3 reporting).

## 4. Guarantees

- A contract can only widen or narrow *configuration* — it cannot require code (enforced by the
  fact that it's just entitlement + rate data).
- Every contract mutation is audited; margin exceptions are permanently visible on the margin
  dashboard until the line is repriced.
- Template library ships with: "Enterprise Standard" (500k credits, 25 seats, all modules,
  priority support), "Enterprise Unlimited" (class unlimited + true COGS visibility), "Partner",
  "Pilot (90-day)" — all editable in Admin.
