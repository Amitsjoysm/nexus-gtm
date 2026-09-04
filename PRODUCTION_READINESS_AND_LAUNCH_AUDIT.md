# Production Readiness and Launch Audit

**System:** Nexus GTM — multi-tenant B2B revenue-intelligence SaaS
**Commit:** `44ea3d8` · **Date:** 4 September 2026 · **Suite:** 2,847 passing
**Auditor posture:** independent third party. Nothing below is accepted on the basis of
documentation, configuration, or a test file existing.

---

## 1. Executive Summary

**Decision: NO-GO for a launch carrying an SLA.**

Three mandatory gates were exercised for real during this audit. One passed, one failed, and one
found a privilege-escalation vulnerability that was live in the deployment at the time of testing.

| Gate | Result | Evidence |
|---|---|---|
| **Load (production path)** | **FAIL** | All three SLO thresholds breached at 200 VUs |
| **Penetration (authz/business logic)** | **1 × P0 found, now fixed** | Any customer could buy the staff plan |
| **Disaster recovery** | **PASS** | Real backup → destruction → restore, RTO 10s |

The blocker is not subtle and is not a matter of tuning. **The deployment is configured to demand
~175 database connections from a Postgres that permits 100.** Under a realistic read workload the
connection pool exhausted, and 4.81% of authenticated requests returned HTTP 500 with a p95 latency
of 2.66 s against a 500 ms objective. This is arithmetic, not a load characteristic — it will
reproduce on any deployment with these defaults.

The security finding was worse in kind if not in blast radius: an ordinary authenticated customer
could POST `/api/billing/checkout` with `plan_id: "internal"` — the $0 staff tier, whose plan class
bypasses every quota and every credit burn — and receive a real Stripe Checkout session. Fixed and
verified during this audit (`97d83ee`), but its existence tells you the checkout guard was never
tested against the plan catalogue.

**What is genuinely strong:** tenant isolation held under direct probing; webhook forgery,
replay, tampering and staleness were all rejected; credentials are not exposed in any response;
security headers are present; containers run as non-root; and DR restored cleanly with verified
data integrity.

---

## 2. Application Overview

FastAPI + async SQLAlchemy backend, React/TypeScript SPA, PostgreSQL 16, Valkey, Caddy TLS
termination, one background worker. Multi-tenant with two isolation layers: an application-level
`TenantSession` guard and Postgres row-level security. RBAC is `owner > admin > manager > rep`,
with a separate per-permission platform-admin model for staff.

Commercial model: credits. Every metered action burns `credits_per_unit × quantity` from a
tenant balance at the moment of use. Stripe handles hosted checkout and subscription invoices.

**Scale of the codebase:** 708 files, 7,442 symbols, 66,752 edges (code-review-graph, rebuilt
for this audit).

---

## 3. Audit Scope and Method

**Exercised for real against the running deployment:** load testing, authorization probing,
webhook forgery, DR rehearsal, concurrent double-spend, live billing flows, the Stripe account
state, and the full automated suite.

**Read but not exercised:** cloud IAM, network boundaries, CDN, DNS, certificate lifecycle,
mobile clients (none exist), video delivery (not applicable).

**NOT TESTED and explicitly not claimed:** external network penetration testing, TLS
configuration analysis, fuzzing, XSS/CSRF against the SPA, chaos engineering, region failover,
multi-region DR, soak testing beyond 2 minutes, and any test at production data volume.

### Evidence status model

| Status | Meaning |
|---|---|
| PASS | Implemented and demonstrated |
| PASS WITH RISK | Works, with a documented limitation |
| PARTIAL | Exists with meaningful gaps |
| FAIL | Does not meet the requirement |
| NOT TESTED | May exist; not validated here |
| NOT IMPLEMENTED | Absent |

---

## 4. Evidence Inventory

| Evidence | Source | Status |
|---|---|---|
| Load test | `deploy/loadtest/load.js` via k6, 200 VUs, 2m | Executed |
| Connection arithmetic | `nexus/core/config.py`, `show max_connections` | Measured |
| DR rehearsal | `scripts/dr_rehearsal.sh` | Executed |
| Authorization probe | `.audit/pentest_probe.sh`, 30 cases | Executed |
| Concurrency probe | `scripts/verify_credit_concurrency.py` + control | Executed |
| Webhook forgery | `tests/test_webhook_forgery.py` (21) + live | Executed |
| Billing conformance | `tests/test_rate_card_conformance.py` | Executed |
| Adversarial billing | `tests/test_billing_adversarial.py` (16) | Executed |
| Stripe account state | `scripts/stripe_status.py` | Executed |
| Alert rules | `deploy/monitoring/alerts.yml` | Read only — stack not running |
| Backup schedule | `scripts/backup_cron.sh` | Read only — **no cron installed** |
| CI gates | `azure-pipelines-ci.yml` | Read only |

---

## 5. Load Testing — **FAIL**

### Workload model

The k6 scenario replays a sales rep's daily read path: authenticate once, then repeatedly
`GET /api/accounts`, `/api/signals`, `/api/inbox`, `/api/analytics/overview`, with a 1 s pause
modelling human think-time. Ramp 0 → 50 → 200 VUs over 2 minutes.

**Assumption, stated:** 200 concurrent readers approximates a few thousand daily-active reps.
This was not derived from a business traffic model, because no DAU or peak-concurrency figures
exist for this product. **That is itself a gap** — see P1-4.

### Results

```
http_reqs .............. 18,211      149.5/s
http_req_duration ...... avg 764.95ms   med 261.54ms
                         p(90) 1.82s     p(95) 2.66s     max 30.71s
http_req_failed ........ 4.81%       (876 of 18,211)
checks_succeeded ....... 95.18%
```

| SLO | Target | First run | After the pool fix | Verdict |
|---|---|---|---|---|
| Error rate | < 1% | **4.81%** | **0.05%** | **PASS** |
| p95 latency | < 500 ms | 2,660 ms | **3,090 ms** | **FAIL** (6.2×) |
| p99 latency | < 1,500 ms | > 2,660 ms | > 3,090 ms | FAIL |

### Re-run after remediation (`44ea3d8`)

The pool was resized (max_connections 100 → 300, pool 16, overflow 5) and the identical test
re-run. **Errors fell from 876 to 10 — a 96× improvement, and comfortably inside the objective.**
P0-1 is resolved and the application now logs `130 steady / 234 during a rollout, of 300` at boot.

**Latency did not improve, and the cause is now measured rather than inferred.** Sampling
container CPU mid-run:

```
nexus-gtm-app-1        224% CPU
nexus-gtm-app-2        223% CPU      ~450% of 800% available
nexus-gtm-postgres-1     0.04% CPU   1 active query of 60 connections
```

**The remaining bottleneck is application CPU, not the database and not the pool.** Postgres is
idle. Measured capacity is roughly **37 requests/second per uvicorn worker**, and the cost is in
the request path itself — Pydantic validation, ORM hydration, the per-request RLS `SET LOCAL` —
not in query execution. That is a different finding requiring a different fix (P0-5), and it is
recorded as one rather than folded into the pool result.

### Root cause — reproducible, not probabilistic

```
sqlalchemy.exc.TimeoutError: QueuePool limit of size 10 overflow 20 reached,
connection timed out, timeout 30.00
```

371 HTTP 500s spread across every authenticated endpoint. The arithmetic:

```
per process            35   (db_pool 10 + overflow 20, platform pool 2 + 3)
app processes           4   (2 replicas × 2 uvicorn workers)
worker processes        1
                      ----
connections demanded  175
postgres max_connections 100        →  1.8× overcommit
```

Two failures are stacked here. Requests queue on an exhausted pool for 30 s and then 500 — that
is the observed one. Beneath it, the configuration cannot be satisfied even if the pool were
raised: four app processes at full pool would exhaust Postgres itself, taking the worker down with
them.

**A second interaction, measured separately.** The tenant credit advisory lock is held for a whole
transaction, so a burst from a *single* tenant serialises while every waiter holds a connection.
At 60 parallel callers against one tenant, 30 died on a 30 s checkout timeout. Per-tenant
concurrency is therefore bounded by the pool, not by the lock.

### Coverage honesty

**What was exercised:** app → SQLAlchemy → Postgres → Valkey, through the real HTTP API with real
authentication and real RBAC.

**What was NOT:** Caddy, TLS termination, and the SPA asset path. Containers on this Docker
Desktop host cannot reach the host's published ports, so k6 addressed `http://app:8000` directly.
**Roughly 75% of the request path was exercised.** TLS handshake cost and Caddy's own limits are
`NOT TESTED`.

**Also NOT TESTED:** stress beyond 200 VUs, spike, soak, recovery-after-outage, capacity ceiling,
autoscaling behaviour (there is none configured), and cost under load.

### The latency number is not trustworthy, and the error-rate number is

This matters for how the two results should be read. The Docker host has **8 CPUs and 3.95 GB of
RAM**, and it ran Postgres, two app replicas, the worker *and* the k6 generator simultaneously. At
idle, before any load, Postgres sat at 56% CPU and the worker at 44%.

The **error-rate** result is sound regardless: pool exhaustion was a configuration defect, proven
by arithmetic and fixed deterministically. The **latency** result is partly the test rig measuring
itself, and a credible p95 requires an environment where the generator is external and the app
tier is sized. It is reported as FAIL because no evidence exists that it passes — not because
3.09 s is a trustworthy production figure.

---

## 6. Penetration Testing — **1 × P0 found and fixed**

30 authenticated authorization cases against the running deployment.

### P0 — Privilege escalation through the billing surface

An ordinary authenticated customer POSTed `/api/billing/checkout` with `plan_id: "internal"` and
received a real Stripe Checkout session (`cs_test_b1Hopm…`).

`internal` is the staff tier: `base_price_cents = 0`, and its class sits in
`entitlements._UNLIMITED_CLASSES`, so `resolve_entitlement` returns `mode="unlimited"` and every
quota check and credit burn is skipped. Completing that $0 checkout puts a customer on unlimited
usage permanently, through the front door. `legacy-unlimited` and `trial` were exposed identically.

`UNPURCHASABLE_PLAN_CLASSES` contained `("free",)` alone. The reasoning recorded against that one
entry applies verbatim to the four that were missing, which makes this an omission rather than a
judgement call — and the price list already excluded them, so the till and the catalogue disagreed
about what was for sale.

**Fixed in `97d83ee`.** Verified live: `internal`, `legacy-unlimited`, `trial`, `free` → 409;
`launch` → 200. The regression test binds the guard to `_UNLIMITED_CLASSES` itself, so a fifth
unlimited class cannot open a fifth way to buy unlimited usage, and a second test fails on **any**
active $0 plan that becomes purchasable.

### Passed

| Class | Result |
|---|---|
| Unauthenticated access to authenticated surface | 403 on all |
| Vertical escalation to platform admin | 404 on all — the admin surface is hidden, not merely refused, which is the stronger posture (verified: 200 with a superadmin token) |
| **Horizontal / IDOR** — read, delete, enrich another tenant's account | 404 on all |
| Tenant switching without membership | 403 |
| SQL injection in identifiers and filters | No leakage |
| Webhook forgery — unsigned, bad signature | 400 |
| Secret exposure in responses | Provider keys return a hint only; CRM token hidden |
| Security headers | HSTS, X-Content-Type-Options, X-Frame-Options, CSP all present |

Two probe results initially read as failures and were **not** vulnerabilities: the admin `404`s
(deliberate obfuscation) and a path-traversal `200` (the SPA catch-all serving `index.html`). Both
were verified before classification.

### NOT TESTED

External network testing, TLS analysis, XSS/CSRF against the SPA, file upload (no upload surface
beyond CSV, which is parsed not stored), deserialization, SSRF against the source-database DSN
form (guard exists and is documented; not probed), fuzzing, and rate-limit bypass at the edge.

---

## 7. Disaster Recovery — **PASS WITH RISK**

`scripts/dr_rehearsal.sh` executed end to end: marker written → backup taken → second marker
written → **table dropped** → restore → verify.

```
RTO (restore wall-clock):          10s
pre-backup marker restored:        2 (expected 2)
post-backup marker after restore:  0 (expected 0 — the RPO gap)
VERDICT: PASS
```

Application verified healthy afterwards: `/health` 200, 7 tenants and 285 ledger rows intact,
login working. **This is a genuine restore, not a backup check.**

### Risks attached to that PASS

- **Scenario C only** (accidental deletion). Primary-database failure, corruption, region loss and
  bad-deployment recovery are `NOT TESTED`.
- **RTO of 10s reflects a ~1 MB dataset.** It does not predict recovery time at production volume.
- **Database only.** Configuration, secrets and DNS recovery were not exercised.
- **Backups are not scheduled.** `scripts/backup_cron.sh` supports retention and S3 offsite, but
  no crontab entry is installed and the only dump on disk is the one this rehearsal created.
  A restore capability that has nothing recent to restore from is not a recovery plan.

---

## 8. Billing and Data Integrity — **PASS**

The strongest area, and the one with the most evidence behind it.

- **Uniform pricing.** Every priced, non-gauge capability burns exactly `credits_per_unit ×
  quantity`, verified at 1, 7 and 50 units across the *entire* seeded rate card.
- **Exactly once.** Four attempts on one idempotency key cost what one costs. Scoped
  `(tenant_id, key)`, so one tenant cannot settle another's charge.
- **No second price.** The unit past a quota costs what the unit before it cost; `_burn_for_usage`
  is asserted structurally never to read `overage_price_credits`; a usage invoice bills no
  non-gauge capability.
- **Concurrent double-spend.** 25 parallel connections against real Postgres: 10 allowed, 20
  spent, balance exactly 0. **With the lock disabled as a control: 19 allowed, 38 spent, balance
  −18.** The lock is load-bearing and proven so.
- **Adversarial input.** Negative, zero, NaN and infinite quantities; 1000× the cap; spending past
  zero — all refused without moving the balance.

**Proven live via the SuperAdmin console:** rate set to 9 → charged 9; 2 → 2; 5 → 5.

---

## 9. Observability — **PARTIAL**

13 alert rules are defined and they are good ones — `AppDown`, `HighServerErrorRate`,
`QueueBacklogGrowing`, `WebhookSignatureFailing`, `WebhooksVerifiedButNeverApplied`,
`EntitlementEngineErroring`, `DunningBacklogGrowing`.

**The monitoring stack is not running.** Zero Prometheus, Grafana or Alertmanager containers.
The rules therefore evaluate against nothing, and the full operational loop —

```
failure → metric changes → alert fires → operator paged → runbook
```

— is `NOT TESTED`. During this audit, a load test drove a 4.81% error rate for two minutes and
nothing alerted, because nothing was watching.

`/metrics` is served and the counters are well chosen (including `nexus_llm_fallback_total`, added
during an earlier round precisely because silent degradation was this system's recurring failure).

---

## 10. Stripe — **FAIL (not launch-ready)**

```
account acct_1TyQzPFIw0Fxml7q  (test mode)
  charges_enabled     NO
  details_submitted   NO
  webhook endpoints:  0
```

The account cannot take a payment, and no webhook endpoint is registered, so a completed checkout
would never reach the application. **Our side of the chain is proven** — signature, freshness,
tamper-detection, replay and tenant resolution all verified live — so this is onboarding and
configuration, not code. See `docs/STRIPE-TEST-PLAN.md`.

---

## 11. Production Readiness Scorecard

Scoring: 0–10 per domain. **A critical failure in any single domain caps the overall verdict
regardless of the average** — a system is not launch-ready because it scores well on nine things
and fails on the tenth.

| Domain | Weight | Score | Status |
|---|---:|---:|---|
| Functional correctness | 10 | 8.0 | PASS |
| Billing / financial integrity | 10 | 9.0 | PASS |
| Data integrity | 10 | 8.5 | PASS |
| Authentication | 8 | 8.5 | PASS |
| Authorization | 10 | 8.0 | PASS (after the P0 fix) |
| Security (broader) | 10 | 6.0 | PARTIAL — no external pen test |
| **Performance** | 10 | **4.0** | **FAIL** — errors fixed, latency unproven |
| **Scalability** | 10 | **3.0** | **FAIL** — CPU-bound at ~37 req/s per worker; no traffic model |
| Reliability | 8 | 5.0 | PARTIAL |
| Availability | 8 | 4.0 | PARTIAL — single-host, no autoscaling |
| Disaster recovery | 10 | 6.5 | PASS WITH RISK |
| Backups | 8 | 3.0 | FAIL — not scheduled |
| Observability | 8 | 4.0 | PARTIAL — stack not running |
| Infrastructure | 6 | 5.0 | PARTIAL |
| CI/CD | 6 | 7.0 | PASS |
| Operations | 8 | 4.5 | PARTIAL |
| Mobile | — | — | NOT APPLICABLE |

**Weighted average: 6.2/10 — and the average is not the decision.** Performance, scalability and
backups each fail independently of it.

---

## 12. Findings

### P0 — Launch blocking

**P0-1 · Database connection pool exceeds Postgres capacity by 1.8×**
*Evidence:* k6 run — 4.81% 500s, p95 2.66 s, `QueuePool limit of size 10 overflow 20 reached`;
`show max_connections` = 100 against 175 demanded.
*Impact:* Authenticated requests fail under ordinary read load. At full pool the worker is
starved too.
*Fix:* Set `db_pool_size`/`db_max_overflow` so `(app_procs + worker_procs) × (pool + overflow +
platform_pool)` ≤ 80% of `max_connections` — with the current topology, pool 10 / overflow 5.
Alternatively raise `max_connections` and size the host for it, or introduce PgBouncer.
*Verification:* Re-run k6 at 200 VUs; all three SLOs must pass. Then stress to find the true
ceiling.
*Status:* **RESOLVED in `44ea3d8`.** max_connections 300, pool 16, overflow 5;
`connection_budget()` computes the fleet demand and logs it at every boot; six regression tests
pin the arithmetic including the rollout doubling. Re-run: error rate 4.81% → 0.05%.
*Launch blocking:* No longer.

**P0-5 · Application is CPU-bound at ~37 req/s per worker**
*Evidence:* Post-fix load test — p95 3.09 s against a 500 ms objective, with both app containers
at ~224% CPU and Postgres at 0.04% and one active query.
*Impact:* The latency SLO cannot be met by adding database capacity or connections. Throughput
scales only with app processes, and each costs a full core under load.
*Fix:* Profile the request path before adding hardware — the cost is in serialisation, ORM
hydration and per-request setup, not in queries. Then size the app tier against a real traffic
model.
*Verification:* Re-run on a host where the generator is external; all three SLOs must pass at
peak × 1.5.
*Launch blocking:* **Yes**, for any SLA quoting latency.

**P0-2 · Backups are not scheduled**
*Evidence:* `crontab -l | grep backup_cron` → 0. One dump on disk, created by this audit.
*Impact:* The proven restore capability has nothing recent to restore. RPO is unbounded.
*Fix:* Install the cron entry with `BACKUP_OFFSITE` set; verify a dump lands offsite; alert on
backup age.
*Launch blocking:* **Yes.**

**P0-3 · Monitoring stack is not running**
*Evidence:* 0 Prometheus/Grafana/Alertmanager containers. A 2-minute 4.81% error rate alerted
nobody.
*Fix:* Start the overlay; trigger a controlled failure and confirm an alert is actually received.
*Launch blocking:* **Yes** — no SLA can be offered on a service nobody is watching.

**P0-4 · Stripe cannot take a payment**
*Evidence:* `charges_enabled: NO`, 0 webhook endpoints.
*Launch blocking:* **Yes**, if launch includes paid conversion.

### P1 — Must fix or formally accept

- **P1-1 · No external penetration test.** Authorization and business logic were probed here;
  network, TLS and the SPA were not.
- **P1-2 · DR tested for one scenario only.** Database failure, corruption, region loss and bad
  deployments are untested; recovery covered the database alone, not secrets/config/DNS.
- **P1-3 · Single-host deployment, no autoscaling.** Two app replicas on one machine share one
  failure domain.
- **P1-4 · No traffic model exists.** No DAU, peak-concurrency or transaction-volume figures, so
  "expected peak plus margin" cannot be defined — and therefore Gate 4 cannot be answered even
  once P0-1 is fixed.
- **P1-5 · Worker runs as the database owner**, bypassing RLS for all background work. 16 call
  sites need converting before the role can be changed; flipping it blind makes cross-tenant
  sweeps return zero rows *silently*.
- **P1-6 · 18 priced capabilities still charge nothing**, each recorded in `KNOWN_GAPS`.

### P2 — Post-launch

- No edge rate limiting (the in-app limiter is Valkey-shared but there is no WAF).
- `risky_reason` computed for email verification and never surfaced.
- Firecrawl unfunded; the per-task search cost split cannot be used.
- Two `legacy-unlimited` tenants consume without spending.

---

## 13. Mandatory Launch Gates

| # | Gate | Verdict |
|---|---|---|
| 1 | Functional — critical journeys proven | **PASS** — signup, ICP, discovery, enrichment, drafting, billing all driven live |
| 2 | Security — meaningful pen testing | **PARTIAL** — authz/business logic done, external not |
| 3 | Performance — realistic production-path load | **FAIL** — error rate now passes; latency does not, and the environment cannot prove it either way |
| 4 | Scalability — peak + margin demonstrated | **FAIL** — and no traffic model to define peak |
| 5 | Reliability — dependency failures tested | **PARTIAL** — provider failures observed in the wild, not injected |
| 6 | Disaster recovery rehearsal | **PASS WITH RISK** |
| 7 | Backup restoration demonstrated | **PASS** — but backups are not scheduled |
| 8 | Observability — operators can detect and diagnose | **FAIL** — stack not running |
| 9 | Deployment and rollback | **PARTIAL** — rolling deploy verified; rollback NOT TESTED |
| 10 | Data integrity | **PASS** |
| 11 | Privacy — sensitive-data controls | **PASS WITH RISK** — Fernet at rest, secrets absent from responses; log redaction NOT TESTED |
| 12 | Operations — on-call can run it | **PARTIAL** — runbooks exist; no on-call rota or paging |

**Four gates fail. Rule 14 applies: no GO is available.**

---

## 14. Go / No-Go

# NO-GO

Not because the software is bad — the billing engine is the most rigorously evidenced part of this
system and I would defend it — but because four mandatory gates fail and three of them fail on
*operational readiness rather than code*.

### Conditions to reach CONDITIONAL GO

| # | Condition | Verification |
|---|---|---|
| 1 | ~~Fix the connection pool arithmetic~~ **DONE (`44ea3d8`)** | Error rate 0.05%; budget logged at boot |
| 2 | Schedule backups with offsite copy | A dump lands offsite; restore it |
| 3 | Run the monitoring stack | Trigger a failure; confirm the alert is received |
| 4 | Complete Stripe onboarding + webhook | One test-mode payment through all five checks |
| 5 | Build a traffic model | DAU, peak concurrency, transaction volume — then re-load-test at peak × 1.5 |
| 6 | Rollback rehearsal | Deploy a bad build; roll back; measure |

### To reach GO, additionally

External penetration test; DR scenarios A, B and D; soak and stress testing; dependency-failure
injection.

### Accepted residual risk at CONDITIONAL GO

Single-host deployment; worker on the owner role; 18 unbilled capabilities; no edge WAF. Each
needs a named owner and a date.

---

## 15. Post-Launch Monitoring Period

For the first 30 days: daily review of error rate, p95 latency, connection-pool saturation, queue
depth, `nexus_llm_fallback_total`, webhook `bad_signature` rate, and credit-burn anomalies. Weekly
`reconcile.py` against Stripe. Any P0 recurrence should trigger rollback rather than a forward fix.

---

## 16. Auditor's Note on Evidence

Every result above came from a command executed against the running system during this audit.
Where a capability was not exercised it is marked `NOT TESTED` rather than inferred from
configuration — including in the two places where doing so would have made this report look
better: the load test covers ~75% of the request path, and the DR rehearsal covers one scenario
of four.

Two probe results that initially appeared to be vulnerabilities were investigated and found to be
correct behaviour. They are recorded as such rather than counted, because an audit that inflates
its findings is as useless as one that hides them.
