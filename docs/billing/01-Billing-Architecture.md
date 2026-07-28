# 01 — Billing Architecture

> The commercial operating system for NEXUS GTM: one metadata-driven platform through which
> every feature, API call, AI action, workflow, and resource becomes billable **without
> application code changes**. Plans configure; code never branches on plan.
>
> Related: [02-Entitlement-Engine](02-Entitlement-Engine.md) ·
> [03-Metering-Architecture](03-Metering-Architecture.md) ·
> [04-Pricing-Engine](04-Pricing-Engine.md) · [05-Subscription-System](05-Subscription-System.md) ·
> [15-Migration-Strategy](15-Migration-Strategy.md)

## 1. Design principles

1. **Catalog, not code.** Every monetizable thing is a row in `billing_capabilities`. Application
   code references a capability by stable ID (`ai.email_draft`, `search.icp_discovery`) and asks
   one engine "may I, and record that I did." Nothing else.
2. **One enforcement seam.** A single `check_and_meter()` call is the only place entitlements are
   evaluated and usage is recorded. It is exposed three ways (FastAPI dependency, worker
   decorator, ASGI middleware) but is one code path — mirroring how `TenantSession` is the single
   tenancy seam today.
3. **Default-allow, shadow-first.** An unregistered capability is allowed and merely logged.
   Existing behavior cannot regress by omission ([15-Migration-Strategy](15-Migration-Strategy.md)).
   Enforcement is flipped per-capability from Admin, never by deploy.
4. **Event-sourced usage.** Usage is an append-only event stream (`billing_usage_events`) with
   idempotency keys (reusing `nexus/core/idempotency.py`), rolled up asynchronously. Invoices are
   derived state; the event log is truth.
5. **PSP-agnostic.** Invoicing/rating is internal. Payment collection goes through a
   `PaymentProvider` seam (`stripe` reference adapter, `noop` for offline/test) exactly like the
   codebase's existing LLM/search/CRM/telephony provider seams.
6. **Multi-tenant + RLS by construction.** All tenant-owned billing tables are `TenantScoped`;
   `scripts/apply_rls.py` picks them up automatically (it derives the table list from
   `Base.metadata`). Catalog/plan/price tables are platform-global and admin-only.
7. **Offline-testable.** The whole platform runs in CI with SQLite + in-memory queue + `noop`
   PSP, per the repo's zero-network test rule.

## 2. System overview

```
                         ┌─────────────────────────────────────────────┐
                         │             ADMIN PORTAL (/admin)           │
                         │  catalog · plans · price books · contracts  │
                         │  coupons · credits · flags · dashboards     │
                         └───────────────┬─────────────────────────────┘
                                         │ writes (audited)
        ┌────────────────────────────────▼─────────────────────────────────┐
        │                      CONFIGURATION PLANE (Postgres)              │
        │  billing_capabilities · billing_plans · billing_plan_entitlements│
        │  billing_price_books · billing_rate_cards · billing_contracts    │
        │  billing_coupons · billing_cost_rates                            │
        └───────┬──────────────────────────────────────────────┬──────────┘
                │ resolved + cached (Valkey, version-keyed)     │ rating input
┌───────────────▼───────────────┐              ┌────────────────▼───────────────┐
│      ENTITLEMENT ENGINE       │              │         PRICING ENGINE         │
│ check(tenant, capability, qty)│              │ rate(usage) → invoice lines     │
│ → allow / soft / block / trial│              │ credits · overage · discounts   │
└───────────────┬───────────────┘              └────────────────┬───────────────┘
                │ allow ⇒ meter                                  │
┌───────────────▼──────────────────────────────────────────────▼───────────────┐
│                            METERING ENGINE                                    │
│  EventBus "usage.recorded" → queue job → billing_usage_events (append-only)   │
│  Valkey hot counters → billing_usage_rollups (hour/day) → invoice lines       │
└───────────────┬───────────────────────────────────────────────────────────────┘
                │ period close
┌───────────────▼───────────────┐   ┌───────────────────────────────────────────┐
│     SUBSCRIPTION ENGINE       │──▶│  INVOICING → PaymentProvider (stripe|noop)│
│ lifecycle · periods · proration│  │  dunning · receipts · refunds             │
└───────────────────────────────┘   └───────────────────────────────────────────┘
```

## 3. New package layout (implementation phase; see [14-Implementation-Roadmap](14-Implementation-Roadmap.md))

```
nexus/billing/
  catalog.py        # capability registry access + seed loader
  entitlements.py   # check_and_meter(); resolution + Valkey cache
  metering.py       # usage event emit/ingest, rollups
  pricing.py        # rating: usage × rate card × discounts → lines
  credits.py        # credit ledger (grant/burn/expire), balance
  subscriptions.py  # lifecycle state machine, period close
  invoicing.py      # invoice assembly, numbering, tax hooks
  payments/         # provider seam: base.py, stripe.py, noop.py, registry.py
  costs.py          # unit-cost table access; margin computation
  api/              # /admin/billing/* and /billing/* routers
nexus/models/billing.py
```

Follows every house convention: provider registry with test override (like
`network/connectors/registry.py`), lazy imports in worker handlers, `NEXUS_BILLING_*` settings
that are inert until configured.

## 4. Data model (summary — details in the per-engine docs)

**Platform-global (admin-managed, no tenant_id):**

| Table | Purpose |
|---|---|
| `billing_capabilities` | The catalog: every billable thing (see [08](08-Feature-Catalog.md)) |
| `billing_plans` | Plan definitions incl. class (free…custom), status, version |
| `billing_plan_entitlements` | plan × capability → limits/pricing overrides |
| `billing_price_books` | currency/region price sets per plan |
| `billing_rate_cards` | per-capability unit pricing + volume tiers |
| `billing_coupons` | promos: %/amount/credits, windows, redemption caps |
| `billing_cost_rates` | unit COGS per capability ([12-Cost-Analysis](12-Cost-Analysis.md)) |
| `platform_admins` | staff access to /admin (separate from tenant RBAC) |

**Tenant-scoped (TenantScoped ⇒ automatic RLS):**

| Table | Purpose |
|---|---|
| `billing_subscriptions` | tenant ↔ plan, periods, status, trial, grandfather flags |
| `billing_contracts` | enterprise overrides: bundle JSON, custom rates, SLA (see [07](07-Enterprise-Licensing.md)) |
| `billing_credit_ledger` | append-only credit grants/burns/expiries |
| `billing_usage_events` | append-only metering stream (partition by month) |
| `billing_usage_rollups` | hour/day aggregates per capability |
| `billing_invoices` / `billing_invoice_lines` | derived, immutable once finalized |
| `billing_payments` | PSP transactions, refunds |
| `billing_audit_log` | every admin/billing mutation, actor + before/after |

## 5. The single enforcement seam

```python
# The ONLY billing touchpoint application code ever sees.
result = await billing.check_and_meter(
    ts,                          # TenantSession — tenant attribution for free
    capability="ai.email_draft", # catalog ID; unregistered ⇒ allow+log (shadow)
    quantity=1,
    idempotency_key=f"draft:{run_id}:{step}",   # replay-safe (core/idempotency.py)
    attrs={"tokens_in": 1512, "tokens_out": 402, "model": "llama-3.3-70b"},
)
if result.blocked:
    raise QuotaExceeded(result)  # → HTTP 402 with upgrade context
```

Exposure points (all delegate to the same function):
- **FastAPI dependency** `Depends(Metered("search.network"))` for endpoints;
- **worker decorator** `@metered("job.icp_discovery")` for queue handlers;
- **ASGI middleware** for blanket `api.request` metering (extends the existing
  `RequestContextMiddleware`), giving "bill every API" without touching routers.

Cost attributes (`tokens_in/out`, `provider`) ride along so [12-Cost-Analysis](12-Cost-Analysis.md)
can compute per-event COGS. The LLM layer (`nexus/agents/llm.py`) already centralizes completions —
one emit point covers every AI feature.

## 6. Failure and consistency posture

- **Metering must never take the product down.** Event emission is fire-and-forget onto the
  existing EventBus/queue; if Valkey is unavailable the event still appends to Postgres in the
  request transaction's `after_commit` hook. Counters are eventually consistent; **hard blocks
  use the Postgres counter, soft warnings may use the cache**.
- **Enforcement fail-open by default** (configurable per capability to fail-closed for
  Enterprise-only gates). An entitlement-engine outage degrades to shadow mode, never a 500.
- **Idempotency everywhere:** usage events, credit burns, invoice finalization, and PSP webhooks
  all carry idempotency keys through the existing `nexus/core/idempotency.py` machinery.
- **Auditability:** finalized invoices immutable; corrections are credit notes; every admin
  mutation writes `billing_audit_log`.

## 7. What is explicitly out of scope for v1

Tax calculation (hook interface only; Stripe Tax/Avalara later), payment-method vaulting (PSP
owns it), marketplace resale, and per-button frontend telemetry billing (metered server-side
instead; see [10-Usage-Tracking](10-Usage-Tracking.md) §"click-level telemetry").
