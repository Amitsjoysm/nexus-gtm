# 16 — Testing Strategy

> Billing is the one subsystem where a bug is a refund, a churned customer, or a compliance
> incident. Test posture: everything offline-deterministic (house rule), money math
> property-tested, and a full simulated billing month in CI.
>
> Related: [14-Implementation-Roadmap](14-Implementation-Roadmap.md) ·
> [17-Production-Checklist](17-Production-Checklist.md)

## 1. Layers

| Layer | What | How (offline) |
|---|---|---|
| Unit — pricing math | tier pricing, proration (seat-days), credit burn order, coupon application, margin guardrail | pure functions; **property tests**: rating is deterministic (same inputs ⇒ same lines), Σ lines = invoice total, no negative balances, re-rating idempotent |
| Unit — entitlements | resolution precedence (contract>plan>class>default), every outcome (allow/warn/block/throttle), reset boundary math, dependency graph | table-driven fixtures |
| Unit — metering | idempotent event insert (duplicate key ⇒ 1 row), rollup watermark correctness, compensating events | SQLite + in-memory queue |
| Integration — the seam | endpoint with `Metered(...)` under each outcome → 200/402/429 with correct payloads; worker `@metered` retry ⇒ single event | existing HTTP test client pattern |
| Integration — lifecycle | simulated month: subscribe→use→soft-warn→overage→invoice→pay(noop)→fail→dunning→suspend→recover; upgrade/downgrade proration | time injected via `now_iso` payloads (existing worker-test pattern) |
| PSP adapter | Stripe webhook signature verify, event idempotency, state-machine advancement | recorded fixtures through `httpx.MockTransport` (same as the OAuth connector tests) |
| Shadow-safety regression | **entire existing suite must pass with billing deployed in shadow mode** — the no-behavior-change guarantee is CI-enforced, plus canary asserts: unregistered capability ⇒ allow; engine exception ⇒ allow+log | full-suite gate |
| Reconciliation | events ↔ rollups ↔ invoice lines round-trip on a seeded 10k-event dataset; Valkey counter drift self-heal | dedicated test dataset |
| Load (pre-go-live) | metering overhead on hot endpoints (<3ms p95), rollup job at 1M events, entitlement cache under churn | extend `deploy/loadtest/` k6 scripts |
| Security | /admin RBAC matrix (each platform role × each router), tenant token cannot touch /admin, RLS on all billing tables (extend the existing tenancy tests), audit-log completeness | API tests |

## 2. Financial correctness gates (CI-blocking)

1. `test_invoice_replay`: re-rating any closed period reproduces identical lines.
2. `test_no_double_billing`: N duplicate deliveries of every event type ⇒ 1 charge.
3. `test_margin_floor`: seeding a rate below 50% margin is rejected without `margin_exception`.
4. `test_grandfather_frozen`: plan mutation never alters a `grandfathered` subscriber's terms.
5. `test_suspend_never_deletes`: suspended tenant retains reads + data.

## 3. Staging rehearsal (per release)

Restore prod snapshot (existing `scripts/restore_db.sh` / DR rehearsal flow) → apply migration →
run shadow for 24h → diff shadow COGS vs. provider dashboards (±10%) → flip one capability →
verify 402 UX → flip back. Documented as a runbook next to the existing DR runbooks.
