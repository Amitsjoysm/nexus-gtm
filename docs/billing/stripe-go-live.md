# Stripe go-live checklist

Measured against the live account on 2026-08-19, test mode (`acct_1TyQzPFIw0Fxml7q`):

| Check | State | Consequence |
|---|---|---|
| API key configured | **yes** (`sk_test_…`), provider resolves to `StripePaymentProvider` | — |
| Products / prices created by us | **yes** — 2, both carrying `plan_id` metadata | the custom-plan publish path works |
| `charges_enabled` | **false** | **no payment can be taken** |
| `details_submitted` | **false** | account onboarding never finished |
| Webhook endpoints registered | **0** | Stripe never tells us about a payment |
| Billing-portal configurations | **0** | `POST /billing/portal` fails even once charges work |
| Live subscriptions | **0** | nothing has ever been billed |

So Stripe is **connected and able to create objects, but cannot take a payment and cannot report
anything back**. Three things fix that, and all three are actions inside the Stripe dashboard.

Note the receiving side is already correct: `POST /api/billing/webhooks/stripe` is mounted and
returns **400 on an unsigned request**, verified live.

---

## 1. Finish account onboarding

Dashboard → **Settings → Business settings** (or the "Complete your account" prompt). Until
`details_submitted` and `charges_enabled` are both true, every charge attempt fails — including
`collect_invoice` on an enterprise deal and any Checkout session.

Verify with the script in section 4.

## 2. Register the webhook endpoint

Dashboard → **Developers → Webhooks → Add endpoint**.

**URL** — must be reachable from the public internet. `app_base_url` is currently
`https://localhost`, which Stripe cannot reach, so this needs your real hostname:

```
https://<your-public-host>/api/billing/webhooks/stripe
```

**Events** — exactly these six. The code ignores anything else, and subscribing to "all events"
just adds noise to the dedupe table:

- `checkout.session.completed`
- `customer.subscription.created`
- `customer.subscription.updated`
- `customer.subscription.deleted`
- `invoice.paid`
- `invoice.payment_failed`
- `invoice.finalized`

Then copy the endpoint's **signing secret** (`whsec_…`) into `deploy/.env` as
`NEXUS_STRIPE_WEBHOOK_SECRET` and recreate the app.

> A `NEXUS_STRIPE_WEBHOOK_SECRET` is already set, but **no endpoint exists**, so that value came
> from somewhere else (most likely a `stripe listen` session). A secret that does not match the
> endpoint fails signature verification, and a rejected webhook writes **no row** — the only trace
> is `nexus_webhook_events_total{outcome="bad_signature"}` on `/metrics`. Replace it with the
> secret from the endpoint you actually create.

For local testing without a public host:

```bash
stripe listen --forward-to http://localhost:8080/api/billing/webhooks/stripe
```

That prints its own `whsec_…`, which is the one to use while the listener runs.

## 3. Create a billing-portal configuration

Dashboard → **Settings → Billing → Customer portal** → save a configuration.

`POST /billing/portal` fails without one even after charges are enabled. This is a separate step
people miss because the API returns a Stripe-side error rather than a missing-setup message.

## 4. Verify

```bash
python scripts/stripe_status.py
```

Re-run the same read-only checks that produced the table above: account flags, endpoint count,
portal configuration count.

---

## What is NOT blocked by any of this

Plan gating and entitlements do not touch Stripe. A restricted plan hides modules and refuses API
calls whether or not payments work — `NEXUS_BILLING_ENFORCEMENT` is the only switch that matters
there.

## Which plan type can be sold self-serve

This is a design decision, not a gap:

- **Standard plans** (`starter`, `growth`, `professional`, `business`) → self-serve
  **Checkout + Customer Portal**. Edit their modules in Control plane → Plans → entitlements, set
  a module's mode to `disabled`.
- **Per-tenant custom plans** (Control plane → Subscriptions → *Custom plan*) are deliberately
  **admin-managed**: `_reject_if_admin_managed` returns **409** for `plan_class` in
  `("custom", "enterprise")` on both `/billing/checkout` and `/billing/portal`. An enterprise
  customer landing in a self-serve portal that knows nothing about their contract is worse than
  being told to talk to their account team. They are billed through `collect_invoice`.

So: a bespoke "core only" deal for one customer → custom plan. A repeatable "Core" tier on the
price list → edit a standard plan's entitlements.

**There is no endpoint to create a brand-new sellable plan.** `PATCH /admin/billing/plans/{id}`
edits the eight seeded plans and `POST /admin/billing/tenants/{id}/custom-plan` creates per-tenant
ones; adding a ninth public tier still needs a `nexus/billing/plans.py` change and a deploy.
