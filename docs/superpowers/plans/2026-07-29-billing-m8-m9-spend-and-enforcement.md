# Billing M8 + M9 — Credit Spend, Payments, and the Dead Enforcement Fields

**Status:** design + task list. Implemented directly (no subagent), TDD per task.

**Why these two together:** both close the gap between *stored configuration* and *evaluated
behaviour*. Today the platform records prices, credits, burst limits, dependencies and seat
counts — and acts on none of them. A plan can say "60 requests/min, requires module.network,
5 seats" and every one of those is decoration.

**Run tests with `PYTEST_XDIST_WORKER=m8 py -3.10 -m pytest`.**

**Enforcement stays `shadow`.** Every behaviour below is evaluated and recorded; none of it
blocks until Admin flips a capability. That is what makes this safe to ship.

---

## M8 — Credits are actually spent

### The defect

`burn_credits()` is implemented, tested, and called by nothing. `grant_credits()` runs on every
period roll. So a balance only ever grows: the ledger is an accounting fiction.

### The burn order (docs/billing/04-Pricing-Engine.md §2)

Per metered action, in order:

1. **Inside the plan's included quota** → allow, burn nothing. Quota is what they already paid for.
2. **Beyond quota, credits available** → burn `units_over × price`, allow.
3. **Beyond quota, no credits, plan sets `overage_price_credits`** → allow and invoice it. An
   overage price means "keep going and charge for it", not "stop".
4. **Beyond quota, no credits, no overage price** → block (`quota_exhausted`).

Step 2 is new. It is strictly *more permissive* than today's behaviour, so it cannot regress an
existing tenant into being blocked.

### Price resolution

`entitlement.overage_price_credits` when set, else the rate card's `credits_per_unit`. Same
precedence as the invoice rating path (M4), so what is burned in-flight and what is billed at
period close agree.

### Idempotency

The burn key is derived from the usage event's idempotency key (`f"{key}:burn"`). A retried
request that no-ops on the usage insert must also no-op on the burn, or a retry storm silently
drains a balance.

### Failure posture

Burning is inside `check_and_meter`, which must never raise. A burn failure logs and allows —
the same fail-open rule as the rest of the seam. Losing revenue on one action is strictly better
than breaking the product.

### Task 1 — burn on overage

**Files:** `nexus/billing/entitlements.py`; test `tests/test_billing_burn.py`

Tests:
- inside quota → balance unchanged
- beyond quota with balance → burns `over × price`, action allowed
- beyond quota, insufficient balance, no overage price → blocked when enforcing, balance unchanged
- beyond quota, insufficient balance, overage price set → allowed, balance unchanged (invoiced)
- retry with the same idempotency key → burns exactly once
- entitlement `overage_price_credits` wins over the rate card
- a raising burn degrades to allow (fail-open)

### Task 2 — payment provider seam

**Files:** `nexus/billing/payments.py`, `nexus/core/config.py`; test `tests/test_billing_payments.py`

Mirrors every other provider seam in this repo (LLM, search, CRM, telephony, network): interface
+ offline default + real adapter + registry with a process-wide test override.

```python
class PaymentProvider(abc.ABC):
    async def ensure_customer(self, *, tenant_id, email, name) -> str: ...
    async def create_checkout(self, *, tenant_id, plan_id, amount_cents, currency) -> dict: ...
    async def charge(self, *, customer_id, amount_cents, currency, idempotency_key) -> dict: ...
    async def refund(self, *, charge_id, amount_cents, idempotency_key) -> dict: ...
```

- `NoopPaymentProvider` — the default. Records the intent and returns a synthetic id. Lets the
  whole subscription lifecycle be exercised offline, which is what
  [16-Testing-Strategy](../../billing/16-Testing-Strategy.md) §2 asks for.
- `StripePaymentProvider` — real calls, **inert until `NEXUS_STRIPE_SECRET_KEY` is set**. An
  unconfigured provider raises a clear error rather than silently pretending, matching the
  `provider_configured` convention used by the network connectors.
- `NEXUS_PAYMENT_PROVIDER=noop|stripe`, default `noop`.

No live keys are available, so Stripe ships written-and-inert. Webhooks and dunning are
explicitly deferred: a webhook endpoint that has never received a signed event is not something
to claim as done.

---

## M9 — The dead configuration fields

### Task 3 — dependency gating

`BillingCapability.depends_on` holds module gates (`["module.network"]`) and is never read. A
plan that omits `module.network` still lets the tenant use the whole Network subsystem.

In `resolve_entitlement`: after resolving, if any dependency resolves to `disabled`, return the
capability as `disabled` with `source="dependency"`. Cycles are impossible (the seed is a flat
two-level structure) but the walk is depth-limited anyway rather than trusting that.

Tests: dependency satisfied → unchanged; dependency disabled → capability disabled with reason
`dependency`; unknown dependency → ignored (unknown means allow, per the regression-proof rule);
depth guard terminates.

### Task 4 — burst limits

`burst_limit`, `rate_limit` and `cooldown_s` are stored; `BillingThrottled` is defined and never
raised.

Evaluate `burst_limit` as events for this capability in the trailing 60 seconds, using the
existing `ix_usage_tenant_cap_time` index. Over the limit → `BillingThrottled` (HTTP 429 with
`Retry-After`). Only evaluated when `burst_limit` is set, so it costs one indexed count on the
capabilities that opt in and nothing everywhere else.

Tests: under limit → allowed; over limit → throttled when enforcing; over limit in shadow →
allowed with `would_block`; no `burst_limit` → no query issued.

### Task 5 — seats as a gauge

`seat.member` has entitlements in five plans and nothing counts seats. It is a **gauge**, not a
counter: the question is "how many members exist right now", not "how many were added this
period". `current_usage` special-cases gauge capabilities to count live rows instead of summing
usage events.

Tests: seat usage equals active membership count; removing a member lowers it (a counter never
would); over-seat invite blocked when enforcing.

### Task 6 — admin audit log

[17-Production-Checklist](../../billing/17-Production-Checklist.md) §Admin requires 100% of admin
mutations captured. Today a platform admin can reprice a plan with no record of who or when.

New platform-global table (migration `0025_billing_audit`), deliberately no `tenant_id` and no
RLS — it records actions *across* tenants and must not be readable through a tenant session:

| column | purpose |
|---|---|
| `actor` | admin email from the platform-admin principal |
| `action` | `plan.update`, `rate.upsert`, `credits.grant`, `subscription.change` |
| `target` | the id acted on |
| `tenant_id` | nullable — the tenant affected, when there is one |
| `before` / `after` | JSON snapshots, so a dispute can be reconstructed |
| `created_at` | when |

Wired into all five write endpoints. Recording failure must not fail the mutation, but it is
logged at ERROR: an unaudited admin write is a real problem, not a shrug.

Tests: each mutation writes exactly one row with actor and before/after; the log is not reachable
through a tenant session; a recording failure does not roll back the mutation.

---

## Gate

- `PYTEST_XDIST_WORKER=m8 py -3.10 -m pytest tests/ -k billing -q`
- `py -3.10 -m ruff check nexus/ tests/ migrations/`
- single alembic head; chain still replays (`tests/test_migrations_replay.py`)
- full suite green
- redeploy + verify `0025` applied, audit table present with no RLS policy

## Deliberately out of scope

Stripe webhooks and dunning (needs live keys and a signed-event replay to be worth claiming),
seat-day proration, credit expiry sweep, coupons and price books, enterprise contracts. Each is
listed in [14-Implementation-Roadmap](../../billing/14-Implementation-Roadmap.md) phases 6–9.
