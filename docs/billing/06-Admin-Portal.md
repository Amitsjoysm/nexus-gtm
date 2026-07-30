# 06 — Admin Portal

> The staff-facing control plane: run the entire commercial platform — and the operational
> platform around it — without touching code.
>
> Related: [02-Entitlement-Engine](02-Entitlement-Engine.md) ·
> [04-Pricing-Engine](04-Pricing-Engine.md) · [11-Profitability-Analysis](11-Profitability-Analysis.md)

## 1. Access model

- **New scope, not new roles inside tenants.** `platform_admins` table (email, platform_role,
  permissions) — completely separate from tenant RBAC (owner/admin/manager/rep). A staff member
  may hold zero tenant memberships. Membership also comes from an env allowlist
  (`NEXUS_PLATFORM_ADMIN_EMAILS`), which exists to solve the bootstrap problem and deliberately
  carries full power.
- Every mutation writes `billing_audit_log` (actor, action, entity, before/after JSON, reason).
  Sensitive actions (refund > cap, margin exception, plan retire) require a typed reason.
- Served under `/admin` (API prefix `/api/admin/...`), same FastAPI app, distinct router set; SPA
  gets an `/admin` route section visible only to platform admins.

### 1.1 As built: permissions, not roles

The gate is a **permission**, not a role — `require_platform_permission("pricing.write")`. A role
is only a shortcut for granting a set of permissions, and the **expanded** set is stored on the
row, so redefining a preset later cannot retroactively re-grant power to people provisioned
today. Three presets shipped rather than five: `ops` and `sales` had no endpoints to gate, and a
role that grants nothing is worse than no role, because it reads as working access.

| Permission | Grants | superadmin | finance | support |
|---|---|:-:|:-:|:-:|
| `billing.read` | capabilities, plans, rates, subscriptions | ✓ | ✓ | ✓ |
| `pricing.write` | reprice plans, edit entitlements, set rate cards | ✓ | ✓ | |
| `subscriptions.write` | move a workspace between plans, custom plans | ✓ | ✓ | |
| `credits.grant` | credit grants with no ceiling | ✓ | ✓ | |
| `credits.grant.capped` | goodwill grants up to `NEXUS_BILLING_SUPPORT_CREDIT_CAP` | ✓ | | ✓ |
| `invoices.collect` | charge a finalized invoice | ✓ | ✓ | |
| `jobs.manage` | dead-letter triage and replay | ✓ | | |
| `admins.manage` | grant/revoke platform admins, and see who they are | ✓ | | |
| `users.manage` | reset a user's MFA, account recovery | ✓ | | ✓ |

Four decisions inside that table are load-bearing:

- **Support gets `credits.grant.capped`, not `credits.grant`.** Goodwill credits are the single
  most common support action; forcing an escalation for every one turns the escalation into a
  rubber stamp. A ceiling keeps the blast radius small while leaving the workflow usable. This is
  the one amount-dependent check, so it lives in the handler body rather than a `Depends`.
- **Finance cannot `admins.manage`.** Whoever can grant permissions can grant themselves any
  other permission, so that one is not delegated alongside pricing.
- **`admins.manage` also gates *reading* the admin list.** Who operates the platform is visible
  only to people who can change it.
- **An unknown role degrades to read-only, not to nothing.** Failing closed to no access would
  lock out an admin whose role string was mistyped, with no way to fix it through the product.

An empty `permissions` list falls back to the role preset. That is what made migration `0029` a
pure `add_column` with no backfill: every pre-existing admin kept exactly the access their role
implied, with no window in which a half-run backfill left someone half-granted.

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
