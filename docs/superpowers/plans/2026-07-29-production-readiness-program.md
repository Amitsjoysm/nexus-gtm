# Production Readiness Program — NEXUS GTM

Remediation plan for the technical due-diligence audit. Every milestone is independently
deployable, additive, and preserves existing behaviour. Reviewed by Codex.

**Golden rules (inherited from the billing program, they worked):**
- No existing API, response shape, or UI behaviour changes without an explicit note.
- No existing test is weakened. If one breaks, STOP and report — it is defending something.
- Every milestone ends green on the full suite, then deploys and verifies against live Postgres.
- SQLite has no RLS. Anything touching tenancy or money gets a **live** verification pass.

**Decisions taken by the product owner (2026-07-29):**
| Question | Decision |
|---|---|
| Stripe model | **Both** — hosted Checkout/Portal for self-serve, API path retained for enterprise |
| Signal sources | **All four tiers**, and **synthetic/demo data removed from production** |
| MFA | **TOTP + email OTP** (both offered) |
| Admin RBAC | **Per-permission matrix** |

---

## Sequencing and file contention

Milestones that touch the same file cannot run in parallel. Contended files:

| File | Milestones |
|---|---|
| `nexus/workers/tasks.py` | M11, M16-M20, M22 |
| `nexus/workers/queue.py` | M11 only |
| `nexus/api/deps.py` | M13, M14 |
| `nexus/billing/payments.py` | M12 only |
| `nexus/ingestion/sources.py` | M16-M20 |
| `nexus/core/config.py` | most — small append-only edits, low risk |

**Parallel-safe groups:** (M11) → (M12 ‖ M13) → (M14) → (M15 ‖ M16) → (M17-M20 sequential) →
(M21) → (M22 ‖ M23 ‖ M24) → (M25) → (M26 ‖ M27).

---

# PHASE 0 — RELIABILITY (P0, blocks everything)

## M11 — Job durability: retry, backoff, dead-letter

**Problem.** `workers/worker.py:44` dispatches a job once. `dispatch` swallows handler errors and
logs. A failed job is **gone** — no retry, no DLQ, no visibility timeout. Periodic sweeps
self-heal because they are re-enqueued each tick and are idempotent; `process_account`,
campaign sends, and orchestration runs do not. Silent customer-visible data loss.

**Deliverables**
- `attempts` + `max_attempts` + `enqueued_at` on `Job`; backward compatible (defaults) so
  in-flight jobs from the old shape still dispatch.
- Retry with exponential backoff + jitter on handler exception, re-enqueued to the same queue.
- `billing_dead_letter`-style `dead_letter_jobs` table (migration `0027`): job name, payload,
  error, attempts, first/last seen. **Platform-global**, `subject_tenant_id` naming per the RLS
  trap in CLAUDE.md.
- `handle_*` contract unchanged — handlers stay ignorant of retries.
- Admin API: list dead letters, replay one, replay all for a job name.
- Metrics counters: enqueued, succeeded, retried, dead-lettered.

**Preserves:** every existing handler signature and the `HANDLERS` registry. In-memory queue
behaviour for tests is unchanged when `max_attempts=1`.

**Acceptance**
- A handler that raises twice then succeeds is retried and completes.
- A handler that always raises lands in `dead_letter_jobs` after `max_attempts`, exactly once.
- Replay re-runs it and clears it.
- Existing worker tests pass untouched.

---

# PHASE 1 — FINANCIAL CORRECTNESS (P0)

## M12 — Stripe Checkout + Customer Portal + state-driving webhooks

**Problem.** 7 of ~25 Stripe surfaces implemented. No `checkout.session.completed`,
`customer.subscription.*`, or `invoice.*` handlers, so a cancellation or card change made in
Stripe never reaches our database. Stripe and NEXUS will diverge, and divergence in billing is a
legal problem, not a bug.

**Deliverables**
- `create_checkout_session(tenant, plan_id, success_url, cancel_url)` → hosted Checkout for
  self-serve plans; `create_billing_portal_session(tenant)` → hosted portal.
- Tenant-facing endpoints `POST /billing/checkout` and `POST /billing/portal`, RBAC `admin`+.
- Webhook handlers, all idempotent on event id (the existing PK guard already covers replay):
  `checkout.session.completed` → activate subscription, store `psp_subscription_id`
  `customer.subscription.created|updated|deleted` → mirror status/plan/period into
  `billing_subscriptions`
  `invoice.paid|payment_failed|finalized` → drive invoice status and dunning
- **Reconciliation job**: nightly diff of our subscriptions vs Stripe's; report drift rather than
  auto-correct (auto-correcting a billing disagreement unsupervised is worse than reporting it).
- **Enterprise path untouched**: admin custom-plan + manual `collect_invoice` continue to work
  exactly as today, and are explicitly excluded from portal self-service.

**Preserves:** the entire existing collection path, `metered()`, rating, dunning. Checkout is
additive; nothing currently working is rerouted through it.

**Acceptance (live, Stripe test mode + `stripe listen`)**
- Checkout completes → subscription active in our DB, driven by webhook not by our own write.
- Cancel in the portal → our subscription reaches `canceled` within one webhook.
- `invoice.payment_failed` → dunning picks it up on schedule.
- Replay of every new event type is a no-op.

---

# PHASE 2 — SECURITY (P0)

## M13 — MFA (TOTP + email OTP) and auth hardening

**Problem.** No MFA anywhere (`grep -iE "mfa|totp"` → zero matches). `auth_rate_limit_enabled`
defaults to **false**, so login and password-reset are unthrottled out of the box.

**Deliverables**
- `user_mfa` table (migration `0028`): user_id, method (`totp`|`email`), secret (Fernet-sealed
  using the existing `network/crypto.py` pattern), confirmed_at, last_used_at.
- `mfa_recovery_codes`: one-way hashed, single-use.
- TOTP: RFC 6238, ±1 step drift, **replay guard** (a code cannot be reused inside its window).
- Email OTP: reuses the existing registration OTP machinery.
- Enrolment, verification, disable, and regenerate-recovery-codes endpoints.
- Login becomes two-step **only when MFA is enrolled** — existing single-step login for
  non-enrolled users is byte-for-byte unchanged.
- `auth_rate_limit_enabled` default flipped to **true**; tests that make rapid auth calls get an
  explicit opt-out fixture rather than the default being weakened.

**Preserves:** every existing auth flow for users without MFA. Enrolment is opt-in per user;
admin-required enforcement is a later, separate decision.

## M14 — Per-permission admin RBAC

**Problem.** `api/deps.py:80` checks only that an active `platform_admins` row exists. It never
reads `platform_role`. A "support" admin can reprice plans and mint unlimited credits.

**Deliverables**
- `permissions: list[str]` on `platform_admins` (migration `0029`), plus role presets that
  expand to permission sets: `billing.read`, `billing.write`, `pricing.write`, `credits.grant`,
  `credits.grant.capped`, `subscriptions.write`, `admins.manage`, `users.manage`.
- `require_platform_permission("pricing.write")` dependency; `require_platform_admin` retained as
  `billing.read` so **no existing endpoint changes behaviour** until each is annotated.
- Every admin endpoint annotated with its required permission.
- Capped credit grants for support (configurable ceiling, default 1000 credits).
- Existing superadmins are backfilled with the full permission set — nobody loses access.

**Preserves:** all current admin endpoints and the superadmin experience. Only *new* narrower
roles gain restrictions.

---

# PHASE 3 — OBSERVABILITY (P1)

## M15 — Metrics on by default, billing and queue instrumentation

**Problem.** `main.py:96` — metrics disabled unless explicitly enabled. Default deployment is
blind to queue lag, 402/429 rates, dunning depth, and webhook failures.

**Deliverables**
- `metrics_enabled` default **true**, still degrading to no-op if the instrumentator is
  incompatible (that guard already exists and stays).
- Counters/gauges: queue depth, job outcomes by name, dead-letter count, webhook
  verify-failures, 402/429 by capability, dunning queue depth, collection success rate,
  credit burn rate, entitlement resolution latency.
- Prometheus alert rules committed under `deploy/monitoring/`.
- `/metrics` stays out of the OpenAPI schema and behind the existing exclusion.

---

# PHASE 4 — SIGNAL INTELLIGENCE (P1) — the largest gap

## M16 — Source framework + remove synthetic data from production

**Problem.** `config.py:175` — `signal_sources: str = "demo"`. The **default pipeline emits
synthetic fixtures**. Alerts built on it therefore have no business value.

**Deliverables**
- `DemoSignalSource` demoted to an explicit offline test double, mirroring
  `network/connectors/fixture.py`: importable by tests, **never selectable in production**. A
  production config naming `demo` fails loudly at startup rather than silently faking data.
- Default `signal_sources` becomes `web,rss`.
- Source contract hardened: per-source timeout, structured `SourceResult` with
  `raw_payload` retained for provenance, per-source failure isolation and metrics.
- `signal_source_runs` table: source, account, started/finished, item count, error — the
  **crawl history** the audit found missing.

**Preserves:** `tests/conftest.py` keeps injecting the demo source explicitly, so the offline
suite is unaffected. This is a default change, called out in release notes.

## M17 — Hiring-board signals (Greenhouse, Lever, Ashby)

Public JSON boards, no key, no ToS risk. Highest GTM signal-to-effort: hiring reveals budget,
growth direction, and tech stack.

**Deliverables:** board discovery from company domain; per-role parsing; signals for
`hiring.surge`, `hiring.new_function`, `hiring.seniority_shift`; tech extraction from job text
into `account.tech_stack`; dedupe on `(board, req_id)`.

## M18 — Funding and filings (SEC EDGAR + funding classification)

**Deliverables:** EDGAR full-text search by company name/CIK (free, official, documented rate
limits respected with a declared User-Agent); 8-K/S-1/10-K signal extraction; funding-round
classification from the existing news stream with amount/stage/investor extraction.
**Crunchbase deferred** — needs a paid key; ask before building.

## M19 — Developer and product signals (GitHub, Product Hunt, changelogs)

**Deliverables:** GitHub public API (releases, stars velocity, language mix → technographics),
Product Hunt launches, changelog/release-notes RSS. Unauthenticated GitHub is 60 req/h — a token
is optional and raises it to 5000; the source degrades rather than fails without one.

## M20 — Website change monitoring

**Deliverables:** watch pricing / careers / about / security pages; normalised content hashing
(ignore nav, timestamps, CSRF tokens); emit `website.pricing_changed`,
`website.careers_changed`; store previous hash + diff summary. This is ours end to end — no
third party, no key.

## M21 — Alert engine v2 and notification routing

**Problem.** Alerts are created in exactly three places, none of them from an incoming signal
(`plays/engine.py:91`, `workers/tasks.py:383`, `:557`). Channels are configured **per
environment**, so "users choose where alerts are delivered" is not currently possible.

**Deliverables**
- Signal→alert rules engine: category, importance, confidence, ICP-match reason, summary,
  source URL, suggested SDR action, next best action — the full shape the audit asked for.
- `contact_id` and ICP-match linkage on alerts.
- Alert-level dedupe window (distinct from signal dedupe) so one event does not alert twice.
- `notification_preferences` (migration `0030`): per **user**, per category, per channel,
  with quiet hours and digest-vs-immediate.
- Channel registry extended: WhatsApp, Teams, Discord, Web Push, SMS as declared-but-inert
  adapters following the repo's provider-seam convention (clear error until configured, never a
  fake success).

**Preserves:** existing tenant-level channels keep working as the fallback when a user has
expressed no preference.

---

# PHASE 5 — BILLING COMPLETENESS (P2)

## M22 — Proration, trial expiry, pause/resume
Mid-cycle plan changes currently charge the new base fee with no day-weighting. Deliverables:
seat-day proration credit/debit lines, automated trial→active/expired transition, pause and
resume with entitlement suspension.

## M23 — Revenue reporting
`grep -riE "\bmrr\b|churn|ltv"` → no matches. Deliverables: MRR/ARR, expansion/contraction,
churn, LTV, failed-payment and credit-usage reports; admin dashboard; all derived from existing
tables, no new writes.

## M24 — Feature flag evaluation
`feature_flag` exists on entitlements (`models/billing.py:124`) and is **never read**. Same dead
-config class as `burst_limit` and `depends_on` before they were fixed. Deliverables: evaluate
in `resolve_entitlement`; flags settable per plan, per tenant, per environment.

---

# PHASE 6 — ADMIN COMPLETENESS (P2)

## M25 — User and organisation management
Zero matches for `suspend|lock_account|impersonat`. Deliverables: create/suspend/reactivate/
delete user, force password reset, reset MFA, **time-boxed and fully-audited impersonation**
(banner in UI, separate token type, auto-expiry), merge duplicates, transfer ownership,
organisation suspend/transfer/lifecycle.

---

# PHASE 7 — SCALE (P2)

## M26 — Usage partitioning and retention
`billing_usage_events` is unpartitioned. Deliverables: monthly partitions with a rolling create
job, archive/retention policy, verified query plans on the quota hot path.

## M27 — Zero-downtime deploys
Measured 1×502 on a genuine restart even with health checks and retry. Deliverables: two app
replicas with staggered restart, readiness gating, documented rollback.

---

## Cross-cutting requirements for every milestone

**Feature flags / rollout.** Each behavioural change ships behind a setting defaulting to
current behaviour, flipped in a separate commit after verification.

**Migrations.** Additive only. The chain must keep replaying —
`tests/test_migrations_replay.py` is the gate. Any table with a tenant dimension either takes
`tenant_id` (and gets RLS automatically) or names it `subject_tenant_id` deliberately.

**Testing.** Per milestone: unit + adversarial/abuse tests + a live verification pass for
anything touching tenancy, money, or auth.

**Rollback.** Every migration has a tested `downgrade`. Every default flip is one setting.

**Regression checklist per milestone:** full suite green · ruff clean · single alembic head ·
chain replays · RLS posture unchanged · live smoke on the deployed stack.
