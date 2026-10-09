# Stripe-managed plans and subscriptions (superadmin)

Date: 2026-10-09. Status: approved by the product owner in chat, 2026-10-09.

## Why

1. **"Product id not available" after adding a new Stripe key.** Checkout caches each plan's Stripe
   product and price ids in `billing_plans.meta` and each workspace's Stripe customer id in
   `billing_subscriptions.psp_customer_id`, and reuses them without recording WHICH Stripe account
   they belong to. Production switched from a test key to a live key and staging switched to a new
   test account, so checkout sends ids from the old account to the new one and Stripe answers
   "No such price / product / customer". Locally `launch` and `internal` hold ids from
   `acct_…FIw0Fxml7q`.
2. **A superadmin cannot run Stripe-billed plans and subscriptions from the console.** Plans get a
   Stripe object only on the first self-serve purchase. "Change plan" changes only our row, so a
   workspace billed through Stripe keeps being charged the old price. Nothing creates a Stripe
   subscription from the admin side. The New plan / Edit dialog exists only on unreleased local
   commits.

## Decisions (product owner, 2026-10-09)

| Question | Decision |
|---|---|
| Where plans are defined | NEXUS is the catalog and creates them in Stripe |
| Superadmin starts a subscription | Two methods: emailed Stripe invoice, or a Checkout link to send |
| Superadmin changes a Stripe-billed plan | At Stripe, now, prorated, difference invoiced immediately |
| Credits on an invoiced subscription | When the invoice is paid, not when the subscription starts |
| Invoice terms | 14 days by default, editable per subscription |
| Who | **Platform superadmin only.** Not Finance, not Support, not workspace admins |
| Existing behaviour | Must not change: self-serve Checkout and portal, webhooks, cancel, rate cards, entitlements, non-Stripe plan changes, enterprise invoice collection |

## Design

### 1. Stripe ids carry their account

- `stripe_catalog.current_account(provider)` returns the Stripe account id (`acct_…`) the active key
  belongs to: the verified Payments credential's `account_id`, else one `GET /v1/account` cached per
  key digest for the life of the process. The noop provider answers `"noop"`.
- `billing_plans.meta` gains `price_account` beside `product_id` / `price_id` and the price terms.
  `billing_subscriptions.meta` gains `psp_customer_account`.
- **Reuse rule:** an id is reused only when its recorded account is the current one. A cache written
  before this change has no account: the object is fetched once (`GET /v1/prices/{id}`,
  `GET /v1/customers/{id}`); a 404 means another account, anything else adopts the current account.
- **Within one account a plan keeps one product.** A new price is created only when the amount,
  currency or interval changed; the superseded price is archived (`active=false`). Today's
  `ensure_plan_price` relies on a 24-hour idempotency key and creates a duplicate product after that.
- Ids from another account are not deleted. They move to `meta.previous_stripe` so an operator can
  still trace them.

### 2. Plans live in Stripe (superadmin)

- **Publish** (`PUT /admin/billing/plans/{id}/status` → `active`) makes the plan's product and price
  exist in the current account first. If Stripe refuses, the plan stays unpublished and the response
  is 502 with Stripe's reason. A plan on sale is therefore always buyable. Publishing with the noop
  provider (no Stripe) behaves as today.
- **Edit** (`PATCH /admin/billing/plans/{id}`): a new name renames the Stripe product; a new base
  price on an active plan creates a new Stripe price and archives the old one. Existing subscribers
  stay on their price until a plan change moves them (Stripe prices are immutable).
- **Hold** (status → `draft`) archives the plan's price so nobody new can be put on it; republishing
  reactivates it. Archiving never blocks the hold.
- **Sync to Stripe** (`POST /admin/billing/plans/stripe-sync`): ensures every active standard plan
  exists in the current account and reports created / reused / failed per plan. It also runs when a
  Payments credential for a different account is activated, and its report rides on that response.
- Self-serve checkout uses the same account-aware path, which is what fixes the reported error.

### 3. Superadmin starts a subscription for a workspace

`POST /admin/billing/tenants/{tenant_id}/stripe-subscription` with `plan_id`, `method` and
`days_until_due` (default 14, 1–90):

- `method: "invoice"`: ensures the workspace's Stripe customer (billing email: the workspace owner)
  and creates the Stripe subscription with `collection_method=send_invoice`, the metadata the webhook
  already reads (`tenant_id`, `plan_id`) plus `nexus_credits=on_payment`, then finalizes and sends
  the first invoice so Stripe emails it now. Our row records the Stripe ids and the plan immediately.
  **Credits are granted when that invoice is paid** (`invoice.paid`), keyed per plan per period like
  every other grant. Unpaid by the due date → Stripe marks it past due → existing dunning.
- `method: "checkout_link"`: creates a Checkout session for that workspace and plan (the same call
  self-serve uses) and returns its URL and expiry (Stripe's 24-hour limit). The subscription starts
  when the customer pays, through the existing webhook, with today's credit rule (card paid now).
- Refused (409) when the workspace already has an active Stripe subscription (use a plan change),
  when the plan is free, trial, internal or unlimited, or when no Stripe credential is in force.
  Allowed for standard plans and for that workspace's own custom plan.

### 4. Superadmin changes the plan of a Stripe-billed workspace

`POST /admin/billing/tenants/{tenant_id}/subscription` keeps its path and body. When the workspace's
subscription has a Stripe id and Stripe is the provider in force, it now updates the Stripe
subscription: the single subscription item moves to the new plan's price,
`proration_behavior=always_invoice`, metadata `plan_id` updated. Our row changes only after Stripe
agrees (as cancel does since 2026-09-30); a refusal is a 502 that says nothing changed here. Credits
follow the subscription's rule: card-collected now (webhook, as today), invoice-collected when the
proration invoice is paid. A workspace not billed through Stripe keeps today's NEXUS-only change and
today's permission.

### 5. Superadmin only

- New platform permission `stripe.manage`, in the `superadmin` preset only. Migration `0067`
  backfills it into stored `platform_admins.permissions` rows that held every permission existing
  before it, exactly as `0055` did for `features.manage`; a narrowed row stays narrowed. The env
  allowlist keeps full power.
- Gated on `stripe.manage`: create plan, edit plan, publish/hold, Sync to Stripe, start a Stripe
  subscription, and a plan change for a Stripe-billed workspace (403 for anyone else, naming the
  permission). Rate cards and entitlements stay on `pricing.write`; NEXUS-only plan changes stay on
  `subscriptions.write`.
- The console hides those controls from admins without the permission.

### 6. Webhook changes (additive)

- A plan applied from `customer.subscription.*` whose metadata says `nexus_credits=on_payment` takes
  the plan's terms but defers the grant: `sub.meta.credits_pending_plan = plan_id`.
- `invoice.paid` for that subscription grants the pending plan's credits and clears the marker.
- Everything else in the handler is unchanged. Subscriptions without the marker behave as today.

### 7. Console (UI)

- **Plans tab:** New plan / Edit / Publish / Hold shown only with `stripe.manage`; each plan shows
  whether it exists in the current Stripe account; a **Sync to Stripe** button with the report.
- **Customers tab, workspace actions:** **Start Stripe subscription** dialog: plan, method (Email an
  invoice / Checkout link), days to pay. The result shows the invoice link or the Checkout link with
  a copy button and its expiry.
- **Change plan:** for a Stripe-billed workspace the dialog says the price changes at Stripe now and
  the prorated difference is invoiced immediately.
- **Payments tab:** activating a credential for a different account shows the sync report.

## Error handling

- Every Stripe write happens before our row changes; a refusal leaves our state untouched and returns
  Stripe's words (502). Archiving a superseded price is best effort and reported, never blocking.
- No Stripe credential in force: the new superadmin actions answer 409 with what to set; publish,
  edit and hold behave as today (nothing to create).
- Idempotency keys on creates include the account and the terms, so a retry never duplicates.

## Stripe key permissions

Add **Subscriptions: Write** (creating and changing subscriptions) to the restricted key. Products,
Prices, Customers, Checkout Sessions and Invoices: Write are already required.
`docs/billing/stripe-connection-guide.txt` is updated.

## Testing

- Stripe provider calls against `httpx.MockTransport` (the existing pattern): account lookup, product
  reuse, price archive, subscription create/send, price swap, 404-on-legacy detection.
- Flows on the noop provider: account switch re-creates plan and customer ids; checkout unchanged
  within one account; publish/edit/hold; start subscription (both methods); plan change routing.
- Signed webhook events: deferred credits granted on `invoice.paid`, never twice; card subscriptions
  unchanged.
- Permission matrix: superadmin 200, finance/support 403, workspace admin 401/403; migration
  backfill.
- UI source tests for the gated controls (no frontend runner). Full CI-equivalent before landing.

## Out of scope

- Moving live subscriptions between Stripe accounts.
- Plan switching inside the Stripe customer portal (stays off).
- Reconciliation noise for subscriptions that belong to a previous account (reported, not fixed).
