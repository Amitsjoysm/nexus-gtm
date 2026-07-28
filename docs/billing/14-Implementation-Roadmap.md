# 14 — Implementation Roadmap

> Phased delivery. Each phase ships working, tested software behind the shadow/enforcement
> switch; **no phase changes existing product behavior until Admin flips it**
> ([15-Migration-Strategy](15-Migration-Strategy.md)). Follows the house method: spec → plan →
> TDD tasks → two-stage review, all offline-testable.

| Phase | Scope | Key deliverables | Exit criteria |
|---|---|---|---|
| **0. Foundation** | models + migration + seed | `nexus/models/billing.py`, migration `00XX_billing_foundation` (all tables, partitioned usage), catalog seed (58 capabilities, [08](08-Feature-Catalog.md)), `legacy-unlimited` plan auto-assigned to every tenant, `NEXUS_BILLING_ENFORCEMENT=off` | migration applies clean on prod copy; RLS auto-covers new tenant tables; zero behavior change (full suite green) |
| **1. Metering core** | record everything, bill nothing | `check_and_meter()` (shadow-only), EventBus→queue ingestion, idempotent event insert, hourly rollups, LLM chokepoint emit, middleware `api.request` meter, worker decorator on all handlers | 7 days shadow data in prod; events↔rollups reconcile exactly; p95 request overhead < 3ms |
| **2. Entitlement engine** | evaluation + cache | resolution chain, Valkey cache + version bump, all outcomes incl. 402/429 payloads, seat check, dependency graph | enforcement on for **one** canary capability (`data.export`) for internal tenant only; suite green |
| **3. Admin portal v1** | control plane | platform_admins + RBAC, catalog/plans/entitlement-matrix/rate-cards/coupons CRUD, per-capability shadow→enforce flip, usage explorer, audit log | PM launches a complete test plan end-to-end with zero engineering involvement |
| **4. Pricing + credits** | money math | rating engine, credit ledger + burn order, packs, coupons, margin-floor validation, `preview` endpoint | re-rating any period is deterministic; guardrail blocks a <50% card in test |
| **5. Subscriptions + invoicing** | lifecycle | state machine + lifecycle heartbeat job, proration via seat-days, invoice assembly/finalize, `noop` PSP end-to-end | full simulated month: trial→active→past_due→recovery produces correct invoices offline |
| **6. Stripe adapter + tenant billing UI** | go-live surface | `stripe.py` (checkout, webhooks, refunds), Settings→Billing page (plan, meters, packs, invoices), 402 upgrade CTAs in SPA | test-mode Stripe round trip; dunning path verified |
| **7. Dashboards + CS** | revenue ops | MRR/ARR/churn, cost/margin/profit dashboards, upgrade-pipeline + adoption views, anomaly watch | finance signs off numbers against a seeded dataset |
| **8. Enterprise licensing** | sales motion | contract object + builder + finance approval flow, minimum-commit rating, templates | sales builds a custom deal in <10 min without engineering |
| **9. Production hardening** | scale + trust | usage partition retention/archive job, counter reconciliation, load test (extend `deploy/loadtest/`), billing runbooks, alerting rules, invoice reconciliation tool, pen-review of /admin | go-live checklist ([17](17-Production-Checklist.md)) fully green |

**Sequencing rules:** phases 0–1 are pure additive plumbing and can merge continuously; 2+ ride
the per-capability flags; nothing after phase 0 requires schema change (contracts/coupons tables
ship in 0). Estimated engineering shape: each phase = one spec+plan+subagent-executed cycle like
the Network build, individually reviewable by Codex.
