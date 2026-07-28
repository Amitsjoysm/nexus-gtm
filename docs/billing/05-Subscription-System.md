# 05 — Subscription System

> Lifecycle, periods, proration, dunning, and PSP integration for tenant subscriptions.
>
> Related: [04-Pricing-Engine](04-Pricing-Engine.md) · [06-Admin-Portal](06-Admin-Portal.md) ·
> [15-Migration-Strategy](15-Migration-Strategy.md)

## 1. Subscription record

```
billing_subscriptions (TenantScoped; one active row per tenant)
  plan_id · price_book_id · status · interval(month|year)
  current_period_start/end · trial_end · cancel_at_period_end
  grandfathered(bool) · psp_customer_id · psp_subscription_id (nullable — internal plans have none)
  contract_id (nullable → billing_contracts)
```

## 2. Lifecycle state machine

```
        ┌────────┐  start trial  ┌─────────┐  convert  ┌────────┐
 new ──▶│ trialing│──────────────▶ active  │◀──────────│ past_due│──▶ suspended ──▶ canceled
        └───┬────┘   expire      └──┬──────┘  payment  └───▲────┘   (grace over)    (final)
            └────────▶ free/blocked │ cancel_at_period_end │ payment fails
                                    └──────────────────────┘
```

- `trialing → active` on first successful payment or admin conversion; `trialing → free` on
  expiry (data intact, entitlements drop to Free — nothing is deleted).
- `active → past_due` on failed charge; **dunning**: retries at day 1/3/7 (PSP smart retries when
  available), soft-warn banners in-app, CS alert at day 3.
- `past_due → suspended` after grace (default 14d): entitlements drop to Free-equivalents,
  workers skip the tenant's automation (same gate pattern as `automation_enabled`) — but reads
  keep working. **We never hold data hostage.**
- Cancel = `cancel_at_period_end`; immediate cancel is an admin action with prorated credit note.

All transitions are events (`subscription.*`) on the EventBus → audit log + CS alerting, driven
by a `billing_lifecycle` heartbeat job (same idempotent per-interval pattern as
`discover_icp_accounts`).

## 3. Changes & proration

- **Upgrade (mid-period):** immediate entitlement switch; base-fee delta prorated by remaining
  period-days on the next invoice; credit grants topped up to the new plan's grant immediately
  (upgrades should feel instant).
- **Downgrade:** scheduled for period end by default (no clawbacks); immediate downgrade is an
  admin override with prorated credit note.
- **Seat changes:** metered as seat-days ([04](04-Pricing-Engine.md) §rating) — adding a seat on
  day 20 costs 10/30ths of the seat price; removing stops accrual next day. No proration
  special-cases in code; it falls out of the metering.
- **Interval switch (mo↔yr):** takes effect at period end; early switch = admin action.

## 4. PSP integration (`nexus/billing/payments/`)

`PaymentProvider` protocol (mirrors every other provider seam in this codebase):

```python
class PaymentProvider(Protocol):
    name: str
    async def ensure_customer(tenant) -> psp_customer_id
    async def checkout_session(tenant, plan, price) -> url          # hosted checkout
    async def charge_invoice(invoice) -> PaymentResult              # off-session
    async def refund(payment, amount) -> PaymentResult
    def verify_webhook(headers, body) -> PSPEvent                   # signature check
```

- `stripe.py` — reference adapter (Checkout + Billing webhooks: `invoice.paid`,
  `invoice.payment_failed`, `customer.subscription.deleted`). Webhooks are verified, idempotent
  (event-id key), and only *advance our state machine* — Stripe is the collection rail, our
  tables are the system of record.
- `noop.py` — offline/test/internal: checkout auto-succeeds, charges always approve. The entire
  suite runs against it (zero-network rule).
- Config: `NEXUS_BILLING_PSP=noop|stripe`, `NEXUS_STRIPE_SECRET/WEBHOOK_SECRET` — inert until set.

## 5. Invoicing

- Generated at period close by the lifecycle job: draft → finalize (immutable, numbered
  `INV-{yyyy}{seq}`) → collect (PSP) → paid/failed.
- Lines come from rating ([04](04-Pricing-Engine.md)); PDF rendering is a later phase — v1 serves
  a print-ready HTML invoice (matches the SPA's server-rendered simplicity).
- Refunds/corrections = credit notes referencing the original invoice; originals never mutate.

## 6. Tenant-facing surface

`/settings → Billing` section (frontend phase): current plan, usage meters per capability with
soft-limit bars, credit balance + purchase, invoice history, payment method (PSP-hosted portal
link), cancel/upgrade. Upgrade CTAs deep-link from every 402 response's payload
([02](02-Entitlement-Engine.md) §3).
