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

**As built — two deliberate deviations.**

1. *No `billing.write`.* It would have been a grab-bag covering repricing, plan moves, and
   charging a card. Split into `pricing.write`, `subscriptions.write`, and `invoices.collect`
   (plus `jobs.manage` for dead-letter replay), so finance can reprice without being able to
   replay jobs. Nine permissions, each mapping to one real decision.
2. *No backfill.* Rather than writing the full set onto every existing row, an **empty**
   `permissions` list falls back to the role preset (`effective_permissions`). Same outcome —
   nobody loses access — but there is no window in which a half-run backfill leaves an admin
   half-granted, and `0029` is a pure `add_column`. Writes store the **expanded** set, so
   redefining a preset later cannot retroactively re-grant power.

Also: `require_platform_admin` now *is* `billing.read` rather than "any active row". Left as a
flat gate it would have been a loaded gun — the next endpoint written against it would silently
reopen exactly this hole. No endpoint uses it any more.

Verified in `tests/test_admin_permissions.py` (17 cases). The deny cases were confirmed to bite:
restoring the old "any active row = full access" behaviour fails 7 of them.

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

**As built — deviations and additions.**

1. *No `SourceResult` return type.* Changing what `SignalSource.fetch` returns would break every
   source and every injected double for no gain. The structured record is the **run row**
   (`signal_source_runs`), written by `IngestionService` around each source, with the rendered
   queries kept as `provenance`. Sources stay ignorant, exactly as handlers did in M11.
2. *Two guards, not one.* `demo_signals_active` already forced synthetic signals off in
   staging/prod, but silently. A startup validator now refuses to boot when
   `NEXUS_SIGNAL_SOURCES` names `demo` there, because "asked for demo signals, got none" and
   "pipeline is broken" were previously the same observation.
3. *`empty` is a distinct outcome from `ok`.* A source that runs cleanly and returns nothing every
   single time is broken; folding the two together hides precisely the case this milestone exists
   to surface.
4. *Per-source timeout is a source-declared `timeout_s`*, not a config key per source. The shared
   8s default assumes one request; a multi-query source needs more, and one killed mid-run reports
   nothing — indistinguishable from an account with no signals.

**Also delivered here (out of the original scope, driven by live measurement).** The dork library
(`nexus/ingestion/dorks.py`) and `DorkedSearchSource`, because "the sources are real" is worth
little if the queries cannot find anything. Verified against live search rather than assumed, which
surfaced three things no test would have:

- Keyless DuckDuckGo returns **403 after ~10 rapid queries** — inside one account refresh — and,
  separately, returns **zero results for any query containing `site:` or `-site:`**. It does not
  error either way, so an operator dork there is a source that silently finds nothing forever.
  Hence per-provider pacing, a stop-the-batch-on-provider-failure rule, and `plain` as the default
  dialect.
- Exa is **semantic**: operator dorks measurably return the wrong thing there (the ATS dork returned
  other companies' postings that mention the account name). Hence three dialects, not one.
- `crunchbase.com` is a **directory**, not a publisher, and returned profile pages that match a
  name perfectly and report no event.
- Two precision rules came straight off live Firecrawl output: the account name must appear in the
  **title** (an industry round-up mentioning Vanta scored funding 0.90), and the event must be
  classified from the **title alone** (a product page whose body recalled an earlier round scored
  funding 0.85).

Every one of these was invisible to the test suite and would have shipped as "signal collection is
enabled but finds nothing useful". The lesson is the same one the RLS traps taught: **the offline
suite cannot tell you whether a real integration works.**

`firecrawl` was added as a keyword-native provider so signal collection does not depend on a single
vendor, and `NEXUS_SIGNAL_SEARCH_PROVIDER` keeps that choice separate from the global
`search_provider` — lookalikes and company discovery need Exa-only capabilities, and repointing the
global setting would have taken them down silently.

## M17 — Hiring-board signals (Greenhouse, Lever, Ashby)

Public JSON boards, no key, no ToS risk. Highest GTM signal-to-effort: hiring reveals budget,
growth direction, and tech stack.

**Deliverables:** board discovery from company domain; per-role parsing; signals for
`hiring.surge`, `hiring.new_function`, `hiring.seniority_shift`; tech extraction from job text
into `account.tech_stack`; dedupe on `(board, req_id)`.

### Revised design — measured, 2026-07-31

Two corrections to the sketch above, both from probing the live endpoints.

**1. "Board discovery from company domain" does not work by guessing.** The token is not the
domain root: Vanta and Ramp 404 on Greenhouse (they are on Ashby), and Linear's Ashby token is
capitalised `Linear`. Guessing fails silently — a 404 is indistinguishable from "not hiring".

What *does* work is crawling the company's **own careers page** and reading the board token out of
the HTML, which is where the page embeds its ATS:

| Careers page | Token recovered |
|---|---|
| vanta.com/careers | `ashby: vanta` |
| ramp.com/careers | `ashby: ramp` |
| linear.app/careers | `ashby: Linear` |
| figma.com/careers | `greenhouse: figma` |
| stripe.com/jobs | none (custom site) — but the domain-root guess hits Greenhouse |

So discovery is a waterfall: careers page → token → keyless board; then the domain-root guess; then
sitemap/careers diffing for Workday and bespoke sites; then the dork library, which already
backstops everything. Cache the result in `Account.custom_fields` so this runs once per account,
not once per refresh.

**2. Do not parse the careers page for the jobs themselves.** Ramp's is 3.7 MB of JavaScript shell
and the postings it renders come from Ashby anyway. Reverse-engineering a SPA to obtain data that is
one unauthenticated GET away is strictly worse.

**Outcome semantics.** A board returning HTTP 200 with an empty list ("exists, nothing open") is a
real business signal and must not be confused with 404 ("not on this ATS"). This maps onto the
`empty` vs `error` outcomes already in `signal_source_runs` from M16.

**Verified shapes.** Greenhouse `boards-api.greenhouse.io/v1/boards/{token}/jobs` → `{jobs, meta}`,
542 for `stripe`, fields `title/location/absolute_url/updated_at/requisition_id`. Ashby
`api.ashbyhq.com/posting-api/job-board/{token}` → `{jobs, apiVersion}`, fields
`title/department/team/employmentType/location/publishedAt` — `publishedAt` gives real recency.
**Lever's populated shape is unverified**: every token tried returned 404 or 200-with-zero, so its
field names come from documentation, not measurement. Treat it as unconfirmed until a live board
with postings is seen.

**Taxonomy note.** `hiring.surge` / `hiring.new_function` / `hiring.seniority_shift` do not exist in
`SIGNAL_KINDS`, which is a flat set. Either map them onto the existing `job_posting` / `hiring`
kinds, or extend the taxonomy deliberately — inventing kinds that no play trigger or alert rule
matches on would produce signals nothing acts upon.

**LinkedIn: through the search index, not by scraping.** Fetching linkedin.com is off the table —
no compliant API, and it bans scrapers; CLAUDE.md already records this for the network subsystem,
which is why LinkedIn ingestion there uses a member-supplied CSV export. But LinkedIn *job pages are
publicly indexed*, and asking a search provider about them is a different act from hitting
LinkedIn's servers. Verified live: `site:linkedin.com/jobs "Vanta" hiring` returns posting pages
whose titles read "Vanta hiring Senior Copywriter" — the company name is in the title, so the
existing precision gate applies unchanged. Shipped as the `hiring_linkedin` dork with
`require_url="linkedin.com/jobs/"`, because the same query also surfaces company pages and
`linkedin.com/company/vantaesports` passed the name gate for an unrelated esports org.

The other compliant open-API social sources (GitHub, Hacker News, Product Hunt, Bluesky) are M19.

**As built.** `nexus/ingestion/ats.py` (discovery + fetch + ownership verification) and
`AtsSignalSource`. On by default, `no_ats` opts out. The signal is aggregated — one `hiring` signal
per account per month with the role count and departments — because Vanta has 100 open roles and
Stripe 542, and one signal per requisition would bury everything else in the inbox. Live output for
Vanta: *"100 open roles on ashby. Hiring in: Revenue (54), Software Engineering (20), Marketing (7),
People (5)"*.

Deferred deliberately rather than faked: **tech extraction from job text** needs per-posting fetches
(the list endpoints carry no description), and **surge / new-function / seniority-shift** need a
stored baseline. `signal_source_runs.items_found` is the natural baseline and makes the comparison
real rather than heuristic — a follow-up, not a guess. The taxonomy question stands: those three
names are not in `SIGNAL_KINDS`, so they map onto `hiring` until the taxonomy is extended
deliberately.

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

**As built (2026-07-31).** `nexus/ingestion/webwatch.py` + `WebsiteWatchSignalSource` +
`page_snapshots` (migration `0031`). Opt-in via `NEXUS_SIGNAL_SOURCES=...,website` because it is the
heaviest source in the pipeline. New `website_change` signal kind; pricing changes carry 0.75, other
pages 0.55. A first sighting establishes a baseline and emits nothing.

The normaliser was the whole milestone. **Raw-HTML hashing is unstable across two fetches seconds
apart** on 2 of 3 live pages tested (linear.app/pricing, ramp.com/security), so a naive watcher
would have reported "pricing changed" on every single run. Dropping script/style/svg *bodies*
before stripping tags, lowercasing, removing build ids / ISO timestamps / cache-busters, then
collapsing whitespace, was stable on all three. Every nuisance is pinned by a test.

**Measured caveat (2026-07-31).** Sitemap-based change detection is a fallback, not the mechanism:
`linear.app` publishes 900 `<lastmod>` entries, `vanta.com` publishes 3,149 URLs with **zero**
`<lastmod>`, and `ramp.com` serves no sitemap at all. Page-hash diffing on a named shortlist of URLs
is the reliable path; sitemap `<lastmod>` is an optimisation where it happens to exist.

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

**As built (2026-07-31) — core delivered, extensions deferred.**

Delivered: `alerts/rules.py` (signal→alert decision, category, severity, suggested next action),
`alerts/routing.py` (per-user channel + quiet hours), `alerts/signal_alerts.py` (the missing
subscriber), `notification_preferences` + migration **`0032`** — note the plan said `0030`, which
was taken by `signal_source_runs` in M16.

The confirmed root finding: `signal.created` was published on every ingested signal and **nothing
subscribed to it**. Verified by grep — the only subscriber on the bus was CRM sync listening for
`account.scored`.

Four decisions worth recording:

1. *A floor, not "alert on everything".* Weak mentions (the classifier's 0.4 tier) are recorded and
   visible but never interrupt. An alert costs attention.
2. *Alert dedupe is separate from signal dedupe* — the former is per category per account per day,
   because two distinct job postings are two real signals and one notification.
3. *An unknown signal kind alerts quietly rather than vanishing*, matching the billing engine's
   unknown-capability-resolves-to-allow bias.
4. *Quiet hours are minutes from local midnight*, so the overnight wrap is arithmetic. A naive
   `start <= now < end` disables quiet hours for exactly the people who set them overnight.

Deferred rather than stubbed: **ICP-match linkage on alerts**, the **digest delivery worker** (the
`digest` mode is decided and stored but a sweep must still send it), and the **inert WhatsApp /
Teams / Discord / Web Push / SMS adapters**. Each is additive on top of what shipped; none of them
is load-bearing for the gap this milestone existed to close.

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


---

# As-built notes, M22–M27 (2026-07-31)

## M22 — proration, trial expiry, pause/resume

`nexus/billing/lifecycle.py`, pure functions so the money arithmetic is testable without a database.
**Rounding is toward the customer** — the credit rounds up, the charge rounds down. Any rounding
rule loses a fraction of a cent somewhere; losing it in the customer's favour makes it a policy
rather than an overcharge they can dispute and be right about.

An expired trial **without** a payment method is cancelled, not flipped to `active`. Activating one
would manufacture receivables that can never be collected and pollute MRR with revenue that does not
exist. Pausing a `past_due` subscription is refused: `suspended` is a status the dunning sweep
ignores, so allowing it would be a way to make a real debt stop being chased.

### Wired up (2026-08-03) — the arithmetic had no callers

The milestone above shipped `lifecycle.py` with 23 passing tests and **nothing calling it**. The
calculator was correct and every customer was still billed a full month for two days of service:
exactly the dead-config failure the billing engine exists to prevent, committed by the engine's own
author. What closed it:

* **`billing_proration_adjustments`** (migration `0036`, tenant-scoped so `apply_rls.py` enrols it).
  `change_plan` writes a signed credit/charge pair; `rate_period` **reads** them on every pass and
  never consumes them, which is what keeps re-rating a pure function of current data — a row marked
  "applied" would vanish from the second pass and the invoice would silently change.
* **Two rows, never one netted row.** An invoice showing only `$52.66` for an upgrade reads as a
  second full month. The credit beside it is the difference between an invoice a customer
  understands and one they dispute.
* **We do not prorate a provider-owned subscription.** Stripe prorates its own changes, so adding
  our lines bills the difference twice. This is why the webhook writes `sub.plan_id` directly
  instead of calling `change_plan` — that asymmetry is now load-bearing, not incidental.
* **`invoice.total_cents` is clamped at zero** while `subtotal_cents` keeps the true arithmetic. A
  net credit is real, but it must never reach the payment provider as a negative charge.
* **`expire_trials`** worker job + scheduler entry, outside the automation gate: a trial ends on a
  date, and gating it on automation is how a trial runs forever in a workspace that never switched
  automation on.
* **Pause suspends entitlements**, per the original deliverable. `resolve_entitlement` returns
  `mode="disabled", source="suspended"`. Still subject to `NEXUS_BILLING_ENFORCEMENT`, so arming
  pause and arming enforcement stay one decision rather than two.
* **UI**: proration preview in `TenantActionsDialog` before the admin commits, pause/resume with an
  audited reason, and a tenant-facing trial countdown, paused banner and pending-proration panel.

**A bug the 1,352-test suite did not catch, found by clicking the page.** `/billing/usage` selected
only `trialing|active|past_due`, so a paused workspace fell down the "no subscription" branch: no
plan, no status, no capabilities. The screen read `No plan assigned` with an empty page —
indistinguishable from a broken account, and the customer had no way to learn they had been paused.
Pinned by `test_a_paused_workspace_still_sees_its_plan_and_status`. That is now the seventh defect in
this project found by running the thing rather than by testing it.

## M23 — revenue reporting

`nexus/billing/revenue.py` + `GET /admin/billing/revenue`. **Derived at read time**, never stored: a
stored MRR figure is a second source of truth that drifts from the subscriptions it describes, and
reconciling the two becomes somebody's month-end job forever. Annual plans divide by 12; trials are
a live logo and zero revenue; `past_due` still counts, because dropping it makes a dunning problem
look like churn. Runs through the platform sessionmaker — under the RLS-bound role a cross-tenant
aggregate silently returns zero rows and would report an MRR of $0.

**UI added 2026-08-03** — a Revenue tab on the billing console. A definition list, not a row of hero
metric cards: the operator reading it is reconciling against a finance sheet, and boxes get in the
way of comparison down a column. "On trial" sits beside "paying workspaces" so a pipeline number is
never read as an MRR number.

## M24 — feature-flag evaluation

**Write surface added 2026-08-03.** M24 made `feature_flag` *evaluated*, which fixed half the
problem: an operator could name a flag on an entitlement but had no way to create it or turn it off,
and an unknown flag is ON by design — so naming one changed nothing. The feature stayed in the exact
dead-config shape it was built to escape. `GET/PUT /admin/billing/flags`, plus per-tenant and
per-environment overrides that can be **cleared**, not only set (without that, a beta grant is
permanent: forcing `tenant:X` false is not the same as "follow the default from now on"). The list
reports `used_by_plans`, because a flag nothing references is free to flip and one wired into a paid
plan turns a customer's feature off. All writes audited. Admin UI is a tab on the billing console.

`nexus/billing/flags.py` + `billing_feature_flags` (migration `0033`). Resolution is narrowest-first:
tenant override → environment override → default. **An unknown flag is ON**, matching the engine's
existing bias (unknown capability → allow). A flag named on an entitlement but never created must
not silently disable a capability the customer is paying for.

## M25 — impersonation

Time-boxed, **read-only**, attributable, audited *before* the token is minted. Gated on a new
`users.impersonate` permission that is deliberately **not** part of `users.manage`: resetting
someone's MFA and becoming them are different powers. Read-only is enforced at the RBAC choke point
rather than in the UI — a banner is a courtesy, a 403 is a control — and `run_agents` /
`run_orchestration` are refused because they spend the customer's money.

Deferred: suspend/reactivate, merge-duplicates and ownership transfer.

## M26 — usage retention

`nexus/billing/retention.py` prunes only events that are **both** rolled up and outside the
retention window. An unrolled event is uncounted usage, and deleting one silently reduces a
customer's bill. Filters on `occurred_at` (the billing fact, and the indexed column), not
`created_at` (row insert time) — caught in testing.

**Partitioning is documented, not migrated** (`scripts/partition_usage_events.sql`). Postgres cannot
`ALTER` a table into a partitioned one; it needs a copy-and-swap under lock, which is a maintenance
window on the table recording what customers are billed for and is not the additive, replayable
migration this project requires. The script flags the two things most easily forgotten in that
window: RLS is **not** inherited by the rename, and counts must match before dropping the original.

## M27 — zero-downtime deploys

Two replicas, `/ready` (not `/health`) as the health probe so a replica that is up but cannot reach
the database is never rotated in, and `deploy/rollout.sh` for a genuinely staggered restart —
`docker compose restart` stops every replica at once, which is exactly the measured 1x502 this
exists to prevent.

**A race this introduced and had to fix:** with two replicas, both start with
`NEXUS_RUN_MIGRATIONS=1`, so two concurrent `alembic upgrade head` runs would race on one
`alembic_version` row. `scripts/bootstrap_db.py` now takes a Postgres **session** advisory lock —
session rather than transaction so it spans the alembic subprocess, and so a replica crashing
mid-migration releases it automatically instead of wedging every future deploy. It blocks rather
than skipping: the right behaviour when another replica is migrating is to wait and then observe an
already-current schema, not to start serving against a half-applied one.

---

# Status, 2026-08-04

Branch `master` created from `release/billing-platform` and pushed; it contains every feature
branch (`feat/relationship-graph`, `feat/research-grounding-enhancements`,
`feature/intelligence-depth`, `fix/signal-classifier-negation`) — each verified as an ancestor, so
nothing was lost and no merge was needed. `main` is untouched and still holds only the licence
commit. Gate: **1453 passed** in 19 minutes (parallel; ~55 min serial before `-n auto`).

## Shipped since the last note

| Area | State |
|---|---|
| M22 proration / trial expiry / pause | **Wired.** The arithmetic had shipped with zero callers |
| M23 revenue | Endpoint + admin tab |
| M24 feature flags | Evaluation **and** a write surface with clearable overrides |
| M25 | **Complete** — impersonation, suspend/reactivate, merge duplicates, ownership transfer |
| Shared company fan-out | On, gated per company by `crawl_verdict` |
| Shared people store | Built (`0037`), email/phone Fernet-sealed, hashed index |
| Apify seam | Built, key rotation; both registered actors (phone, profile) wired — **but all accounts currently 403 unapproved** |
| Account dedupe | One write-point across all six creation paths |

## Not built

* **Crunchbase (`PPUtGNTB6xB9dJ2di`) and company-search (`ayZno82KNVAVaWMpg`) actors** — **removed
  from `ACTORS` on 2026-08-20** rather than wired, so this is now a deliberate absence rather than a
  gap. Crunchbase keys on an organisation URL the shared-company layer has no way to derive (its
  identity is the normalised domain alone), and company-search had 42 runs across 2 users. Neither
  could be exercised even once against the live API to learn its output shape — see the operator
  blocker in `CLAUDE.md`. Re-adding either is one `ACTORS` line plus a consumer.
* **M17 tech extraction**, **M21 digest worker + channel adapters**, **M26 partitioning**
  (runbook only), **external source database** (designed in the company-data-layer plan).

## The deployment gap nobody has started

There is **no Azure configuration of any kind**. What exists is `docker-compose.prod.yml` plus a
Caddy reverse proxy for a single host, and `deploy/rollout.sh` for a staggered restart of two local
replicas. Azure needs, at minimum: a container registry and either Container Apps or AKS manifests;
Azure Database for PostgreSQL (which changes the RLS bootstrap, because the `nexus_app` least-
privilege role and `apply_rls.py` currently run against a container the compose file owns); Key
Vault replacing the `.env` files; and a decision about where the worker runs, since it is
single-process by design and holds the state gauges (`/metrics` on `:9100`).

That last point is the one most likely to be got wrong: **the worker cannot be scaled horizontally
as-is.** Prometheus multiprocess mode does not read gauges or custom collectors, which is exactly
why state lives in the single-process worker (M15). Two worker replicas would double every periodic
sweep and produce two conflicting gauge sets.
