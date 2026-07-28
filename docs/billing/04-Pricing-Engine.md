# 04 — Pricing Engine

> Turns metered usage + plan configuration into money: rate cards, credits, overage, volume
> tiers, coupons, regional price books, and enterprise contract rates — all data, no code.
>
> Related: [05-Subscription-System](05-Subscription-System.md) ·
> [07-Enterprise-Licensing](07-Enterprise-Licensing.md) ·
> [13-Pricing-Recommendations](13-Pricing-Recommendations.md)

## 1. Price model primitives

| Primitive | Table | What it expresses |
|---|---|---|
| **Plan** | `billing_plans` | class (`free/standard/trial/usage/hybrid/unlimited/enterprise/partner/internal/custom`), billing interval(s), base fee, included-credit grant, seat pricing, status (`draft/active/grandfathered/retired`), version |
| **Price book** | `billing_price_books` | plan × currency × region → base fee & seat price (USD base; INR/EUR/GBP books as data) |
| **Rate card** | `billing_rate_cards` | capability → unit price in **credits**, with volume tiers `[{upto, price}]` and an optional direct-USD price for pure usage plans |
| **Credit grant** | plan field + `billing_credit_ledger` | monthly included credits; add-on credit packs; promo credits — all ledger entries with expiry |
| **Coupon** | `billing_coupons` | `%` off base, fixed off, or bonus credits; window, max redemptions, plan-class allowlist |
| **Contract** | `billing_contracts` | enterprise: negotiated base, custom rate card overrides, minimum commit, true-up rules ([07](07-Enterprise-Licensing.md)) |

**The credit is the universal unit.** 1 credit = $0.01 list. Every consumable capability is
priced in credits on the rate card ([13-Pricing-Recommendations](13-Pricing-Recommendations.md)
has the full card). This gives one mental model to customers ("everything costs credits"),
one overage SKU, and one knob per capability for margin control.

## 2. Rating algorithm (period close, and on-demand preview)

```
for each capability with usage in period:
    ent  = resolved entitlement (contract > plan)          # [02]
    incl = min(usage, ent.quota or ∞)                      # included units → 0 credits
    over = usage - incl
    price = contract.rate_override or rate_card.tiered_price(over)
    credits_due += over × price
burn order: expiring promo credits → purchased packs → monthly grant → overage invoice line
invoice = base fee (price book, prorated)                  # [05]
        + seat lines (Σ seat-days × seat price / period days)
        + credit-pack purchases
        + overage line (credits_due beyond balance × $0.01)
        − coupon adjustments (audited)
        + tax hook (v1: pass-through zero; Stripe Tax later)
```

Deterministic and replayable: rating reads only rollups + config versions effective in the
period, so `reconcile_invoice` always reproduces the same lines.

## 3. Plan-class semantics (behavior is data, not code)

| Class | Base fee | Credits | Enforcement default |
|---|---|---|---|
| `free` | $0 | small monthly grant, hard-capped | block at quota |
| `trial` | $0, time-boxed | generous grant, `trial_days` from plan | converts → chosen plan or `free` |
| `standard` (Starter/Growth/Professional/Business) | per-seat/mo or /yr | per-plan grant, overage allowed | soft-warn → overage |
| `usage` | $0 base | pay-as-you-go, credit packs only | prepaid balance gate |
| `hybrid` | low base | grant + overage | soft-warn → overage |
| `unlimited` | negotiated | ∞ (metered for COGS visibility) | never block |
| `enterprise` / `custom` | contract | contract | per-contract |
| `partner` / `internal` | $0 | ∞ or granted | shadow (metered, never billed) |
| `grandfathered` | frozen legacy terms | frozen | as frozen |

Launching a new plan = insert plan + entitlements + price book rows in Admin. Zero deploys.

## 4. Discount & promotion machinery

- **Volume tiers** live on the rate card (`[{upto: 10_000, price: 1.0}, {upto: null, price: 0.8}]`).
- **Annual billing** = price-book column (typically 2 months free ≈ 17%).
- **Coupons** apply at invoice assembly, are single-audit-line, never mutate rate cards.
- **Regional pricing** = additional price books; tenant's book chosen at subscribe time and
  pinned (no silent currency flips).
- **Grandfathering** = plan `status=grandfathered`: closed to new subscribers, terms frozen;
  migrations off it are explicit admin actions with audit trail.

## 5. Margin guardrails (the 50% floor, enforced as config validation)

Admin-side validation refuses to activate a rate card entry whose credit price implies
`gross_margin < billing_cost_rates.min_margin` (default 50%) at current COGS — the pricing
engine literally will not let a plan go live underwater
([11-Profitability-Analysis](11-Profitability-Analysis.md)). Override requires a
`margin_exception` flag + reason, which shows on the margin dashboard.

## 6. APIs

- Tenant: `GET /billing/plan`, `GET /billing/usage`, `POST /billing/checkout` (PSP session),
  `POST /billing/credits/purchase`, invoice list/download.
- Admin: full CRUD on plans/rate cards/price books/coupons + `POST /admin/billing/preview`
  (rate any tenant's current period against any draft plan — the sales what-if tool).
