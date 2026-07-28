# 06 — Admin Portal

> The staff-facing control plane: run the entire commercial platform — and the operational
> platform around it — without touching code.
>
> Related: [02-Entitlement-Engine](02-Entitlement-Engine.md) ·
> [04-Pricing-Engine](04-Pricing-Engine.md) · [11-Profitability-Analysis](11-Profitability-Analysis.md)

## 1. Access model

- **New scope, not new roles inside tenants.** `platform_admins` table (user_id, platform_role,
  mfa_enforced) — completely separate from tenant RBAC (owner/admin/manager/rep). A staff member
  may hold zero tenant memberships.
- Platform roles: `super` (everything), `finance` (plans/pricing/invoices/refunds),
  `support` (read + credits/comps within caps), `ops` (workers/queues/health, no money),
  `sales` (contract builder, draft-only until finance approves).
- Every mutation writes `billing_audit_log` (actor, action, entity, before/after JSON, reason).
  Sensitive actions (refund > cap, margin exception, plan retire) require a typed reason.
- Served under `/admin` (API prefix `/api/admin/...`), same FastAPI app, distinct router set with
  a `require_platform(role)` dependency; SPA gets an `/admin` route section visible only to
  platform admins.

## 2. Portal map (v1 scope → later)

| Area | Capabilities | Phase |
|---|---|---|
| **Organizations** | search tenants; plan/status/usage/MRR at a glance; impersonate-read (audited, no writes); suspend/restore | 1 |
| **Users** | cross-tenant user lookup, lockout/reset, seat audit | 1 |
| **Plans & Pricing** | CRUD plans, entitlement matrix editor (plan × capability grid), rate cards w/ margin preview, price books, coupons; draft → activate w/ margin validation | 1 |
| **Catalog** | register/edit capabilities, default modes, units, cost-rate binding; shadow→enforced flip per capability | 1 |
| **Subscriptions** | assign/override plan per tenant, start/extend trials, cancel, comp credits (capped by role) | 1 |
| **Contracts** | enterprise bundle builder ([07](07-Enterprise-Licensing.md)) | 2 |
| **Credits** | ledger view, grant/adjust with reason, expiry policy | 1 |
| **Usage** | per-tenant/per-capability explorer, event drill-down, export | 1 |
| **Invoices & Payments** | list, finalize/void, credit notes, refunds (PSP), dunning status | 2 |
| **Feature Flags** | global + per-tenant flags (entitlement `feature_flag` field) | 1 |
| **Rate Limits** | per-plan burst/rate/cooldown editing (entitlement fields) | 1 |
| **API Keys** | (future public API) issue/revoke tenant API keys, per-key metering | 3 |
| **AI Providers** | current provider chain + keys status (masked), model routing, per-model cost rates | 2 |
| **Ops: Workers/Queues** | heartbeat health, queue depth, last job outcomes (reads the worker's own logs/metrics), pause tenant automation | 2 |
| **Ops: Crawlers/Signals** | source status (news/RSS), per-tenant signal volumes | 2 |
| **Monitoring & Health** | surface existing Prometheus/Grafana ([deploy/monitoring](../../deploy/monitoring)) links + key SLIs inline | 2 |
| **Audit Logs** | full billing_audit_log search | 1 |
| **Support** | tenant context page: plan, usage, denials, recent errors — the CS cockpit | 2 |
| **Security** | platform-admin management, MFA enforcement, session revocation | 1 |

## 3. Executive dashboards (reads rollups + subscriptions + cost rates)

| Dashboard | Contents |
|---|---|
| **Revenue** | MRR/ARR (by plan, by cohort), new/expansion/contraction/churn waterfall, trial conversion |
| **Cost** | COGS by capability/provider/tenant (Σ `unit_cost_usd`), infra baseline allocation |
| **Margin** | gross margin by plan/tenant/capability; exceptions list (below-floor SKUs); trend |
| **Profit** | contribution after support/ops allocation ([11](11-Profitability-Analysis.md) model) |
| **Growth signals** | soft-limit hits, denial events, credit-pack purchases — ranked upgrade pipeline for CS |

All are SQL over `billing_usage_rollups`, `billing_invoices`, `billing_subscriptions`,
`billing_cost_rates` — no external BI dependency for v1; Grafana panels can mirror them via the
existing observability stack.

## 4. Non-negotiables

- Read paths never bypass RLS semantics: admin queries run under an owner-privileged session
  through dedicated audited endpoints, never by reusing tenant tokens.
- No plaintext secrets rendered — provider keys masked (same rule as `email_settings.password`).
- Impersonation is read-only and watermarked in the audit log; support writes happen through
  explicit actions (grant credits, change plan), never "act as user".
