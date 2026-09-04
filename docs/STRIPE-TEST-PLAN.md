# Stripe test plan

How to prove the payment path works, what each check should return, and — just as important —
what **not** to do by hand against a live deployment.

Written for **test mode on a production deployment** (`gtm.infojoy.com`), which is the awkward
case: real infrastructure, real webhooks, fake money. Everything here assumes the Stripe dashboard
toggle is set to **Test**.

---

## The one thing to understand first

**Stripe keeps test and live completely separate.** Different keys, different customers, different
products, different subscriptions — and, most easily missed, **different webhook endpoint lists**.

An endpoint registered while the dashboard was in Live mode is invisible to a `sk_test_` key. That
is not a configuration error you can see; the endpoint simply never fires and checkout completes
into silence. It has already happened once on this deployment.

Whenever something "works in the dashboard but not in the app", check the mode toggle first.

---

## Before you start

| Check | Command | Expected |
|---|---|---|
| Which key is loaded | `docker exec nexus-gtm-app-1 printenv NEXUS_STRIPE_SECRET_KEY \| cut -c1-8` | `sk_test_` |
| Where checkout returns to | `docker exec nexus-gtm-app-1 printenv NEXUS_APP_BASE_URL` | exactly `https://gtm.infojoy.com` |
| Whether the account can charge | `docker exec nexus-gtm-app-1 python /app/scripts/stripe_status.py` | see below |

`NEXUS_APP_BASE_URL` is worth its own line. Stripe's `success_url` is built from it, so a wrong
value redirects the customer to a **different origin** after paying — where the browser has no
session, and the symptom is "I got logged out after subscribing". Nothing validates it at startup.

---

## Phase 1 — Account readiness (read-only, safe)

```bash
docker exec nexus-gtm-app-1 python /app/scripts/stripe_status.py
```

**Target output:**

```
account acct_XXXX  (test mode)
  charges_enabled     YES
  payouts_enabled     YES
  details_submitted   YES

webhook endpoints: 1
billing-portal configurations: 1
```

**Current output on this deployment (2026-09-04):**

```
  charges_enabled     NO
  details_submitted   NO
webhook endpoints: 0
subscriptions       0
```

Three separate fixes, in this order:

1. **Complete account activation.** Dashboard → Test mode → activation form. Test mode accepts
   placeholder business details; it only needs submitting. Re-run the script; both flags flip.
2. **Register the webhook** (Phase 2).
3. Re-run. `subscriptions` stays 0 until Phase 5.

> A `sk_test_` key on an unactivated account will *create* checkout sessions happily and then fail
> at payment. Getting a session URL back is not evidence the account can charge.

---

## Phase 2 — The webhook endpoint

Dashboard → **Developers → Webhooks → Add endpoint**, with the toggle on **Test**.

**URL:** `https://gtm.infojoy.com/api/billing/webhooks/stripe`

**Events** — the app tells you which it handles:

```bash
curl -s https://gtm.infojoy.com/api/admin/runtime/webhook -H "Authorization: Bearer $SUPERADMIN_TOKEN"
```

At minimum:

```
checkout.session.completed
customer.subscription.created
customer.subscription.updated
customer.subscription.deleted
customer.subscription.trial_will_end
invoice.paid
invoice.payment_failed
invoice.finalized
invoice.upcoming
payment_intent.succeeded
payment_intent.payment_failed
charge.refunded
```

Copy the signing secret (`whsec_…`) into `NEXUS_STRIPE_WEBHOOK_SECRET` and restart.

**Verify it registered:** re-run `stripe_status.py` → `webhook endpoints: 1`. If it still says 0,
you registered in Live mode.

---

## Phase 3 — Prove our side without Stripe

This is the highest-value check and costs nothing. It signs realistic events with your configured
secret and POSTs them exactly as Stripe would, including the attacks.

```bash
docker exec nexus-gtm-app-1 python /app/scripts/verify_stripe_webhook.py \
  --url http://localhost:8000 --tenant <throwaway-tenant-id>
```

| Case | Expected |
|---|---|
| Valid signed event | `200`, `applied: true`, tenant resolved |
| Same event id replayed | `200`, `duplicate: true` — **not** an error |
| Bad signature | `400` |
| Timestamp outside the tolerance | `400` |
| No signature header | `400` |
| Body edited after signing | `400` |

All six behaving means signature, freshness, tamper-detection, dedupe, tenant resolution and
status mapping are sound — **only delivery is unproven**. That is a real distinction: it separates
"our code is wrong" from "Stripe cannot reach us", which are fixed in completely different places.

> Use a throwaway tenant. It writes a real subscription row.

The same guarantees are pinned by `pytest tests/test_webhook_forgery.py` (21 cases) so they cannot
regress silently between manual runs.

---

## Phase 4 — Do NOT do these by hand

| Don't | Why |
|---|---|
| **Register the webhook in Live mode "to test it"** | It will not fire for a test key, and you will spend an afternoon debugging the app instead of the toggle. |
| **Edit `billing_subscriptions` directly to "fix" a plan** | Stripe becomes the disagreeing authority. `reconcile.py` will report drift forever and you will not know which side is right. Change the plan through the app or the dashboard, never in SQL. |
| **Insert rows into `billing_credit_ledger` by hand** | The ledger is append-only with idempotency keys, and every balance is `SUM(delta)`. A hand-written row has no key, so a later retry double-grants. Use `POST /admin/billing/tenants/{id}/credits`, which is audited. |
| **Replay a webhook by re-POSTing a captured body** | It will be refused (freshness window) — and if you defeat that, the dedupe table means the effect is a no-op you may misread as a failure. Use the dashboard's own "Resend" button. |
| **Delete a `billing_webhook_events` row to "retry"** | That is the replay guard. Removing it lets the same event apply twice. |
| **Change a rate card while a test transaction is mid-flight** | Each charge uses the rate in force at that instant — correct behaviour, confusing evidence. Finish the test, then reprice. |
| **Test refunds on an account you have not finished onboarding** | Refunds against uncaptured charges fail in ways that look like our bug. |
| **Use a real card, ever, in test mode** | Test mode declines it, but the number reaches a log. Use `4242 4242 4242 4242`. |
| **Point test mode at the production database and then leave it** | Test-mode subscriptions and invoices are indistinguishable from real ones in our tables. Either accept the pollution knowingly, or use a throwaway workspace you delete afterwards. |

---

## Phase 5 — A real end-to-end payment

Buy a plan through the app: **Settings → Billing → choose Launch**, card `4242 4242 4242 4242`,
any future expiry, any CVC, any postcode.

Then confirm **all five**, in order. Stopping at the first is how a half-working integration looks
healthy:

```bash
# 1. Stripe has the subscription
docker exec nexus-gtm-app-1 python /app/scripts/stripe_status.py
#    -> subscriptions   1

# 2. The webhook arrived and verified
docker exec nexus-gtm-postgres-1 psql -U nexus -d nexus -c \
  "select event_type, status, created_at from billing_webhook_events order by created_at desc limit 5;"
#    -> checkout.session.completed, customer.subscription.created, invoice.paid

# 3. Our subscription row moved
docker exec nexus-gtm-postgres-1 psql -U nexus -d nexus -c \
  "select plan_id, status, psp_subscription_id from billing_subscriptions order by updated_at desc limit 3;"
#    -> plan_id='launch', status='active', psp_subscription_id NOT NULL

# 4. The plan's credits were granted
docker exec nexus-gtm-postgres-1 psql -U nexus -d nexus -c \
  "select delta, kind, reason, idempotency_key from billing_credit_ledger order by created_at desc limit 5;"
#    -> +2000, kind='grant', key 'plan_change:launch:<period>'

# 5. The invoice is visible to the customer
docker exec nexus-gtm-postgres-1 psql -U nexus -d nexus -c \
  "select period_key, status, total_cents, meta->>'hosted_invoice_url' from billing_invoices order by created_at desc limit 3;"
#    -> period_key 'stripe:in_...', status='paid', total_cents=9900, a hosted URL
```

Steps 4 and 5 are recent fixes — before them the plan changed, the credits stayed at zero and no
invoice appeared anywhere. If either is empty, the deployment predates commit `41a92e2`.

### The failure that looks like success

Checkout completes, the customer is charged, and `billing_webhook_events` stays **empty**. The
money moved and your database never heard. Check the dashboard's delivery log for the response
code:

- **404** — wrong URL
- **timeout / no attempt** — DNS or firewall; Stripe cannot reach the host
- **400** — the endpoint was reached and the signature failed: wrong `whsec_`, or a proxy is
  rewriting the body

`POST /admin/runtime/webhook/test` proves the secret verifies and the route is live. It
deliberately does **not** prove Stripe can reach you — that depends on DNS and firewalls outside
the process, and claiming otherwise would be the more dangerous answer.

---

## Phase 6 — Lifecycle and edge cases

Each is a dashboard action against the test subscription, then the same five checks.

| Scenario | How | Expected |
|---|---|---|
| **Card declined** | Card `4000 0000 0000 0002` | Checkout refuses. No subscription, no credits, no invoice. |
| **Insufficient funds** | Card `4000 0000 0000 9995` | As above. |
| **3D Secure** | Card `4000 0025 0000 3155` | Authentication prompt; approve → normal success path. |
| **Renewal** | Dashboard → subscription → **Advance clock** (test clocks) | `invoice.paid`, a fresh `plan_grant:<next-period>` |
| **Upgrade mid-period** | Buy Accelerate from Launch | Credits granted under `plan_change:accelerate:<period>`; the Launch grant is **not** re-applied |
| **Downgrade** | Portal → change plan | Plan changes; no new credits (already granted this period) |
| **Cancel at period end** | Portal → cancel | `cancel_at_period_end=true`, status stays `active` |
| **Immediate cancel** | Dashboard → cancel now | `customer.subscription.deleted` → status `canceled` |
| **Failed renewal** | Attach card `4000 0000 0000 0341`, advance clock | Invoice stays `finalized`, `payment_failed_at` set, dunning takes over |
| **Refund** | Dashboard → payment → Refund | `refunded_cents` on the invoice; `net_paid_cents` drops |

**Do not test cancellation on a workspace you still need.** Immediate cancel sets status
`canceled`, and entitlements resolve from the subscription row.

---

## Phase 7 — Fraud and abuse

Already covered by automated tests; listed so you know they exist rather than needing to be redone
by hand:

```bash
pytest tests/test_billing_adversarial.py          # 16 cases
pytest tests/test_webhook_forgery.py              # 21 cases
pytest tests/test_rate_card_conformance.py        # every priced capability
docker exec nexus-gtm-app-1 python /app/scripts/verify_credit_concurrency.py
docker exec -e NO_LOCK=1 nexus-gtm-app-1 python /app/scripts/verify_credit_concurrency.py   # CONTROL: must FAIL
```

Covered: negative / zero / NaN / infinite quantities; a quantity 1000× the cap; spending past zero;
ten replays of one call; one tenant's idempotency key against another's charge; replayed grants
and retried upgrades; cross-tenant isolation; a body edited after signing; a signature from
another secret; an unconfigured secret; secret rotation; stale and future timestamps; and genuine
concurrent double-spend against real Postgres.

**Run the concurrency control too.** With the lock disabled, 20 credits buy 38 credits of product.
A passing run alone proves nothing.

---

## Going live

1. Swap `NEXUS_STRIPE_SECRET_KEY` to `sk_live_`.
2. **Register a second webhook endpoint in Live mode.** Test endpoints do not carry over.
3. Put that endpoint's own `whsec_` into `NEXUS_STRIPE_WEBHOOK_SECRET` — it differs from the test one.
4. Re-run Phases 1 and 3 against live.
5. Run one real transaction with a real card and refund it.
6. Schedule `nexus/billing/reconcile.py` weekly. It **reports** drift between Stripe and our
   database and deliberately never repairs it: which side is right depends on what the customer
   agreed, and an automated writer would resolve that wrongly and destroy the evidence.

Delete the test-mode subscriptions and workspaces from the production database before launch, or
they will appear in revenue reporting as real customers.
