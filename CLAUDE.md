# NEXUS GTM — AI-Powered GTM Intelligence Platform

## 🎯 What We're Building

**NEXUS GTM** is an AI-powered Go-To-Market intelligence platform (a [Pocus.com](https://pocus.com)-class product):
a multi-tenant SaaS for B2B revenue teams that:

1. **Ingests buying signals** — funding rounds, hiring, tech stack changes, news mentions
2. **Scores account relevance** — deterministic ICP-fit using Relevance Engine (no LLM)
3. **Runs AI agents** — research, messaging, enrichment, contact recommendations
4. **Automates rep workflows** — prioritized inbox, saved lists, signal-triggered plays, alerts

**Key principle**: Signal → Action in one move. Reps triage dozens of accounts daily; speed matters.

**Users**:
- **Sales Reps** — work prioritized inbox, research accounts, draft outreach
- **Sales Managers** — build lists, create automated plays, track analytics
- **Admins** — manage team, roles, integrations (CRM, sales engagement platform)

**Tech**: FastAPI backend + React/TypeScript frontend, multi-tenant (Postgres RLS), 
RBAC (owner → admin → manager → rep). Runs fully offline in dev (SQLite + stub LLM).

---

## 🚀 Every New Task: Reference the Code-Review-Graph & Superpowers

**This project has comprehensive analysis ready. Before starting any work:**

1. **Use the code-review-graph** to understand scope and impact:
   ```bash
   code-review-graph search "YourSymbol"              # Find functions/classes
   code-review-graph detect-changes --brief           # See what changed (before review)
   ```
   Open the interactive visualization: `.code-review-graph/graph.html`

2. **Reference superpowers analysis** (generated in scratchpad):
   - **SUPERPOWERS-ANALYSIS.md** — Complete architecture deep-dive + code review methodology
   - **NEXUS-GTM-QUICK-START.md** — Quick commands and project structure
   - Use `/code-review` command — automatically uses graph for focused analysis

3. **Read CLAUDE.md, PRODUCT.md, DESIGN.md** — Grounding for every decision

4. **For architecture questions**: Check code-review-graph, then read relevant SUPERPOWERS-ANALYSIS section

**Why?** The graph cuts code-review token usage by 90% (only affected symbols, not full files).
Superpowers analysis summarizes all major systems so you don't re-derive architecture every task.

## 🛠️ Development Workflow (Using Code-Review-Graph & Superpowers)

### Adding a Feature

1. **Understand the system** (5 min):
   ```bash
   open .code-review-graph/graph.html    # Visualize architecture
   # Read: SUPERPOWERS-ANALYSIS.md (relevant section)
   # Read: PRODUCT.md (requirements)
   ```

2. **Find related code** (2 min):
   ```bash
   code-review-graph search "RelevantComponent"
   # or in Claude: /code-review medium
   ```

3. **Implement** — follow conventions below

4. **Review yourself** (2 min):
   ```bash
   /code-review    # Graph-assisted focused review
   ```

5. **Run tests**: `pytest tests/ -v`

### Fixing a Bug

1. **Reproduce & locate**:
   ```bash
   code-review-graph search "BuggyFunction"
   # Read SUPERPOWERS-ANALYSIS.md risk areas section
   ```

2. **Write a test** that fails

3. **Fix** — verify test passes

4. **Check for ripples**:
   ```bash
   /code-review    # Graph shows all callers/dependents
   ```

### Code Review (Every PR)

1. Use `/code-review` — automatically leverages graph
2. Read SUPERPOWERS-ANALYSIS.md (review checklist + risk areas)
3. Check the summary:
   - Is this security-critical? (multi-tenancy, auth, agents)
   - Does it touch data model? (migrations OK?)
   - Performance impact? (N+1 queries, timeouts?)

### Security & Architecture Review

**Before approving ANY change** to these areas, read the relevant section in SUPERPOWERS-ANALYSIS.md:
- Multi-tenancy (line ~150)
- Agent system (line ~250)
- API authentication (line ~350)
- Database migrations (line ~400)

---

## Repository layout

- `nexus/` — Python backend: FastAPI + async SQLAlchemy 2.0, Pydantic v2, multi-tenant
  (TenantSession + Postgres RLS), RBAC (owner > admin > manager > rep), in-process EventBus.
  Runs fully offline (SQLite + stub LLM + in-memory queue) for tests.
- `frontend/` — **the production React + TypeScript + Vite frontend** (the UI). `npm run build`
  emits the static bundle to `nexus/web/dist/`, which FastAPI serves with SPA fallback.
- `tests/` — pytest (`asyncio_mode=auto`). Keep the suite green. **Runs parallel by default**
  (`addopts = "-n auto --dist loadfile"` in `pyproject.toml`): ~4,500 tests. **`fresh_db` empties
  every table before each test and rebuilds the schema only when SQLite's `schema_version` says
  something changed it** (2026-10-13, `tests/test_fresh_db.py`). It used to drop and recreate all
  ~90 tables per test: measured 3.3 s of reset per test against 80 ms to empty them, and the suite
  crossed Azure's 45-minute job limit on its 2-vCPU agent and GitHub's 25 on slow runners. The
  job limits are now 60 (Azure's free-grant ceiling) and 40, for slow machines only; a hung test
  fails on its own 120 s `--timeout`. `conftest.py` gives each worker its own
  SQLite file via `PYTEST_XDIST_WORKER`, which is what makes this safe; without it one worker's
  reset would wipe another's tables mid-test. `--dist loadfile` keeps a file on one worker,
  because several suites share module-level state (ingestion service, agent runtime, seeded
  catalogs) and splitting a file turns that into flaky cross-talk. Use `-n0` when debugging — serial
  tracebacks are easier to read.
- `docs/` — specs and design docs.

## Backend conventions

- Env config uses the `NEXUS_` prefix (pydantic-settings). Production must reject the insecure
  default JWT secret (enforced by a config validator).
- Reduce external dependencies. Prefer the standard library and what's already vendored.
- Every endpoint is tenant-scoped and RBAC-gated. Never bypass `TenantSession`.

## CI (`.github/workflows/ci.yml`) and what "passed locally" has to mean

**Every job runs Python 3.11, the version the production image runs** (`deploy/Dockerfile`),
since 2026-09-30. CI tested 3.10 while production ran 3.11, so the engagement engine's
`from datetime import UTC` (3.11+) passed every local 3.11 check and failed only on GitHub, for two
pushes in a row. `requires-python` is still `>=3.10`, so write `timezone.utc` (modules that want the
short name bind `UTC = timezone.utc` after their imports), never `datetime.UTC`.

A local run only predicts GitHub when it installs what GitHub installs, and the jobs differ:

| Job | Install / tool | Trap it has caught |
|---|---|---|
| Tests + coverage | `pip install -e ".[dev,migrate,metrics]"`, **no constraints, no `postgres` extra** | asyncpg is absent: `ledger/stores.connect` imported it before the "no DSN" check, so an unconfigured store read "configured". Import optional drivers only once they are needed. |
| Integration | `.[dev,postgres,redis]` against `postgres:16-alpine` + `redis:7-alpine` | |
| Frontend build | Node **20** | |
| Secret scan | gitleaks **8.24.3** via `gitleaks-action`, over the pushed range | stricter than 8.18.4: flagged `idempotency_key=f"populate:{run_id}"`. False positives go in `.gitleaks.toml` by literal value, never by path. |
| Image build + Trivy | `aquasec/trivy:0.65.0`, fixable HIGH/CRITICAL, `.trivyignore.yaml` | runs only after tests pass, so a red test job hides it entirely |

The pre-push run is therefore: those installs in `python:3.11-slim` on a `git archive` of the exact
commit (`--tmpfs /tmp`, `-n 6`, `-o timeout_method=signal` on Docker Desktop), integration on a
Docker network, frontend on `node:20`, gitleaks 8.24.3 over `<before>..<after>`, and Trivy on the
image built with `--build-arg SECURITY_REFRESH=<today>`. The live engagement suite needs GitHub's
secrets and is left to CI.

## Relationship Graph (`nexus/network/`)

A tenant-scoped, deduped graph of each rep's real network (contacts + calendar), used for
NL "who do we know" search (A1) and warm-intro path mapping (A4). Additive subsystem — never
touches Account/Contact/Inbox/Campaigns.

- **Ingestion is provider-based**: `nexus/network/connectors/` — `google.py` / `microsoft.py`
  are real OAuth connectors (Contacts + Calendar, incremental sync, token refresh);
  `fixture.py` is the **offline test double only**, never user-selectable in the product.
  LinkedIn has no compliant live API — it ingests via a member-supplied `Connections.csv`
  export (`nexus/network/linkedin_csv.py`).
- **OAuth security**: `nexus/network/oauth.py` (PKCE + signed short-TTL state JWT) and
  `nexus/network/crypto.py` (Fernet token-at-rest encryption, key derived from `secret_key`
  unless `NEXUS_NETWORK_TOKEN_ENC_KEY` is set). Tokens are never serialized to the client.
- **Resolution/scoring are deterministic, no LLM**: `resolution.py` (identity dedupe by
  normalized email, else name+company) and `strength.py` (0–100 connection strength from
  relationship tier + recency + frequency + reciprocity).
- **Privacy**: pooling is private-by-default per source account; `service.visible_edges_where`
  is the single predicate gating all cross-member reads (search, intro-paths, person lookup).
- API: `nexus/api/routers/network.py` (`/network/...`). Frontend: `frontend/src/pages/NetworkPage.tsx`.
- New provider credentials go in `NEXUS_NETWORK_*` env vars — inert (clear 400) until set,
  never a fake fallback.

## CRM connections (`nexus/ingestion/crm_credentials.py`)

CRM credentials used to be **deployment-global env only** (`NEXUS_CRM_PROVIDER` +
`NEXUS_HUBSPOT_ACCESS_TOKEN`), memoized in one process-wide singleton by `get_crm_connector()`.
So every tenant shared one HubSpot token, a customer could not connect their own CRM, and — the
actual bug — `handle_sync_crm_due_accounts` resolved **one** connector and looped every tenant
with it, pushing tenant A's accounts into whichever portal the deployment env named.

- **`resolve_crm_connector(ts)` is the only thing request and worker paths should call.**
  Precedence: an explicitly installed connector (`set_crm_connector` — the test seam) → the
  tenant's stored credential → `get_crm_connector()`. That third step is why a deployment with
  only env vars set behaves exactly as it did before this existed; tenant credentials are an
  *override*, never a replacement.
- **`crm.py` keeps two globals, not one.** `_connector` memoizes the env-built instance,
  `_override` records a deliberate `set_crm_connector()`. They were one variable, which made
  "is an override installed?" unanswerable — after any `get_crm_connector()` call on an
  env-configured deployment it was non-`None`, so a naive check would skip tenant credentials
  and silently re-create the shared-token bug.
- Tokens are sealed by `crm_crypto.py` (over `core/crypto.py`, mirroring `sources/crypto.py`) and
  appear in **no** response model — `_connection_out` in the router is the single place connection
  state becomes JSON, which is what makes "the secret never leaves the server" checkable.
  An unsealable secret is **tolerated** (like `network/crypto.py`, unlike `sources/crypto.py`):
  it degrades to "reconnect your CRM", a state the admin can fix, and resolution falls back to env
  rather than failing the sync.
- **Salesforce needs its instance URL, and that is a required field rather than an advanced one**
  (2026-09-16). REST is addressed at the org's own host, which only the token response carries, so
  `build_tenant_connector` returns `None` without it. The connect form asked for a token and an
  optional "API base", so a Salesforce workspace stored a credential that could reach nothing.
- **A person pressing Sync never falls back to another CRM.** `resolve_crm_connector` degrades to
  the deployment connector when a tenant's row cannot be built — right for the heartbeat, which must
  never stop collecting, and exactly wrong for a human: the stub accepts everything, so the screen
  reported success while the customer's CRM received nothing. `_reject_unusable_connection` in
  `routers/integrations.py` 400s the request instead, naming the missing instance URL or the
  unreadable secret. That was the whole of "two-way syncs are failing", together with the next line.
- **Test never tests another CRM either, and a 401 says which kind of wrong** (2026-10-02,
  `tests/test_crm_token_paste.py`). Reported from gtm.infojoy.com: "a personal access token created
  just now shows invalid or expired". Four causes, all closed:
  - Test resolved with the fallback, so a stored credential that could not be built was tested AS
    THE DEPLOYMENT'S OWN CONNECTOR, whose stale env token answered "Invalid or expired". Test now
    reports the unreadable credential (or Salesforce's missing instance URL) instead.
  - HubSpot's **Personal Access Key** (for the CLI; easy to find now that private apps sit under
    Development > Legacy apps) 401s against the CRM API. A pasted token not starting `pat-` that
    401s gets a sentence naming that and where the private app's token is; a `pat-` 401 says copy
    it again. The form's hint says the same.
  - A pasted `Bearer ` prefix and whitespace inside the token are removed on save.
  - An "API base" of `app*.hubspot.com` (the web app's address bar) is refused with
    `api.hubapi.com`; regional API hosts (`api-eu1.hubapi.com`) are still accepted.
  - **Test checked only what was SAVED.** Paste a token, press Test without Save, and it tested
    the deployment's env token (`source: env`, the card's "Using deployment default"), whose stale
    value read as "the token I just made is invalid". Test now saves a typed token first; a
    failure with no workspace token says it tested the deployment's default; and "A token is
    saved" appears only when the WORKSPACE has one (the env token was shown as if it were theirs).
- **Both directions are reachable from the screen.** `imports/crm_pull.py` has read accounts and
  contacts from both CRMs since it was written and no screen called either — the Integrations page's
  only Sync button imported hand-typed rows. `CrmPullPanel` wires `POST /imports/accounts/crm` and
  `/imports/contacts/crm`; outbound stays on the heartbeat sweep.
- The resolution cache keys on `updated_at|provider|api_base` and **still reads the row every
  call** — that lookup is how a worker notices a credential the API just changed. It buys instance
  stability (the `MAX_RECORDED_PUSHES` buffers) and a skipped decrypt, not a skipped query. N+1
  pressure is handled by hoisting resolution out of inner loops.

## Migrations

Alembic under `migrations/versions/`. Head: `0072_prospector` (branch `feat/prospect-engine`; on
master the head is still `0071_ledger_erasures`). The chain is
`0020_baseline_schema` (a **frozen, literal-DDL squash** of the old 0001–0020) → `0021`–`0026`
(the Billing tables below) → `0027` (`dead_letter_jobs`, job durability) → `0028` (`user_mfa` +
`mfa_recovery_codes`) → `0029` (`platform_admins.permissions`) → `0030` (`signal_source_runs`) →
`0031`–`0040` (page snapshots, notification preferences, feature flags, contact soft-delete,
`companies`, proration, shared `people`, `crawl_verdict`, user suspension, digest delivery) →
`0041` (`source_databases`) → `0042` (`accounts.next_refresh_at`) → `0043` (`signal_events.subtype`) →
`0044`–`0046` (`provider_keys`, `provider_settings`, `payment_credentials`) → `0047`–`0049`
(`crm_connections`, `audit_log`, `integration_connections`) -> `0050` (`runtime_settings`) ->
`0051`-`0054` (account geo/revenue, signal preferences, `users.token_version`, indexed invoice
PSP references) -> `0055` (`feature_switches`) -> `0056` (`accounts.owner_user_id`,
`notification_preferences.scope`, `alert_channel_rules`) -> `0057` (engagement engine + training
ledger tables, `pending_registrations.training_consent`, `call_tasks.engagement_enrollment_id`) ->
`0058` (`crm_logged_at` on `engagement_messages` and `reply_classifications`) -> `0059`
(`signal_events.dated`, `company_signals.dated`) -> `0060` (`integration_connections.config`) -> `0061` (`placed_calls`,
`tenants.platform_caller_id`) -> `0062` (`web_cache`, platform-global) -> `0063` (LinkedIn
fields on `companies`, `company_countries` and `prospect_cursors`, all platform-global;
`prospect_runs`, tenant-scoped) -> `0064` (`prospect_lists.kind`, `prospect_lists.archived_at`) ->
`0065` (`provider_settings.base_url`) -> `0066` (`page_visibility`, platform-global) -> `0067` (`platform_admins` backfill of
`stripe.manage`, no DDL) -> `0068` (`mcp_connections`, `oauth_clients`, `oauth_codes`,
`oauth_tokens`, platform-global) -> `0069` (`mailbox_connections.auto_actions` and
`.auto_send_replies`, `reply_classifications.auto_reply` and `.auto_reply_note`) -> `0070`
(`payment_credentials.payment_method_configuration`, platform-global) -> `0071` (`ledger_erasures`,
platform-global) -> `0072` (`prospector_pages` platform-global; `prospector_purchases`,
`prospector_daily_rows` tenant-scoped). Every tenant-scoped table gets RLS via
`scripts/apply_rls.py` on deploy — no manual policy work needed for new tables.

**Two feature branches both claimed 0044–0046 and merging them produced two alembic heads**, which
`upgrade head` refuses to run against. Both had branched from `0043_signal_subtype`. Resolved by
renumbering one chain to `0047`–`0049` and rebasing it onto the other's head, rather than adding a
merge revision — neither had been applied anywhere, so no stamped database remembered the old ids.
If you branch for more than a day, check `ScriptDirectory.get_heads()` returns exactly one before
merging; the collision is invisible until a deploy. **It happened again, 2026-09-24:** `feat/self-hosted-signal-fetching` carried `0057_web_cache` on
the same `0056` parent as `0057_signal_dated`. Resolved the same way: merged last, it was renumbered
to `0060_web_cache` and re-parented on `0059_calling_billing`.

**It happened again, found 2026-09-24 before anything merged.** `feat/sdr-engagement` (`0057_engagement`,
`0058_engagement_crm_log`) and the stacked `fix/signals-credits-telephony` →
`feat/self-hosted-signal-fetching` → `feat/linkedin-prospecting` chain (`0057_signal_dated` …
`0061_linkedin_prospecting`) both start from `0056_alert_routing`. Nothing of either had been applied
anywhere (local deploy at `0056`, neither pushed). The engagement chain lands first; the other chain
re-parents `0057_signal_dated` onto `0058_engagement_crm_log` and renumbers to `0059`–`0063` when it
rebases, the same fix as above. **Done 2026-09-28**: the local deploy was by then at
`0058_engagement_crm_log` and nothing of the other chain had been applied anywhere, so it was
the one renumbered (`0059_signal_dated` … `0063_linkedin_prospecting`).

Migrations are **additive only**, and the chain **is** replayable onto an empty database —
`tests/test_migrations_replay.py` builds one from nothing but `alembic upgrade head` and diffs
the result against `Base.metadata`. Do not reintroduce a `create_all()` inside a revision: the
old `0001_initial` did that, so it materialized whatever models existed *at run time* rather
than a frozen historical schema, and the chain could never be replayed. Production still runs
`bootstrap_db.py` → `alembic upgrade head` → `apply_rls.py`.

### Three traps that only bite against real Postgres

The suite runs on SQLite, which has **no RLS** and ignores `varchar` lengths. These pass every test
and fail in prod:

- **A revision id must be 32 characters or fewer.** `alembic_version.version_num` is `varchar(32)`
  on Postgres. `0070_payment_method_configuration` (33) passed the whole suite and stopped the app
  starting on the first real deploy (2026-10-14); it is now `0070_payment_method_config`, and
  `tests/test_migrations_replay.py` refuses a longer id.

- **A hand-built `TenantSession` must call `apply_rls(session, tenant_id)` first**, or writes are
  rejected. `tests/test_rls_binding_guard.py` is an AST guard against this.
- **Genuinely cross-tenant reads return ZERO ROWS, not an error**, under the app's RLS-bound
  role. Use `get_platform_sessionmaker()` (owner role) for those — the staff console and the
  payment webhook both need it — and only behind `require_platform_admin` or signature
  verification.

Also: `apply_rls` sets the tenant GUC **transaction-locally**, so a read after `commit()` has no
binding and silently returns nothing.

## Billing & Entitlements (`nexus/billing/`)

A commercial operating system: any capability can be priced, quota'd, or gated **without
touching application code**. Designed in `docs/billing/` (19 docs); milestone plans in
`docs/superpowers/plans/2026-07-28-billing-m*.md`.

- **One seam.** Application code calls `check_and_meter(ts, capability_id=...)` — or the
  ergonomic `metered()` context manager — and never mentions a plan or a price. Plans, quotas,
  and prices are rows, not branches.
- **Regression-proof by construction.** Unknown capability → allow. Tenant with no subscription
  → allow. Engine raises → allow. Default `NEXUS_BILLING_ENFORCEMENT=shadow` evaluates and
  records but never blocks; `off` is a full kill switch. Every pre-existing tenant maps to the
  `legacy-unlimited` plan.
- **Config, not constants.** `catalog.py` (capabilities), `plans.py` (plans + entitlements),
  `rates.py` (prices + COGS). Seeds run on startup and never overwrite a live value — once
  shipped, pricing belongs to Admin, not to a redeploy.
- **The margin floor is enforced, not aspirational.** `rates.validate_rate()` refuses any price
  below 50% gross margin unless finance records an explicit exception, and it runs on the seed
  itself so a bad price cannot reach the database.
- **Costs are editable too, and recording one is NEVER refused** (`PUT /admin/billing/costs/{id}`).
  That asymmetry against the price endpoint is the point: a price is a *decision*, and deciding to
  lose money should be explicit; a cost is an *observation* about what a provider charges. Refusing
  to record a vendor price rise would leave the floor validating against a stale number and
  reporting a healthy margin — which is exactly what happened, measured 2026-08-25: `search.web`
  carried a $0.004 "blended" cost while we were paying Exa $0.007, and sat at 30% margin with
  nothing complaining. The response instead returns a **work list** of every capability the change
  pushed under the floor, across the whole catalog rather than the one edited, because one provider
  price change can move several that share the input. The UI writes cost **before** price for the
  same reason: the true number lands even if the reprice is then rejected.
- **Usage is an append-only event stream**; rollups are derived and rebuildable. Quota reads =
  period rollup + events still marked unrolled, so they stay exact between sweeps and degrade to
  slower — never to undercounting — if the rollup worker stops. Corrections are compensating
  negative rows, never deletes.
- **Platform admin is separate from tenant RBAC, and is per-permission.** Membership (env
  allowlist or the `platform_admins` table) fails closed; no tenant role grants it. Every endpoint
  names the one permission it needs via `require_platform_permission(...)` — the nine names live in
  `nexus/billing/permissions.py`. `platform_role` is only a shortcut for granting a set: the
  **expanded** set is stored on the row, so redefining "support" tomorrow cannot silently re-grant
  power to people provisioned today. An empty `permissions` list falls back to the role preset,
  which is what makes migration `0029` backfill-free. `require_platform_admin` is retained but now
  means `billing.read`, not "any active row" — as a flat gate it let a support admin reprice every
  plan. The env allowlist deliberately keeps **full** power; narrowing it would reintroduce the
  bootstrap lockout it exists to prevent. Credit grants are the one amount-dependent check
  (`credits.grant.capped` up to `NEXUS_BILLING_SUPPORT_CREDIT_CAP`), so they are checked in the
  body rather than a `Depends`. Every admin mutation is captured in `billing_audit_log` with
  before/after snapshots.
- **Money flows through one seam, at ONE price.** `metered()` → burn the rate card → block when
  the balance cannot cover it. The four-step ladder this used to be (included quota, then credits,
  then an explicit overage price, then the wall) gave the same action up to three prices depending
  on where in the period it landed, so "what does this cost?" could not be answered without knowing
  your own running total — and the last two steps charged the same overage twice, once to the
  ledger at the moment of use and once to the invoice at close. That double charge was observed
  against Stripe. `rate_period` now invoices **gauges only**: seats and stored bytes are levels
  rather than requests, they never draw on credits, and they are the only thing left with a period
  figure an invoice can legitimately price. `_burn_for_overage` and the two agreement test files
  that checked the ledger and the invoice charged the same amount are gone — there is no second
  charge left to agree with, which is a stronger property than the agreement was. Collection is
  keyed by invoice id at the provider, so a retry can never double-charge. Dunning
  (`nexus/billing/dunning.py`) retries on a config schedule and escalates to `past_due`; it never
  silently voids a debt.
- **Payments are a provider seam** (`payments.py`): `noop` by default so the whole lifecycle runs
  offline; `stripe` is inert until keyed and raises rather than faking success. Webhooks verify
  an HMAC over the **raw** body, enforce a freshness window, and dedupe on the provider event id
  as a primary key.
- **Two payment paths, on purpose** (M12). Self-serve uses hosted **Checkout + Customer Portal**
  (`POST /billing/checkout`, `POST /billing/portal`) so card data never touches us and PCI scope
  stays at the provider; the resulting subscription arrives via webhook, never a self-write.
  Enterprise stays admin-driven (custom plan + `collect_invoice`) and is refused by the portal
  endpoints — a `plan_class == "custom"` tenant gets a 409.
- **Stripe drives subscription state** (M12): `checkout.session.completed`,
  `customer.subscription.created|updated|deleted`, `invoice.paid|payment_failed|finalized`.
  Statuses map onto the existing `SUBSCRIPTION_STATUSES` via `STRIPE_SUBSCRIPTION_STATUS` —
  never extend that vocabulary, or rating and entitlements can no longer reason about it.
  `unpaid` → `past_due` (keeps the debt visible); `incomplete` is **deliberately unmapped**
  because the subscription never started, and guessing would either cancel a live customer or
  activate one who has not paid. Unmapped statuses leave our row untouched.
- **Reconciliation reports, never repairs** (`nexus/billing/reconcile.py`). A missed webhook is
  otherwise invisible until a customer complains. It skips subscriptions with no
  `psp_subscription_id` (enterprise deals never had a provider object) and ignores unmapped
  statuses, so real findings are not buried in noise. Which side is right depends on what the
  customer agreed to — an automated writer would resolve that wrongly and destroy the evidence.
  It runs once per `billing_reconcile_interval_hours` (24; decided 2026-10-14): it asks Stripe
  about every paying subscription, and it used to do that on every heartbeat tick.
- **Gauges are not counters.** `seat.member` resolves to live membership count; summing events
  would only ever climb, so a customer could never get back under a seat limit.
- Platform-global tables carry no `tenant_id` and no RLS policy: `billing_capabilities`,
  `billing_plans`, `billing_plan_entitlements`, `billing_rate_cards`, `billing_cost_rates`,
  `platform_admins`, `billing_audit_log`, `billing_webhook_events`. The audit and webhook tables
  deliberately name their tenant column `subject_tenant_id` so `apply_rls.py` — which enrolls any
  table having `tenant_id` — does not hide them from the operators who must read them.
  `dead_letter_jobs` (below) follows the same rule for the same reason.

## The plan ladder (`nexus/billing/plans.py`)

**Free / Launch / Accelerate, plus 20%-off annuals.** Collapsed from eight public tiers on
2026-08-26. Sized against measured cost — blended $0.00192/credit, worst case $0.00400 — so the
whole ladder sits inside the 100 credits-per-dollar design ceiling (survival limit is 250; see
`docs/PRICING-AND-PACKAGING.md`).

| Plan | Price | Credits | cr/$ |
|---|---|---|---|
| Free | $0 | 200 | — |
| Launch | $99/mo | 2,000 | 20.2 |
| Launch (annual) | $950/yr | 24,000 | 25.3 |
| Accelerate | $199/mo | 8,000 | 40.2 |
| Accelerate (annual) | $1,910/yr | 96,000 | 50.3 |

**`plans.py` is authoritative and this table drifted from it.** It read 1,000 / 2,500 / 30,000 —
the sizing before the ladder was resized on 2026-08-27 — while the code had already moved. A stale
number here is worse than none: it is what somebody checks instead of reading the seed.

**Free is 200 on purpose: a trial of the whole product, not a usable tier.** At the worst-case
$0.004/credit that caps a free workspace at $0.80 of COGS, which is what makes an open signup funnel
safe at any volume. It buys roughly 25 enriched accounts or 66 research briefs — enough to see the
product work on the buyer's own data, not enough to run a territory on.

**A new workspace starts on `free`, attached by `start_subscription` in the same transaction that
creates the tenant** (`/auth/signup`, `/auth/workspaces`, and the OTP verify path). Before that
existed, tenant creation attached no plan at all, the engine's "no subscription → allow" default
granted everything, and the startup backfill then grandfathered the tenant onto `legacy-unlimited`
permanently — observed in production on every workspace of a deployment, including ones created days
after billing shipped. `backfill_subscriptions` is now bounded to tenants older than the earliest
`billing_plans` row, so it can only ever claim the pre-billing tenants it was written for.

**Superseded tiers are RETIRED, never deleted, and the distinction is not cosmetic.**
`billing_subscriptions.plan_id` is a foreign key, and entitlements resolve from the plan ROW rather
than from whether it is on sale — so deleting a plan with subscribers is either a constraint
violation or a paying customer with *no* entitlements at all, which falls back to permissive catalog
defaults and hands them everything. `status="retired"` takes a plan off `GET /billing/plans` (which
filters on `active`) and leaves its subscribers exactly where they are.

The seed marks them retired so a **fresh install** gets the current ladder; `sync_plans` never
mutates an existing row, so an established deployment is moved by `scripts/restructure_plans.py`
instead — idempotent, with a dry run that prints subscriber counts before anything is written.

**`free` is on the price list but refused by checkout** (`UNPURCHASABLE_PLAN_CLASSES`). A price list
that hides the free option is a paywall with a gap; a $0 hosted checkout would create a Stripe
product for something that never charges anyone and put a card form in front of a downgrade.

Two things that were wrong and are now pinned by tests:

- **Ladder positioning must ignore retired plans.** `_position_on_ladder` read every `standard` row,
  and a retired tier's `sort_order` describes a ladder that no longer exists. Measured: with five
  retired tiers still counted, a $149 plan landed *after* the $199 one.
- **Overage must never undercut the cheapest in-plan rate.** It was 1c against in-plan rates of
  2.48–4.75c, so overflowing was two to five times cheaper than upgrading — a customer acting
  rationally would sit on the smallest plan forever.

**One card per tier, and the pairing is the server's.** An annual plan is its own row, so nothing in
the schema said `launch-annual` is Launch billed yearly, and `PlanPicker` rendered five cards for
three tiers. `GET /billing/plans` now returns `family` and `counterpart_id`, derived **on read** by
`plans.interval_pairs` (a seeded column would never reach an established deployment, because
`sync_plans` never mutates a row). The rule: `X` (month) pairs with `X-annual` (year) when both are
listed in one currency. Anything else is its own family and shows in both views at the one interval
it sells, labelled as such; pairing runs over listed rows only, so a held monthly tier leaves its
annual unpaired rather than pointing a card at a plan checkout would refuse.

- The picker opens on **Annual**, except for a workspace already paying for a paired tier, which
  opens on the interval it pays so its Current card shows the price it is actually charged.
- Savings, extra seats and extra credits are computed from plan fields in `planFamilies.ts`, never
  written down; `tests/test_plan_picker_ui.py` refuses price literals. Credits per month are equal
  on both intervals, so no "more credits" line appears unless an admin makes it true.
- **Annual credits: the whole year at purchase, the next year when Stripe is paid for it.** The
  webhook takes a bought plan through `take_plan_terms`, so `sub.interval` is `year`; the period is
  Stripe's, read in any API version (`_in_pinned_shape`); and a renewal is credited only on
  `invoice.paid` for `subscription_cycle` (see *A Stripe renewal is credited...*). Verified
  2026-10-21 by paying a test-mode annual Checkout: 24,000 credits, period end a year out. (This
  line used to say the webhook left `sub.interval` behind; that was fixed with `take_plan_terms`.)

## Authoring a capability (`nexus/billing/capability_authoring.py`)

`CAPABILITY_SEED` in `catalog.py` was the **only** write path to `billing_capabilities` — the table
was always writable, the API was the missing half — so a new billable action meant a code change and
a release. `POST /admin/billing/capabilities` closes that.

Safe to add because of two existing properties of the seed: `sync_catalog` **never deletes**, and
`_MANAGED_FIELDS` re-asserts `category`/`unit`/`depends_on` only for ids the seed names. An
admin-created capability is invisible to it, so no later deploy can revert or clobber one. Pinned by
`test_a_redeploy_does_not_disturb_an_admin_created_capability`.

**A retired capability is switched off, never deleted** (`catalog.RETIRED`, 2026-10-14).
`outreach.cadence_touch` and `report.cadence` priced the Cadences screens the engagement engine
replaced, and nothing has metered them since. `sync_catalog` and `sync_rates` set them and their
rate cards inactive, one way: no deploy turns them back on, and the rows stay for the usage history
that names them.

- **Pricing is offered in the same call, and its absence is warned about.** A capability with no rate
  card is metered and then *rated at nothing*: usage accumulates, quotas count down, no revenue line
  appears. It looks handled. That shipped once — `ai.scoring`, 4,090 runs, free. A `module.` gate is
  the one case that legitimately has no unit price and gets no warning.
- **`depends_on` is validated against existing rows.** Naming a capability that does not exist gates
  this one behind something unresolvable: permanently unusable, and silently.
- The id must be `category.name`, lowercase. It appears in URLs, entitlement rows, usage events and
  invoice lines, and the Admin UI groups on the prefix.

## Token metering (`nexus/billing/tokens.py`)

`ai.tokens` was priced and metered at no call site. It now records alongside the flat per-action
charge in `routers/agents.py`, **not instead of it**: the flat rate is the bill and is what makes it
predictable, this is the evidence that the flat rate is still the right one.

Flat pricing is defended by measurement rather than assumed. Across 4,174 real agent runs, token
spread within one agent is up to 49x min-to-max but only ~4x **median-to-max**, which these margins
absorb — but that is a fact about today's prompts, and prompts change. Called outside the `metered`
block, because a token record must not be part of the transaction that decides whether the customer
was allowed to run the agent at all. Never raises.

## Runtime configuration (`nexus/runtime_config/`)

Deployment settings changeable from the Control plane without a deploy. **The mechanism is one
line:** `get_settings()` is an `lru_cache` over a *mutable* object, so an override applied with
`setattr` reaches all 142 call sites without any of them changing. The alternative — a resolver each
call site adopts, as `providers/resolver.py` does for keys — would have been 142 opportunities to
miss one, and missing one is invisible: the toggle reads "off" and the feature keeps running.

**The catalog is an allowlist and its exclusions carry more weight than its inclusions.** Named in
`FORBIDDEN`, not merely absent, so the reason survives and the refusal says the setting is withheld
on purpose rather than mistyped:

- `source_db_allow_private` — the SSRF guard. An admin must not switch off a guard from the form the
  guard protects.
- `security_headers_enabled`, `auth_rate_limit_enabled` — the same argument aimed at the web surface
  through the exact interface it defends.
- `demo_signals_enabled` — fabricated signals in a real inbox is what `nexus/ingestion` was rebuilt
  to prevent.
- `billing_seed_on_startup` — startup-only, so a control for it would be a lie.
- `env`, `secret_key`, database URLs, every `*_enc_key` — identity and cryptographic roots.

Rules that are load-bearing:

- **Every entry states its `effect`, and anything medium or high risk states its `warning`**, asserted
  by test. A toggle whose result nobody can state in a sentence is a trap.
- **Values are coerced against the declared type.** The panel posts JSON, so a boolean arrives as the
  string `"false"` — which is truthy in Python and would switch a setting ON while the control read
  "off".
- **A row whose key has left the catalog is skipped, not applied.** Otherwise removing a setting from
  the panel leaves it taking effect from a row nobody can see.
- **`apply_overrides` never raises.** A config read that fails must leave the process on the values it
  already had; a database blip must not take down an application whose settings were fine a second
  ago. Wired into the API lifespan and into worker `dispatch` on a 30s TTL.
- **`in_effect` is reported separately from `overridden`.** "Saved" and "in force" are different facts,
  and a panel showing only the first is how an operator concludes a feature is on when it is not.

**Note the shape of the one bug this shipped with**: the lifespan block logs what it applied, and
that line only runs when an override *exists*. A `NameError` in it (there is no module-level
`logger` in `main.py`) survived every deploy until the first real override was stored, then refused
to boot — a config helper whose entire contract is "never take the process down" taking the process
down, at startup.

**`admin_ip_allowlist` lives outside `Settings`** (`api/deps_ip.py`), because pydantic refuses an
attribute `config.py` has not declared — so it keeps its own module cache and its own reader,
registered in `_EXTERNAL_SINKS`. Keep that map small: a growing list means the override mechanism is
being worked around rather than used. Empty means open (default-closed would lock every existing
deployment out of its own panel on upgrade), at most two entries, and a malformed list is ignored
rather than enforced — the only place to fix a bad allowlist is the panel it would have closed.
Validation runs **before** the row is written: `coerce` only checks the declared kind, which for a
string is no check at all, and a committed-then-rejected value is an override the panel reports as
active and nothing ever applies.

**Two structural guards against "saved, applied nothing"** (`tests/test_runtime_control_plane.py`),
both added 2026-09-15:

- **Every catalog key is read outside `config.py`**, directly or through a `Settings` property. It
  found `phone_enrich_auto` and `calling_enabled` on its first run: both switches had been in the
  panel, reporting "in effect", with nothing reading either. They were removed (the calling module is
  gated by `module.calling` under Feature switches). A value copied once into a long-lived object
  counts as read and still does nothing, so it also needs an `_ON_CHANGE` hook or a per-call reader:
  `DorkedSearchSource` now reads `signal_dork_max_queries` on each fetch for that reason.
- **Every free-text setting has a validator in `_VALIDATORS`**, run before the row is written.
  `email_verify_url` refuses non-http(s), no host, a bad port, and (outside local/test) any host
  resolving private, loopback or link-local, plus metadata names always: the verifier POSTs there
  and Check connection reports how it answered, which is a port scanner otherwise.
  `billing_dunning_schedule_days` exists because `dunning._schedule` silently falls back to the
  default on a bad value.

**Grouping lives on the server.** `catalog.GROUP_ORDER` orders the groups and `_SPECS` declaration
order is the reading order inside one; `current_values()` sorts by both and the panel never re-sorts.
`option_labels` and `placeholder` ride on each row. The panel's input follows `kind`: it used to draw
a number box for every non-switch setting without options, so the IP allowlist and `apify` could not
be typed at all.

**Email verifier health** (`nexus/verification/health.py`) is one check used three ways: the
`email verifier` row on Platform health, a WARNING in the API lifespan and worker boot log
(`EMAIL VERIFIER UNREACHABLE`, scheduled in the background, never blocking), and
`POST /admin/runtime/email-verifier/check` behind the panel's Check connection. It POSTs an EMPTY body,
so no mailbox is probed. What proves Reacher answered is the SHAPE, not the status: the live instance
(158.69.113.104:8080, measured 2026-09-15) defaults the missing `to_email` to "" and answers **200**
with a full verdict (`is_reachable: "invalid"`, failed syntax, no MX lookup); other versions answer
400/422. A 200 without `is_reachable` is something else on that URL and reads degraded. The first
version treated every 200 as "not Reacher", which would have flagged the working verifier. When Reacher cannot
answer nothing fails; addresses grade DNS-risky or unknown, which staging did for days. The worker now
applies overrides at boot (`refresh_if_stale(force=True)`) so the check sees the URL in force.
`email_verify_auth_header` is `FORBIDDEN` because the panel returns values in plaintext.

**`phone_lookup_provider`** (`apify` | `off`, default `apify`; only the exact word `off` disables).
`find_phone` returns `disabled` after the shared record and source database and BEFORE the actor, and
records nothing: a recorded `not_found` is never re-purchased, so recording one while off would blank
that person's number for every tenant. Nothing bought, nothing metered; answers already bought are
still served and charged as today. The `phone lookup` health row reports off, unkeyed, or an actor
needing per-account approval.

`GET /admin/runtime/webhook` shows what to paste into Stripe and which events to select; the URL is
not ours to set and nothing here can set it. `POST .../webhook/test` posts a correctly signed event
at our own endpoint — it proves the signing secret verifies and the route is live, and deliberately
does **not** claim Stripe can reach the host, which depends on DNS and firewalls outside the process.

**Which provider key is live** is computed in `routers/admin_provider_keys.py` from the *same*
ordering the resolver uses — pinned first, then oldest, enabled only. A separate flag on the row
would be a second source of truth, and the first thing to drift would be exactly the fact the
indicator exists to report.

## Authoring a sellable plan (`nexus/billing/plan_authoring.py`)

A ninth public tier used to need a `plans.py` edit and a deploy. `CustomPlanDialog` could build a
bespoke per-tenant deal, but `plan_class="custom"` is excluded from `GET /billing/plans` and refused
by checkout with a 409 — so there was no path from "we want to sell a new tier" to a customer buying
it. `POST /admin/billing/plans` closes that; `PUT .../status` publishes or holds.

- **`plan_class` is decided by the service, never by the body** (`extra="forbid"`). An endpoint that
  accepted one would mint an `unlimited` or `internal` plan by typing a string — the migration
  keystone and the staff tier. Publishing a non-standard plan is refused for the same reason, as
  are the reserved ids (`free`, `enterprise`, `legacy-unlimited`, anything `custom-`).
- **Draft by default, and `draft` is also the hold.** A held plan leaves the price list and existing
  subscribers are untouched, because entitlements resolve from the plan row rather than from what is
  on sale. That is the difference between holding and retiring, and `retired` is deliberately not
  reachable from the status switch.
- **Entitlements are cloned from a base plan, never empty.** `resolve_entitlement` falls back to
  permissive catalog defaults for anything a plan does not list, so a blank new tier would silently
  grant nearly everything. `clone_entitlements` is shared with `custom_plans` — a second copy would
  drift, and the first thing to drift would be *which fields* get carried, invisible until a customer
  is on the wrong quota.
- **The margin check warns, it does not block**, unlike `rates.validate_rate`. A rate card below cost
  loses money on every call; a plan priced under the cost of its own credits is a normal commercial
  decision, and a hard floor would refuse the `free` tier that already exists.
- **A draft creates nothing at Stripe; putting a plan on sale creates its price** (2026-10-09). A
  draft nobody publishes does not litter the Stripe account. Publishing, or editing a plan that is
  on sale, runs `stripe_catalog.apply_plan_to_provider` before the commit: a price the plan needs
  and Stripe refuses is a 502 and nothing changes, while archiving on hold and renaming the product
  only warn. See *Stripe-managed plans and subscriptions* below.

**A new monthly plan brings its annual plan, at a chosen % off** (2026-10-12, `annual_discount_pct`,
`tests/test_plan_annual_twin.py`). The price list sells a tier as two rows, `X` and `X-annual`,
paired by name into one card; a plan made in Admin had no annual unless someone made a second plan
by hand with exactly the right id, price and credits. Creating a monthly plan with the option
(the dialog ticks it by default, 20% like the seeded ladder) makes `<id>-annual` in the same
transaction, same base, entitlements, seats, trial and status:
- **price and seat price** are twelve months less the discount, to the nearest whole unit, halves up
  (`annual_price_cents`, integer arithmetic; `PlanDialog.annualCents` is the same expression, so the
  preview is the price the plan is made at). $99 at 20% is $950, the seeded Launch annual.
- **credits are exactly twelve months'**: the discount is on price, never on credits, as the seed
  does, or credits per dollar compound past the design ceiling.
- sorted beside the monthly (`sort_order + 1`); `meta.annual_of` and `meta.annual_discount_pct`
  record where it came from.
- the annual id taken, too long to keep its `-annual` suffix, a yearly plan, or a discount outside
  0-90 refuses the WHOLE create: half a pair is what this prevents. Created on sale, both get their
  Stripe price and are fitted to the price list monthly first, so the annual joins the monthly's
  card or both are hidden together. Afterwards they are two plans, edited and published separately.
- **the annual price can be typed instead** (2026-10-21, `annual_price_cents`): the dialog's Annual
  price is worked out from the discount until someone types one, then the discount shows what that
  price comes to (`implied_discount_pct`, the same integer expression in `PlanDialog.impliedPct`),
  which the twin records and its seat price takes. Between 0 and twelve monthly payments, at most 90%
  off, or the whole create is refused. Credits stay twelve months' worth.

**The Plans tab is where this is used** (`admin/PlanDialog.tsx`, 2026-10-01). For five weeks the
server half had no screen: the tab offered only "What's included", the API client carried an
`updatePlan` nobody called, and an operator reported "I can view plans but cannot edit or add one"
as a regression. "New plan" creates a draft cloned from a chosen plan; "Edit" PATCHes only the fields
that changed and never `status`; "Publish"/"Hold" use `PUT .../status` and appear only on standard
plans in draft or active, the one case the server allows. New plan, Edit and Publish/Hold need
`stripe.manage` (superadmin only, since 2026-10-09, because each one writes to Stripe); "What's
included" stays on `pricing.write`. In Subscriptions the plan change sat behind "Manage"; it reads
"Change plan" for `subscriptions.write`.
`tests/test_plan_authoring_ui.py`.

**What the customer price list shows is the superadmin's choice, at most four cards**
(`nexus/billing/price_list.py`, 2026-10-09, `tests/test_price_list.py`). Asked as "show or hide which
plans are visible, not more than 4", together with "a plan changed in Admin does not reach the billing
page". The second had two causes and neither was the server: the picker opens on **Annual**, so a
monthly price edit was invisible behind the tier's annual plan (a separate row at its own price; the
edit dialog now names it), and the page read the list once (it now re-reads when the page comes back
into view, at most every 5 seconds, and keeps the cards on screen while it does).

- A card is a tier as PlanPicker draws it: a monthly plan and its `-annual` twin are one card, so the
  seeded ladder is three. Pairing runs over the LISTED rows, so hiding a monthly leaves its annual a
  card of its own and cannot sneak a fifth past the limit.
- **Hiding is presentation, not a hold.** A hidden plan stays on sale (subscribers, console Checkout
  links, checkout by id); Hold is still how a plan stops being sold. The flag is
  `meta.price_list_hidden`; no flag means shown, so nothing moved when it shipped and `sync_plans`
  cannot undo a choice.
- Showing a fifth card is a 409; **publishing is never refused for it**: a plan that would make a
  fifth card goes on sale hidden and the response's `note` says so.
- `PUT /admin/billing/plans/{id}/price-list` (`stripe.manage`, audited `plan.price_list`). The Plans
  tab shows "N of 4 cards" and a Shown/Hidden box per listable plan; `PRICE_LIST_LIMIT` in
  `AdminBillingPage.tsx` is pinned to `MAX_CARDS` by test. The picker's grid takes one column per
  card (4-up, 2x2 below 1200px, one column on a phone), and with no tier sold both ways it opens on
  the interval the list actually sells instead of labelling every card "Only sold with monthly
  billing".

**A changed price is the price checkout charges** (2026-10-02, found while adding Edit). Checkout
cached a plan's Stripe price in `meta.price_id` on first purchase and reused it whenever one existed,
so a price changed in Admin kept charging the OLD amount at Stripe with nothing reporting it. The
cache now also stores the terms it was made for (`price_amount_cents`, `price_currency`,
`price_interval`) and is reused only while they match; otherwise `ensure_plan_price` mints a new
Stripe price (its idempotency key carries the amount, and Stripe prices are immutable). A cache
written before this has no terms and is re-ensured once. Customers already paying through Stripe keep
their subscription's price: changing it is a plan change, not a plan edit, and the Edit dialog says
so.

`sort_order` defaults to a function of price, so a new tier lands in the right place without the
operator knowing what the number means. An **annual** plan is a separate row with `interval="year"`,
and its auto-position is derived from the yearly price — set `sort_order` explicitly to sit it beside
its monthly sibling.

## Payment credentials (`nexus/billing/credentials.py`) — its own table, on purpose

Stripe is **deliberately not** in `providers/catalog.py`, and the reason it gave is the design brief
for this module: **money fails silently.** A dead search key returns no results and somebody notices
within a day; a wrong Stripe key stops checkout and stops invoices being raised, which is
indistinguishable from a quiet month until a customer asks why they were never charged.

So `payment_credentials` (migration `0046`, no `tenant_id`) carries rules the generic key pool does
not:

- **Verification is mandatory before activation.** `activate_credential` refuses anything not
  `verified`; there is no add-and-see path for money. The endpoint returns **409**, not 400 — the
  request is well-formed and the row exists, it is the *state* that forbids it.
- **Verification reads `/v1/account` and stores the account name and `livemode`.** A key that merely
  authenticates is not enough: authenticating against the **wrong business** is the expensive
  mistake and it looks exactly like success. Test and live keys are the same shape.
- **Exactly one active credential.** No rotation pool — you cannot ride out a bad Stripe key by
  trying the next one, and two accounts both collecting money with no rule about which is worse than
  an outage, because it is an outage you cannot see.
- **The webhook signing secret is the active credential's own, never the environment's**
  (decided 2026-10-14, `resolve_stripe_secrets`). A secret belongs to one account's endpoint, and the
  environment's may be the account before a switch: borrowing it refused the active account's
  events and accepted the old one's. With no secret of its own the credential verifies nothing and
  says so in the log; Create webhook in Stripe stores the right one.
- **A failed re-verification also deactivates.** Leaving a now-broken credential live because it
  passed last month is how a silent billing outage lasts a month.
- The active credential **refuses deletion**; deactivation is **never** refused, because during an
  incident "stop taking money through this account" must not be blocked by a state machine.

`resolve_payment_provider()` is async with a **30s TTL**, mirroring `providers/resolver.py` — the
worker is a separate process, so without a TTL a credential change would need a restart. It falls
back to the environment on any failure, which is also what makes the table additive: a deployment
that never opens the screen behaves exactly as before. `get_payment_provider()` stays for the
synchronous callers and for `set_payment_provider` test injection, which always wins.

**Four operator gaps, fixed 2026-09-30** (`tests/test_stripe_operator_fixes.py`):

- **The Payments tab selects Stripe on its own.** `resolve_payment_provider` returned `noop` unless
  `NEXUS_PAYMENT_PROVIDER=stripe` was ALSO in the deployment env, so an operator could verify and
  activate a key and every checkout still ran offline. An active (hence verified) credential now
  selects Stripe; the env var alone still does too, so no deployment that set it changes. The
  module keeps `_override` (the `set_payment_provider` test seam) apart from the memo, as
  `ingestion/crm.py` does. Platform health and the webhook panel report the provider in force.
- **Cancelling in the console cancels at Stripe.** `subscriptions.cancel_subscription` calls
  `provider.cancel_subscription` FIRST (period end: `cancel_at_period_end=true`; now: `DELETE`) and
  changes our row only if Stripe agreed. A refusal is a 502 that says nothing changed here; a `sub_`
  subscription with no Stripe credential in force is a 409 (cancelling locally would stop nothing).
  Admin-managed subscriptions (no `psp_subscription_id`) cancel here only, as before.
- **No plan switching in the customer portal.** We learn the plan only at Checkout, so a price
  changed in the portal was charged while the workspace kept the old plan. Every portal session now
  names NEXUS's own configuration (`PORTAL_CONFIG_TAG`, found by metadata or created once per
  account): cards, invoices, address and at-period-end cancellation on, `subscription_update` off.
  If it cannot be created the portal does not open: failing closed beats charging the wrong price.
- **The panel tells the truth.** `webhooks.HANDLED_EVENTS` (17 since 2026-10-12) is what the webhook panel lists; it
  summed two of the three tuples and showed 9, and a test checks every `event_type ==` branch is in
  it. Live/Test comes from the key prefix (`credentials.stripe_key_mode`: `sk_`/`rk_` x
  `live_`/`test_`); it was "charges enabled and not `sk_test_`", so a restricted test key read Live.

`stripe_publishable_key` is read with `getattr` — Settings has no such field, because the publishable
key has never been needed server-side.

## Stripe-managed plans and subscriptions (`nexus/billing/stripe_catalog.py`, `stripe_subscriptions.py`)

Reported 2026-10-09 as "product id not available" after a new Stripe credential was added.
Production had moved from a test key to a live key and staging to a new test account, and checkout
reused the price, product and customer ids cached in `billing_plans.meta` and
`billing_subscriptions.psp_customer_id` without knowing which account made them, so the new account
answered "No such price". Spec and plan: `docs/superpowers/{specs,plans}/2026-10-09-stripe-managed-plans*`.

- **An id is reused only inside the account it was made in.** `meta.price_account` and
  `sub.meta.psp_customer_account` record it (`provider.account_id()`, cached per key). A cache from
  before that is asked about once (`object_exists`; a 404 means another account). Ids from another
  account move to `previous_stripe` / `previous_psp_customers`, never deleted. `plan_price_id` and
  `customer_id_for` are the only readers; checkout and `collect_invoice` both go through them.
- **NEXUS is the catalog** (decided with the product owner). Activating a credential as a
  superadmin runs `sync_plans`, which creates every plan on sale in that account and returns
  created / reused / failed per plan; "Sync to Stripe" on the Plans tab runs it on demand. Within one
  account a plan keeps one product, and a changed amount, currency or interval makes a new price and
  archives the old one (subscriptions on it keep billing).
- **Everything that writes to Stripe needs `stripe.manage`**, a tenth platform permission held only
  by the `superadmin` preset (migration `0067` backfills rows holding every earlier permission,
  through `permissions.widened_with`; a narrowed superadmin stays narrowed). Finance keeps
  `pricing.write` for rate cards and "What's included", and loses plan create, edit and publish.
- **A superadmin starts a workspace on Stripe** (`POST /admin/billing/tenants/{id}/stripe-subscription`,
  Customers tab dialog): an emailed Stripe invoice (`collection_method=send_invoice`, 14 days to pay
  by default, 1-90, to the owner unless another address is given) or a Checkout link (nothing changes
  until the customer pays; the existing webhook applies it). Refused for a workspace already live on
  Stripe, for plans with nothing to bill, and with no Stripe credential in force (409).
- **Invoiced credits arrive when the invoice is paid** (decided with the product owner). The
  subscription carries `metadata[nexus_credits]=on_payment`, the webhook records
  `meta.credits_pending_plan` instead of granting, and `invoice.paid` grants it once. Card
  subscriptions are unchanged.
- **A plan change for a Stripe-billed workspace happens at Stripe, now, prorated**
  (`change_stripe_plan`, `proration_behavior=always_invoice`), and our row moves only after Stripe
  agreed. Without `stripe.manage` it is a 403 naming the reason; a workspace not billed through
  Stripe changes plan exactly as before.
- **Switching the account changes only where the money goes** (product owner, 2026-10-09;
  `stripe_carryover.py`, `tests/test_stripe_carry_over.py`). Stripe cannot move a subscription or a
  card between accounts through its API, so a superadmin's activation (after the plan sync) RECREATES
  every live subscription from the previous account: same plan, first billed where the old paid
  period ends (`billing_cycle_anchor`, `proration_behavior=none`; `trial_end` for a trial), so
  nothing is charged twice. It charges the card when the new account holds one for the customer
  (Stripe's own migration copies customers with their ids and cards, and `customer_id_for` adopts
  them), otherwise it emails an invoice, which asks for money and never takes it: a workspace that
  only paid with a test card is never charged a real one. Then the old subscription is set to end at
  period end with `metadata[nexus_carried_to]`, read with the old key from the stored credentials
  or the environment. Rules that hold it together:
  - **No old key, no move.** A new subscription beside an old one nobody can stop bills twice, so
    it fails with `NO_OLD_KEY` and says what to do. Keep the old credential stored (activating a new
    one deactivates it, it does not delete it).
  - **If the old account refuses to end it, the new one is cancelled at once**: never two renewing.
  - **A half-finished move is finished, not repeated**: the old subscription names the new one.
  - **One the old account already ends is left**: the customer chose to leave.
  - **No credits are granted** (the plan does not change), and the webhook keeps our period start
    for the carried first period: the period roll keys its invoice on that start's month, and
    Stripe's start (the switch) would shift it. Events about a retired id
    (`previous_psp_subscriptions`) are ignored, or the old one's end would cancel the new one.
  - **Anything acting on a subscription moves it first**: plan change carries a missed one before
    repricing, cancel ends one still in an old account there, the portal opens on the new account's
    customer. `POST /admin/billing/subscriptions/stripe-carry-over` ("Move subscriptions here" on
    the live Payments row) is the retry. `in_previous_account` is the 409 left for when Stripe still
    answers 404.

**What an account switch needs is in the panel** (asked 2026-10-09, `tests/test_stripe_switch_setup.py`).
Payments tab: the secret and publishable keys, then **Create webhook in Stripe**
(`POST /admin/payment-credentials/{id}/webhook-endpoint`, `stripe.manage`), which uses that
credential's own key, so a new account is set up completely before it goes live. It creates the
endpoint at `{app_base_url}/api/billing/webhooks/stripe` pinned to `StripePaymentProvider.API_VERSION`
with exactly `HANDLED_EVENTS`, tags it `metadata[nexus]=WEBHOOK_TAG`, removes only our own earlier
endpoints at that URL (others are reported, not touched), and seals the signing secret onto the
credential; the secret is never returned. Configuration, Billing: **App public address**
(`app_base_url`, validated by `_validate_public_base_url`: an https origin with no path, private
hosts refused outside local; read by checkout, the portal, the console's Checkout links and
password-reset emails) and **Stripe Tax at Checkout** (`stripe_automatic_tax`, which makes
Stripe refuse every Checkout unless Stripe Tax is set up in that account). The portal configuration
is found or created per account by itself. The restricted key needs Subscriptions: Write and Payment
Methods: Read, and Webhook Endpoints: Write only for the button;
`docs/billing/stripe-connection-guide.txt` lists every scope and the switch procedure.

**Checkout says why Stripe cannot take payment, before a customer finds out**
(`nexus/billing/checkout_readiness.py`, 2026-10-12, `tests/test_checkout_readiness.py`). Reported:
"Continue to checkout" opened Stripe's page and it read "There are no valid payment methods
available for this session". Our session names no `payment_method_types`, so Stripe picks from the
ACCOUNT's own payment method settings when the page renders, and creating the session succeeds
whatever they say: nothing on our side could see it.
- Blocking: charges not enabled (an account Stripe has not finished activating, with the count of
  requirements due), the `card_payments` capability not active, cards switched off in the default
  payment method configuration. Checkout then answers 409 "Nothing was charged" instead of opening
  a dead page, and logs the cause at WARNING.
- Warning only: an **Indian** account selling in anything but INR (Stripe lets a buyer in India pay
  an Indian business only in INR, so exactly that message is what an Indian buyer sees; buyers
  elsewhere can pay), and a restricted key that cannot read Payment Method Configurations.
- An Indian account's Checkout collects the billing address (`account_country`), which Stripe
  requires for India's export payments.
- **The check fails open**: if it raises, checkout goes ahead as before. Cached 10 minutes per key.
  Platform health's `payments` row reads it (ERROR on blocking, DEGRADED on warnings) for the
  currencies on sale, and the Payments tab's **Check checkout** asks it about one credential's own
  account (`POST /admin/payment-credentials/{id}/checkout-check`, `pricing.write`) before it goes live.
- **With a test key, an account Stripe has not activated is a warning, not a refusal** (2026-10-13):
  test mode takes payments before activation. Every Checkout opened logs the account's country,
  currency, card capability and charges flag, and each warning, because when Stripe's page refuses,
  the app log otherwise says only "200".

**Which payment method configuration Checkout uses is chosen per Stripe credential** (2026-10-13,
migration `0070`, `PUT /admin/payment-credentials/{id}/payment-method-configuration`, `pricing.write`,
`tests/test_payment_method_configuration.py`). Asked after staging's Checkout resolved ZERO payment
methods (read from Stripe's own session data: a US account, USD, `payment_method_types: []` under
the Default Config) and the operator made a new configuration that the app had no way to use. Empty
(every row before) is the account's Default Config, as before. On the credential, not a setting,
because a `pmc_` id belongs to one Stripe account. Read back from Stripe with that credential's own
key before storing: another account's id (404), an inactive configuration, or one with cards off
or unavailable is refused with the reason. The active credential's choice rides on
`StripePaymentProvider.payment_method_configuration` into every Checkout, and the readiness check
reads that configuration rather than the default (404 or inactive is blocking).

**One Stripe subscription per workspace, and only one that has started** (2026-10-12,
`tests/test_one_stripe_subscription.py`). Found auditing the payment paths: a workspace paying for
Launch pressed "Continue to checkout" on Accelerate, Checkout made a SECOND subscription, our row
moved to it and Stripe billed both; the old one's events still reached the row by
`metadata.tenant_id`, flipping the plan back, and its `deleted` cancelled a paying workspace.
- **Checkout refuses a workspace already paying through Stripe** (409, "would charge it twice").
  A plan change for it happens on its existing subscription.
- **Only the newest Checkout page can be paid.** The session opened is stored on the subscription
  row (`meta.open_checkout`); opening another expires it first (`expire_checkout_session`), under a
  row lock so two requests cannot both open one. If the customer had already paid on it, 409 "just
  completed". A failure to expire is logged and does not stop the sale.
- **Return addresses stay on this app** (`_own_url`): a path is taken relative to `app_base_url`,
  a full address must be on that origin, backslashes and control characters are refused; 422
  otherwise. With no `app_base_url` (local) values pass as before.
- **The webhook moves a row only for a subscription that has STARTED**: Checkout `paid` or
  `no_payment_required`, `checkout.session.async_payment_succeeded`, or Stripe status `active` or
  `trialing`. A bank debit completes Checkout `unpaid` and settles days later, and an `incomplete`
  subscription expires on its own; before this both moved the workspace onto the paid plan with its
  credits, and the expiry then cancelled the free plan it had. Now they only note
  `meta.pending_psp_subscription`. A plan Stripe reports while the followed subscription is not
  running (`past_due`, `unpaid`) is taken, its credits wait for `invoice.paid`.
- **A started subscription replacing a live one retires the old one**: its id goes into
  `previous_psp_subscriptions` (so `_is_retired` ignores its later events), ERROR log, the
  `subscription_replaced` webhook counter, and the `billing_end_replaced_subscription` job ends it
  at Stripe now. The job ends it only while the replacement reads `active`/`trialing` at Stripe,
  so a stale event can never end the subscription a customer pays on, and a refusal raises into
  retries and dead letters. Refunding any overlap is an operator's call. An event older
  (`created`) than the last one applied cannot move the row to another subscription, and a carried
  subscription (`nexus_carried_from`) is the account switch's to end, not this job's.

## Usage invoices are real invoices (`nexus/billing/collection.py`)

Subscriptions ran through Stripe end to end and got hosted invoices, PDFs and line items.
Usage and overage were rated here and collected with a bare `charge`, so the charges customers most
want explained were the ones with **no document behind them**.

`collect_invoice` now calls `provider.create_invoice` with the lines we already rated.
**Our rating stays the source of truth** — the provider prices nothing; letting it compute the total
would put the arithmetic somewhere `reconcile.py` cannot check.

Two things about the Stripe call that are the opposite of what they look like:

- Create the invoice with `pending_invoice_items_behavior=exclude` **first**, then attach items to
  that invoice id. Adding items first sweeps in anything else pending on that customer — including
  items a subscription put there — and bills it on our usage invoice.
- A finalize that succeeds followed by a payment that fails **still returns the invoice**. It exists
  and is payable from its hosted page; reporting that as a failed creation would hide a real invoice
  and invite a second one for the same period.

**A paying workspace switches plan on the subscription it has** (2026-10-12,
`POST /billing/switch-plan`, `tests/test_switch_plan.py`). What Checkout's refusal points to. The
price list shows "Switch to X" when `UsageOut.switch_in_place`, and a dialog states the terms before
any money moves:
- **The new plan starts today and is charged in full now** (`billing_cycle_anchor=now`,
  `proration_behavior=none`); the rest of the current period is not refunded and the credits held
  stay. Chosen so every credit granted is matched by a payment: with proration and credits up
  front, upgrading on the last day of a period bought the whole allowance for a few cents.
- **Applied only if paid** (`payment_behavior=pending_if_incomplete`). A refused card is a 402
  "Nothing changed", and its pending update is dropped by voiding the invoice; a bank asking to
  confirm returns `confirm_payment` with Stripe's invoice page, and the plan moves when the
  webhook says it applied.
- **Credits once, keyed `plan_switch:<invoice>`.** Our row moves only after Stripe agreed, under a
  row lock that the webhook also takes (`session.refresh(sub, with_for_update=True)`), so its
  `customer.subscription.updated` sees the plan already moved and grants nothing more.
- Only an `active`, card-billed subscription that is not set to end, to a paid plan on sale in the
  same currency; each refusal says what to do instead (past due: update the card; Free: cancel in
  the portal). A `sub_` id with no Stripe credential in force is refused. Stripe idempotency key
  `switch:<sub>:<plan>:<period start>`, so a double click charges once.

**A Stripe renewal is credited when Stripe is paid for it, once** (2026-10-12,
`webhooks._grant_renewal_credits`, `tests/test_stripe_renewal_credits.py`). `roll_period` granted a
period's credits at OUR boundary, before Stripe had tried the card, so an unpaid renewal had
already been spent; and when Stripe's `customer.subscription.updated` moved the row's period first
(it fires at the same moment) the roll never ran and the renewal was not credited at all. Now
`invoice.paid` with `billing_reason: subscription_cycle`, for the subscription the row follows,
grants the plan's credits keyed `stripe_renewal:<invoice>`, and the roll grants nothing when
`psp_subscription_id` starts `sub_`. The first invoice (`subscription_create`) is the purchase,
already credited by Checkout or the subscription event. Workspaces billed here roll as before.

**A webhook reads the same in any Stripe API version** (2026-10-21, `webhooks._in_pinned_shape`,
`tests/test_stripe_event_versions.py`). Stripe renders a webhook in its ENDPOINT's version: Create
webhook in Stripe pins `API_VERSION` (2024-06-20), an endpoint added in Stripe's Dashboard takes the
account's default. Since 2025-03-31 that moves a subscription's period onto its items and an invoice's
`subscription` under `parent.subscription_details`; measured by paying real test-mode Checkouts on
2026-06-24.dahlia, an annual purchase kept Free's one-month period, and a renewal would have been
charged and never credited. The handlers read the pinned shape; missing fields are filled from the
new places, never overwritten. Outbound calls send `Stripe-Version` and are unaffected.

**Refunds, chargebacks and fraud warnings reach the workspace** (2026-10-12,
`nexus/billing/payment_risk.py`, `tests/test_disputes_and_refunds.py`). The webhook looked the
event's OWN id up as a PaymentIntent, but a refund's object is a charge and a dispute's a dispute,
so none had ever matched an invoice. Now the payment is found by the PaymentIntent or invoice the
object names (a Stripe invoice's local copy records its PaymentIntent on `psp_reference` for this),
else by asking Stripe about the charge (`get_charge`) and matching its customer.
- **Refund: recorded on the invoice, nothing else.** Refunding is an operator's decision in Stripe,
  and so is what the customer keeps; a goodwill refund that took credits back would undo itself.
- **Dispute opened, or `radar.early_fraud_warning.created`: the credits that payment bought are
  held**, capped at the unspent balance, as a negative `adjustment` with capability
  `billing.dispute_hold` (so the credit report names it), keyed `payment_hold:<charge>` so a fraud
  warning then a dispute on one payment hold once. Stripe debits a disputed amount at once; the
  money has left. The plan's included credits are the amount (the invoice's plan, else the
  workspace's). ERROR log, `dispute_opened` / `fraud_warning` counters, and `open_disputes` /
  `fraud_warnings` on the subscription row.
- **Dispute won: the hold is released** (`payment_release:<charge>`). Lost: it stays and the row
  lists it in `lost_disputes`.
- **Nothing cancels a subscription or refunds money automatically.** A card flagged by mistake
  would lose its customer to a decision nobody could take back; both stay operator actions.

**An invoice Stripe raised is Stripe's to collect, never also ours** (2026-10-12,
`dunning.is_providers_invoice`, `tests/test_stripe_invoices_are_stripes_to_collect.py`). The local
copy `_record_provider_invoice` keeps of a subscription invoice (origin `provider`, period key
`stripe:<id>`) entered OUR dunning on its second `invoice.payment_failed`, which stamps
`payment_failed_at`; dunning then raised a new Stripe invoice for the same money while Stripe's own
retries went on collecting the first, so a card that recovered paid twice. Dunning skips such an
invoice and `collect_invoice` refuses it.

`hosted_invoice_url` / `invoice_pdf_url` land in `invoice.meta` and are surfaced on the customer's
own `/billing/invoices`. Empty for the noop provider, and the UI hides the link rather than offering
a URL that goes nowhere — a plausible-looking link that 404s is worse than a visibly absent one.

## The customer directory (`/admin/billing/customers`)

"Which workspace is this person in?" had **no surface at all**: the Subscriptions tab knew the plan,
`/billing/usage` is tenant-scoped and answers only for the caller, and credits were visible nowhere
outside a dialog.

- Search matches workspace name, slug, **or the email of any member**. Credits belong to a
  **workspace, not a person** — the ledger, quotas and the metering engine are all tenant-scoped —
  so an email resolves through membership and the row reports **which address matched**, letting an
  operator confirm they found the right human rather than a workspace containing a similar address.
- `/customers/{tenant_id}/usage` gives per-capability consumption for **any** workspace. Only what
  was actually used: the whole catalog at zero would bury the handful that matter under sixty rows
  of nothing.
- Both run on `get_platform_sessionmaker()`. The documented trap, and the third time it has bitten
  here: a cross-tenant aggregate under the RLS-bound app role returns **zero rows, not an error**, so
  the directory would have shown every customer at 0 requests and 0 credits — indistinguishable from
  a platform nobody uses. Pinned by a test that writes rows for a tenant the caller is not.

**Subscription CRUD is now complete.** `cancel_subscription` had existed since M6 with **no
endpoint**, so the lifecycle step support performs most often was the one they could not, and the
workaround — moving the customer to `free` — leaves the subscription `active` on a $0 plan, which
reads as a live customer in revenue and in every count that filters on status.

`PATCH .../subscription` edits terms (status, trial end, period end, seats, `cancel_at_period_end`).
**`plan_id` is deliberately absent from that schema**: a plan change runs proration, and a PATCH that
repriced a customer because a form posted every field it had loaded is the accident the separation
prevents. `status` is validated against `SUBSCRIPTION_STATUSES` — a value outside that vocabulary is
not a stricter setting, it is a subscription neither rating nor entitlements can reason about.
Cancel defaults to **at period end**, because the customer paid through it.

## The email the platform sends itself (`nexus/integrations/platform_mail.py`)

Reported from staging, 2026-10-13: "signup OTPs are not being received". Signup said "check your
email" and no code came. What was there: the account mailbox (signup codes, password resets, MFA
sign-in codes) came only from `NEXUS_SYSTEM_SMTP_*`, copied into three senders; its server could only
be one of three presets (`gmail`, `outlook`, `office365`), so any other server resolved to an empty
host and `send_email` answered "smtp not configured" without connecting; a Gmail app password pasted
as Google shows it (four groups with spaces) did not sign in; and `start_registration` ignored the
answer, so whatever went wrong the visitor was told a code was sent and the only trace was a
WARNING in the log. `tests/test_signup_email.py`.

- **One mailbox per purpose, read at send time**: `account` (signup, reset, MFA codes) and `alerts`
  (notifications, its own server: decided with the product owner, so alert volume cannot get the
  address that sends password resets throttled). Invoices are Stripe's to email.
- **Editable in Superadmin on the screens that already existed** (the product owner asked for an
  enhancement, not a new screen): the servers in **Configuration > Platform email** (runtime
  settings, validated before storing: a public host name only outside local, one email address, no
  line break in a From name), each password in **Provider keys** (`system_smtp`, `alert_smtp`,
  sealed, never returned), whose **Test** signs in and sends nothing. All fall back to the
  environment, so a deployment that never opens either screen sends as before.
- **Any SMTP server**: `system_smtp_host` / `system_smtp_port` (empty / 0 = the provider's preset).
  465 is implicit TLS, 587 STARTTLS (`email_sender.resolve_smtp`). A server with no sign-in is a
  relay, as alert email has always allowed.
- **App passwords**: spaces are dropped for Gmail (Google's never contain one); any other server
  keeps the password exactly. A refusal says what to fix (`explain`): Google's 534 5.7.9 / 535 5.7.8
  mean an app password is needed, Microsoft's 5.7.139 means Authenticated SMTP is off for the mailbox.
- **Registration tells the truth**: a code that could not be sent is a 503 "could not send" with
  nothing pending, so the visitor can retry at once; a failed resend is not counted against the five.
- Alert email (`alerts/channels.EmailChannel`) built with no server reads the alerts mailbox at
  send time, so a change in Superadmin applies without a restart.

## MFA (`nexus/auth/`) — M13

Opt-in per user; **login is unchanged for anyone who has not confirmed a factor**. That is the
compatibility line — an unconfirmed enrolment must never gate login, or a half-finished setup
locks someone out of their own account.

- Two methods, one primitive: TOTP is RFC 6238 on a 30s step; the "email" method is the same
  code generator on a 300s step, so a mailed code lives 5–10 minutes (step ± 1 drift) and
  inherits the TOTP **replay guard** rather than needing its own in-flight row and expiry logic.
- `last_used_counter` refuses a counter value that has already been accepted, even inside its
  valid window — without it a captured code is reusable for up to 90 seconds.
- TOTP seeds are Fernet-sealed at rest (`nexus/core/crypto.py`, key from `mfa_secret_enc_key`
  else derived from `secret_key`); recovery codes are stored as one-way hashes, single-use.
  Neither is readable back from the database.
- When MFA is active, login returns a **short-TTL single-purpose challenge token** that
  authorizes nothing except `/auth/mfa/verify`. A challenge accepted as a bearer token anywhere
  else would make the second factor decorative; there is a test asserting it is rejected.
- `user_mfa` / `mfa_recovery_codes` deliberately have **no `tenant_id`**: MFA is read on the
  login path *before* any tenant is known, so RLS enrolment would return zero rows and lock
  every user out. A user can also belong to several workspaces.
- Account recovery: `DELETE /admin/users/{email}/mfa`, platform-admin only, audited with
  before/after. Deletes rather than deactivates — a stale sealed secret is a liability with no use.
- `auth_rate_limit_enabled` now defaults **True**. Tests that legitimately fire many rapid auth
  calls opt out via the `no_auth_rate_limit` fixture; the default is not weakened for them.

## Job durability (`nexus/workers/`)

A handler exception used to be caught by `dispatch`, logged, and dropped. Periodic sweeps
survived that (re-enqueued every tick, idempotent); one-shot jobs — `process_account`, campaign
sends, orchestration runs — were silently lost.

- `Job` carries `attempts`/`max_attempts` **in the envelope**, so retry state survives the worker
  that created it. `from_json` defaults both, so a job serialized by the previous release still
  deserializes during a rolling deploy.
- `dispatch()` returns the handler's dict untouched on success and adds `JOB_FAILED_KEY` when the
  handler *raised* — deliberately not the plain `error` key, which handlers legitimately return
  as a normal terminal outcome (`account_not_found`). Test with `is_job_failure()`.
- `workers/durability.py` retries with jittered exponential backoff, then parks the job in
  `dead_letter_jobs` with its payload intact. Even a failed dead-letter *write* logs the whole
  payload at ERROR. Shutdown flushes in-flight backoffs back onto the queue.
- Triage/replay: `GET|POST /admin/jobs/dead-letters...`, gated on `jobs.manage`, every replay
  audited. Counters in `workers/metrics.py` (enqueued / succeeded / retried / dead-lettered) —
  a plain dict, mirrored since M15 to a Prometheus counter carrying the **job name**. The dict
  stays unlabelled on purpose: keyed by job name it is a memory leak waiting for a name derived
  from user input.
- `NEXUS_JOB_RETRY_ENABLED=false` disables the retry only; the job is still dead-lettered, so the
  kill switch degrades to "fail fast, keep the evidence" — never back to losing work.
- **Handlers stay ignorant of all of it.** No `handle_*` signature and no `HANDLERS` entry changes.
- **At most one copy of each heartbeat driver, waiting or running** (decided 2026-10-14 and
  2026-10-15, `scheduler._OnePending`). The heartbeat queued every driver on every tick whatever was
  already waiting, so a worker that fell behind gained about 22 duplicates a minute and then ran them
  back to back. A driver claims `nexus:driver:<name>` (`TaskQueue.claim`: Valkey/Redis `SET NX EX`,
  failing open) before it is queued, and the worker frees it when the job FINISHES
  (`job_finished`). Freeing it on pickup still let a slow driver overlap the next tick's copy. The
  15-minute TTL frees a driver whose worker died, or one that runs longer than that.

## Observability (`nexus/core/metrics.py`, `nexus/workers/state_metrics.py`) — M15

`/metrics` is **on by default**. It was opt-in because an unpinned build once pulled a FastAPI the
instrumentator could not introspect and every endpoint 500ed; what prevents a repeat is the
`try/except` around `_instrument()` plus the pins in `pyproject.toml`, not the default. A
deployment blind to queue lag, 402 rates and dunning depth is not operable.

- **Counters in the API, gauges in the worker.** The app runs uvicorn with 2 workers, so it needs
  `PROMETHEUS_MULTIPROC_DIR` (set in compose, emptied by the entrypoint — stale per-process files
  are never reclaimed) for counters to aggregate. In that mode `prometheus_client` reads only the
  mmap files: **gauges need a declared mode and custom collectors are not read at all**. So state
  lives in the worker, which is single-process and exports on `:9100`.
- **State is refreshed on a loop, not collected at scrape time.** A collector that queries Postgres
  per scrape lets anyone who reaches `/metrics` drive four aggregate queries at will.
- A failed state query **leaves the previous gauge value** rather than writing 0 — a zero reads as
  "the problem went away", which is the one thing it must not say when the truth is "we could not
  look". An unmeasurable queue leaves the gauge **absent** for the same reason.
- `nexus_billing_decisions_total{capability,outcome,reason}` is the point of the milestone:
  `would_block` is what shadow mode computes on every call and used to discard, so "what happens if
  we flip enforcement on?" was previously answerable only by flipping it. `outcome` and `reason`
  are separate labels — a shadow-mode throttle and an enforced 429 share a reason and have opposite
  outcomes.
- A rejected webhook writes **no row** (the dedupe table only records events that verified), so
  `nexus_webhook_events_total{outcome="bad_signature"}` is the only trace a stale signing secret
  leaves.
- Alert rules live in `deploy/monitoring/alerts.yml`; the stack runs as an **overlay**
  (`docker compose -f docker-compose.prod.yml -f docker-compose.monitoring.yml up -d`). Before M15
  those configs existed but nothing ran them, and the app job pointed at a service name that does
  not exist — every rule evaluated against no data, which reads as "all clear".

## Load testing (`quality/load/`, `deploy/loadtest/`, `scripts/load/`)

k6 is the release gate (`smoke`/`load`/`soak`); Gatling's TypeScript SDK explores (`stress`/`spike`/
`breakpoint`/`capacity`). Both execute ONE catalogue, `quality/load/scenarios.yaml`, through
`generated/catalogue.json` — never edit journeys in tool code. `python scripts/load/catalogue.py check`
fails on drift. Run everything via `scripts/load/run.py`: it enforces hard caps, the IST run window,
pre-flight, guards and test-data cleanup. Staging only, never production. Details:
`docs/quality/load.md`; connection budget: `docs/quality/load-capacity.md`.

- Default journeys are GET-only: no paid providers. Paid paths need `--budget <journey>=<calls>`.
- Login is rate-limited per client IP (10/min): personas log in once and share tokens.
- B1ms has **35 user connections** (50 − 15 reserved), not ~50. `NEXUS_DB_POOL_SIZE` is shared with
  the worker via `common_env`, so shrinking it also cuts worker concurrency.

## Signal sources (`nexus/ingestion/`) — M16

**`signal_sources` defaults to `web,rss`, not `demo`.** It was `demo`, so an out-of-the-box
deployment scored, alerted and ran plays on **fabricated** events. `DemoSignalSource` is now a test
double in the mould of `network/connectors/fixture.py`, with two guards: `demo_signals_active` is
hard-false in staging/prod, and `Settings._reject_synthetic_signals_in_production` **refuses to
start** if `NEXUS_SIGNAL_SOURCES` names `demo` there. The second exists because the first is
silent, and an operator who asked for demo signals and got none cannot tell that from a broken
pipeline. `tests/conftest.py` injects the double explicitly, so the offline suite is unaffected.

**Every source run is recorded** in `signal_source_runs` (migration `0030`, tenant-scoped so
`apply_rls.py` enrols it) — source, outcome, items found vs. new, duration, error, and the rendered
queries as provenance. Written **whether or not anything was found**: previously a source broken for
a week and a quiet market produced identical evidence, and the first sign of trouble was a rep
asking why an obviously-funded account showed no round. `empty` is deliberately not `ok` — a source
that runs cleanly and finds nothing every time is broken, and merging the two hides that.

A source may declare `timeout_s` to override the shared 8s budget, which assumes one request. A
source killed mid-run reports nothing, which is indistinguishable from an account with no signals.

Three live sources run **together**, not as alternatives; all three route dedupe through
`event_dedupe_key`, so one funding round found by two of them is one signal.

**`_classify_news` reads negation and speculation.** It used to substring-match, so "Acme raised no
new funding" scored `funding` 0.85 — the strongest class, which creates an Inbox task and can
trigger a play, and the rep only found out by opening the article. Two rules make the guard work:

- Cues are checked **within the clause containing the needle**, not the whole headline. "raises $40M
  Series B **with no** participation from existing investors" is a real round whose "no" belongs to
  a different clause.
- Needles are matched against the **original string**, with clauses used only to scope the check.
  Splitting the text first tore `"partners with"` in half — a needle that contains a connective —
  so partnerships silently stopped being detected.

Keep `_NEGATION_CUES` short and high-precision. Every entry can suppress a true positive, which is
why "expected", "seeking" and "aims" are deliberately absent: missing a real round costs as much as
inventing one. Note that several inflected forms ("raising", "acquiring") are not needles at all, so
some negated phrasings pass by luck — `tests/test_signal_classifier.py` pins them, so adding a
needle later cannot quietly reopen the hole.

- `WebNewsSource` — one broad OR-query per account. Catches events indexed under no recognisable
  phrase.
- `DorkedSearchSource` + `dorks.py` — one high-precision query per signal *kind*, preferring recent
  results. On by default; `NEXUS_SIGNAL_SOURCES=...,no_dorks` removes it, and
  `NEXUS_SIGNAL_DORK_MAX_QUERIES` is the cost dial (each dork is one billed search call).
- `RssSignalSource` — the company's own feed. Opt-in via `...,rss`.
- `AtsSignalSource` + `ats.py` — the account's own job board (M17). **Keyless**: Greenhouse, Lever
  and Ashby all serve public JSON. On by default; `no_ats` opts out.
- `PublicApiSignalSource` + `public_apis.py` — SEC EDGAR, GitHub, Hacker News (M18–M19). Keyless;
  each sub-source degrades independently. `no_public_apis` opts out.
- `WebsiteWatchSignalSource` + `webwatch.py` — watch the account's own pricing/security/careers/
  about pages and report changes (M20). Opt-in via `...,website`: it is the heaviest source
  (up to four fetches per account) and needs a stored baseline (`page_snapshots`, migration `0031`).

**Attribution is the recurring bug in this subsystem, and it has bitten five times.** Every one of
these sources is a **global namespace searched by name**, and a name search returns whoever matches,
not whoever you meant. Measured:

| Source | What it returned | Guard now required |
|---|---|---|
| Greenhouse guess | `example.com` → a board owned by *Democorp*, 21 roles | owner name must match |
| EDGAR full-text | "Stripe" → a Form D by *DCP STRIPE XXII* | exact CIK, not full-text |
| EDGAR full-text | "Vanta" → an 8-K from **2006** | recency floor |
| Hacker News | "Vanta" → *Vanta.js*, a 3D-graphics library, 377 pts | story URL must be the account's domain |
| LinkedIn dork | `linkedin.com/company/vantaesports` | `require_url` path constraint |

Before adding a source here, answer: **what proves this result is about this account?** If the
answer is "the name matched", it is not a source yet. EDGAR is the sharpest illustration — moving
from full-text search to exact CIK resolution means private companies now return `unsupported` and
no signals at all, which is a real coverage loss and the correct trade.

**Website change monitoring lives or dies on normalisation.** Hashing raw HTML is unstable across
two fetches *seconds* apart — measured on linear.app/pricing and ramp.com/security — because pages
carry build ids, nonces and cache-busting asset URLs. A naive watcher reports "pricing changed" on
every run and trains people to ignore it. `webwatch.normalise` drops script/style/svg **bodies**
before stripping tags (order matters), then lowercases, strips volatile fragments, and collapses
whitespace. A first sighting is a **baseline, not an event**.

Rules for editing `dorks.py`:

- **Operators are limited to the intersection every engine supports** (`site:`, `-site:`,
  `intitle:`, `inurl:`, quotes, `OR`). No Google-only `after:`/`tbs=`: the default provider is
  DuckDuckGo HTML, and a dork that degrades to keyword soup on three of four providers is worse
  than a plainer one that works on all of them.
- `site:` groups must be **OR-ed inside parentheses**. Repeated bare `site:` terms read as an
  impossible AND and return nothing, forever, silently.
- Recency is `inurl:{year}` plus the year as a token — news CMSs put the date in the path. Real
  date filtering needs `SearchProvider.search_recent`, which only Exa implements; the base
  **delegates to `search`** rather than returning `[]`, or the library would be useless keyless.
- `trust_kind` only where the **URL pattern** settles the event class (ATS boards, a company's own
  engineering blog). Not for trade press — TechCrunch carries funding, acquisitions and launches
  from the same paths, so trusting it stamps one kind on all three. That bug shipped and was caught
  by `test_a_dork_does_not_inflate_a_weaker_event`.
- Open-web dorks keep three post-filters, each added after a live false positive:
  - **The name must be in the TITLE**, not merely somewhere on the page. Matching title+snippet let
    an industry round-up through — *"Cybersecurity Startup Investors Pulled Back In Q3"* scored
    funding 0.90 for Vanta because the article mentions them in passing.
  - **The event is classified from the title alone.** A headline states what happened; a body
    mentions everything the company has ever done. Classifying on both scored a product page,
    *"Vanta Delivers: Vanta control framework"*, as funding 0.85 because its text recalled an
    earlier round. The snippet still feeds `require_any` — a relevance floor and an event
    determination are different questions.
  - **A `require_any` vocabulary floor**: a company's own About page ranks perfectly for its name
    and reports no event.
  `self_evident` skips these only where the hit is about the company by construction (its own ATS
  board, its own domain).
- **Every dork needs both forms**, because `SearchProvider.query_dialect` has three values and all
  three behaviours were measured against the live services:
  - `operator` (Serper, Brave, Firecrawl — real Google-backed SERPs) gets `template`.
  - `plain` (DuckDuckGo HTML, and the **default** for anything undeclared) gets `phrase`. Measured:
    DDG returns **zero results for any query containing `site:` or `-site:`** while the same query
    without them returns the right pages. It does not error, so an operator dork there is a source
    that silently finds nothing forever. It also 403s after ~10 rapid queries.
  - `semantic` (Exa) gets `phrase` plus the structured `sites`/`exclude` tuples. Operators there
    return the *wrong* thing: the operator ATS dork returned other companies' postings that mention
    the account name, the phrase form returned its own careers page.
  Default to `plain` when unsure — over-claiming costs every result, under-claiming costs only some
  precision.

**ATS board discovery (M17): read the token, never guess it.** The board token is not the domain
root — Vanta and Ramp 404 on Greenhouse because both are on Ashby, and Linear's Ashby token is
capitalised `Linear`. A wrong guess returns 404, which is indistinguishable from "not hiring", so a
guess-only strategy fails silently forever. The company's **own careers page** embeds the token
(measured: vanta→`ashby:vanta`, ramp→`ashby:ramp`, linear→`ashby:Linear`, figma→`greenhouse:figma`);
Stripe embeds none and is recovered by a domain-root guess. Cached in `Account.custom_fields`.

Three rules that were each found by running it, not by reasoning:

- **A guessed token must prove ownership.** `example.com` guesses the Greenhouse token `example`,
  which is a real board with 21 open roles owned by "Democorp". So guessing is restricted to
  providers that publish an owner name (`/v1/boards/{token}` → `name`), and the name must match the
  account. Ashby and Lever publish none, so they are discovered or not used.
- **200-with-empty is not 404.** "Board exists, nothing open" is real information about a company
  that stopped hiring; "not on this ATS" is a blind spot. Never merge them.
- **Do not parse the careers page for the jobs.** Ramp's is 3.7 MB of JavaScript whose listings come
  from Ashby anyway.

The signal is **aggregated** — one `hiring` signal per account per month carrying the count and the
departments hiring. Vanta has 100 open roles and Stripe 542; one signal per requisition would bury
every other signal in the inbox.

**LinkedIn jobs are reached through the search index, never by scraping.** `site:linkedin.com/jobs`
against a search provider returns publicly indexed posting pages (titles read "Vanta hiring Senior
Copywriter"), which is a different act from fetching linkedin.com — that has no compliant API and
bans scrapers. The dork carries `require_url="linkedin.com/jobs/"` because the same search also
surfaces company pages: `linkedin.com/company/vantaesports` passed the name gate for an unrelated
esports org.

**No single vendor is load-bearing.** Measured limits: keyless DuckDuckGo's HTML endpoint returns
403 after roughly ten rapid queries — inside a single account refresh — so the dork source paces
itself on that backend and **stops the batch** on the first provider failure rather than deepening
the block. `firecrawl` was added as an operator-native alternative whose `tbs` parameter gives real recency
without an Exa key; it carries a key-rotation pool (`NEXUS_FIRECRAWL_API_KEYS`) with the same
semantics as Exa's, because a crawl issues several queries per account and one free-tier key is
exhausted quickly.

`NEXUS_SIGNAL_SEARCH_PROVIDER` selects a backend for **signals alone**; empty means "use
`search_provider`". Keep them separate: `search_provider` is global, and lookalikes
(`find_similar`) plus company/ICP discovery (`search_companies`) are **Exa-only capabilities**.
Both now ask LinkedIn first and Exa only when LinkedIn gives nothing (see *LinkedIn prospecting*
below), so Exa is the fallback there rather than the only source; the warning still holds for it.
Repointing the global setting to diversify signal collection takes those down silently — the base
`find_similar` returns `[]`, so lookalikes report "no results" with nothing in the logs.

## Self-hosted fetching (`nexus/fetching/`, `services/fetch/`)

Signal collection re-asked the same four dork queries about the same company on every six-hour HOT
refresh, per tenant, at $0.0064 each through Firecrawl: ~$3/month per hot account, bought again for
every workspace tracking it. `DataSourceRegistry`'s cache is in-process and `DorkedSearchSource`
bypassed it entirely. Scrapling was already a declared extra with a `ScraplingBrowser` adapter, and
inert: the production image never installed it. Spec and plan:
`docs/superpowers/{specs,plans}/2026-09-18-self-hosted-signal-fetching*`.

- **`web_cache` is platform-global (migration `0062`, no `tenant_id`)**, like `companies` and
  `people`. Enrolling it in RLS would return zero rows to the shared reader — silently.
- **The TTL IS the cadence.** Funding and news 6h, everything else searched 24h, pages 24h (all
  runtime settings). No scheduler change: a fresh cached answer short-circuits in
  `DorkedSearchSource._run`, so `next_refresh_at` and `tiering.classify` are untouched.
- **A failed search is never cached.** A condemned key pool returns `[]` with `last_failure` set,
  not an exception; caching that freezes an outage for the whole TTL. And a CACHED answer ignores a
  stale `last_failure` from an earlier call, which would otherwise abort a crawl that bought nothing.
- **The cache store is total** (`nexus/fetching/cache.py`): every entry point, including argument
  normalisation, is inside its try. It opens its own platform session, so callers finish cache work
  before opening a tenant write transaction — the ordering `nexus/people/enrich.py` documents.
- **The chain declares `plain`, the dialect of its weakest member** (`search/fallback.py`). The dork
  renders before it knows who will answer; an `operator` query returns zero results on DuckDuckGo
  and does not error. So with the fetcher on, a Firecrawl fallback gets the plain phrasing — a
  deliberate precision cost on what should be the rare path. An empty answer with no failure is an
  answer, never retried down the chain.
- **`nexus-fetch` is stateless and never publicly reachable** (`nexus/fetching/service.py`, deployed
  from `services/fetch/`). Shared secret compared in constant time, and an EMPTY secret refuses
  everything; firewall to the app's egress; `nexus/fetching/guard.py` reuses
  `sources/safety._is_blocked_host` rather than keeping a second copy of the SSRF rules; the HTTP
  tier follows redirects with `follow_redirects="safe"` because the guard only sees the first URL,
  and the FINAL url is re-checked before content is returned, because a browser follows redirects
  the guard never saw. Every endpoint, `/health` included, needs the token. Page content is capped
  (`FETCH_MAX_CHARS`, default 2M, `truncated: true`): targets come from tenant-controlled domains.
  Its own VM, NOT the Reacher box: scraping egress must not share an IP with email verification.
- **`blocked` is not `empty`.** An anti-bot page raises `BlockedByEngine` → 503 → `last_failure`,
  so a crawl records `error` rather than a quiet market. `parse_ddg` pairs titles and snippets by
  destination URL, not position, and drops sponsored results (they still point at duckduckgo.com).
- **Shadow before promotion.** The chain cannot see a free engine that answers WORSE — it never
  falls through. `signal_fetch_shadow` asks both, serves the paid answer, and records per-member hit
  counts in provenance; `scripts/fetch_shadow_report.py` reads `paid_only` asymmetrically, like
  `companies/diff.py`. It runs on the platform sessionmaker (`signal_source_runs` is tenant-scoped).
  Shadow runs have their own cache keys (`fallback-shadow`), or answers cached before it was on
  would be served for a day and nothing compared.
- **The chain holds per-call state on the instance** (`answered_by`, `last_failure`,
  `shadow_report`), like every provider here. Safe while `run_worker` is serial; bounded
  per-account concurrency must give each in-flight account its own provider, or two accounts will
  write each other's provenance.
- **Website watch uses both** (`webwatch._get`): cache, then the fetcher, then plain httpx, so a
  deployment without the VM behaves exactly as before. Pages over 1M characters are used, never
  cached.
- **Signal scans charge no credits** (decided with the product owner 2026-10-15).
  `signal.news_scan` is observe-only in the catalog (`OBSERVE_ONLY` in
  `tests/test_rate_card_conformance.py`) and has no `metered()` call site in `nexus/ingestion/`, so
  this saving is pure COGS. A person scans through Refresh now, charged as
  `workflow.orchestration_run`; `POST /ingest/{account_id}` (no screen) RECORDS one
  `signal.news_scan` after a `preflight`. `POST /relevance/analyze-website` charges
  `ai.icp_from_website`, its own price, once per draft that found something
  (`tests/test_person_started_charges.py`).

## Shared company records (`nexus/companies/`)

One row per real-world company, shared across every tenant, so forty workspaces tracking Stripe
crawl it **once**. Measured motivation: 133 account rows resolved to 115 distinct domains (13.5%
duplication) in a small, mostly-disjoint dataset; the rate rises with tenant count and ICP overlap.
Design and costing: `docs/superpowers/plans/2026-07-31-master-company-data-layer.md`.

**Identity is the normalised domain and nothing else.** Free mail, reserved names, link shorteners
and bare labels resolve to nothing, and an account with no usable domain gets **no** company and
keeps being crawled per-tenant. That is not a gap to close later — name-based resolution across
tenants is how one workspace's data reaches another's, and this subsystem has already shipped six
wrong-attribution bugs by trusting a name match.

- `companies` / `company_signals` carry **no `tenant_id`**, so `apply_rls.py` leaves them alone.
  Enrolling them would make the shared crawler see zero rows — silent under RLS, not an error.
  Everything here runs through `get_platform_sessionmaker()`.
- Ids are `sha1(domain)`. Deterministic, so two workers racing on one company produce the same key:
  one insert wins, the other re-reads, instead of both succeeding and splitting the timeline.
- Firmographics only ever **fill blanks**; `tech_stack` is unioned, never replaced. A tenant's
  correction must not rewrite what every other tenant sees — per-tenant overrides stay on `accounts`.
- `accounts.company_id` is **nullable**, and a null link behaves exactly as before the column
  existed. Pinned by test.

**The rollout is staged on purpose, and the stages are not interchangeable:**

1. Backfill (`backfill.py`) — idempotent, only ever fills a NULL, bounded, has a dry run.
2. Shadow crawl (`crawl.py`) — writes `company_signals`, **read by nobody**. Reuses the existing
   per-tenant sources; a second crawler would drift and the diff would compare two bugs.
   `test_nothing_reads_company_signals_yet` asserts the shadow property *structurally* by scanning
   the tree, so it stays true by test rather than by memory.
3. Diff (`diff.py`) — **reports, never repairs**, like `billing/reconcile.py`. Read it
   asymmetrically: `shared_only` is usually fine (the shared crawl ran more recently);
   **`tenant_only` is the failure** — fan-out would show less than the tenant has today.
4. Fan-out (`fanout.py`) — `NEXUS_SHARED_COMPANY_CRAWL_ENABLED` is now **default on**, but each
   company is gated **individually** by `companies.crawl_verdict` (migration `0038`): `unknown` and
   `disagrees` keep the per-tenant crawl, only `agrees` earns delivery. Turning the global flag on
   therefore changes nothing until a real diff records agreement, per company — the safety that used
   to come from leaving the flag off now comes from evidence instead of abstinence.

   **`fanout_company` and `pipeline._covered_by_shared_crawl` must test the same condition.** Gate
   delivery on the verdict but not the per-tenant skip, and an unproven company gets *neither*
   crawl: signals simply stop, which is indistinguishable from a quiet market. Pinned by
   `test_fanout_and_the_per_tenant_skip_gate_on_the_same_verdict`.

   Fan-out itself is behind that flag, It reuses
   `IngestionService.ingest` rather than writing `signal_events` directly, so per-tenant dedupe,
   `signal.created` and same-transaction alerting all apply unchanged. A second write path would
   drift, and the first thing to drift would be alerts — signals with nobody notified, the exact
   bug that shipped once already.

Do not enable fan-out on assertion. It multiplies any attribution mistake by the number of
subscribing tenants, and four of this subsystem's six attribution bugs were found only by running
against live providers.

## Shared people records (`nexus/people/`) — encrypted, resolve once

The companion to `nexus/companies/`. A `Contact` is per-tenant, so forty workspaces tracking one VP
Engineering means forty rows and **forty paid phone lookups for one phone number**.

- **Identity is explicit: LinkedIn URL, else normalised email. Never a name match.** A contact with
  neither gets no shared person and stays entirely per-tenant. Getting a *person* wrong means a rep
  phones a stranger with someone else's context — a worse failure than the six wrong-*company* bugs
  this codebase has already shipped. LinkedIn beats email because a person keeps their profile
  across jobs; keying on email makes every job change look like a new human, which is exactly the
  event `job_switch` needs to detect.
- **Email and phone are Fernet-sealed; a `sha256` column beside each is what lookups use.** Fernet is
  randomised, so one value seals differently every time and an index over ciphertext matches
  nothing — the hash is what makes the sealed column usable at all. The hash is an *index, not
  anonymisation*: email space is small enough to brute-force. Name, title and company domain are
  deliberately **not** encrypted; they are business-card facts the product must search on.
- `people` / `person_identities` carry **no `tenant_id`** (migration `0037`), like `companies`.
  Everything runs through `get_platform_sessionmaker()`.
- **Erasure deletes identities explicitly**, not via `ondelete="CASCADE"`. Postgres enforces the FK
  cascade and SQLite does not, so relying on it means erasure works in production and silently
  leaves orphaned identity hashes in the suite. Erasure is the one operation where a
  "passes every test, differs in prod" split is unacceptable.
- **A recorded `not_found` is not re-purchased.** A miss is an expensive answer; re-asking every
  crawl is the difference between a bounded monthly bill and an unbounded one.
- **A cache hit is still metered.** The customer received an answer and is charged for the answer;
  what the shared store improves is **COGS**, not price. Billing only on a miss would hand the saving
  to whichever customer happened to ask second and make revenue depend on crawl ordering. Usage rows
  carry `attrs.cached` so the margin is visible in the stream.

**The pool holds what WE found, and Find contacts asks it first** (2026-10-02, `people/pool.py`).
The store held phone lookups and nothing else (empty locally) while workspaces paid LinkedIn for the
same buyers twice. Decided with the product owner, "only what we found":
- every row a LinkedIn employees run PROVED (the account's own page, the company named, an ICP
  title), including ones this workspace did not take: profile, name, title, company domain, source
  `linkedin_employees`. A later read moves a person who changed jobs: that is our newer evidence,
  not a tenant's edit, so fill-blanks-only does not apply to it.
- an address our finder found (not the one the contact arrived with) and a MAILBOX verifier proved,
  on the company's own or mail domain, for a contact with a LinkedIn profile. Only the address and
  the profile are shared: the name and title may be the customer's upload, and a row made this way
  keeps the default source, so Find contacts never offers it.
- customer uploads, CRM imports and typed contacts stay in their workspace.

Writes go through the `pool_found_people` job, never the request: the pool is platform-global and
the request holds a tenant write transaction (the SQLite suite's "database is locked"). Reuse:
`prospecting/contacts.linkedin_contacts` offers pooled people at the domain who pass the same title
gates, seen on LinkedIn within 180 days (delivering one does not refresh them), and buys a run only
for the rest; they are saved as `sourcing:pool`. `SharedPeopleProvider`, free and second in the
waterfall, offers a pooled address (90 days) for the contact's own profile WITHOUT a verdict, so the
final pass verifies it live; `_satisfied` still wants a valid verdict plus a phone, so billing is
exactly what it was.

## LinkedIn URLs: a person's own profile, a company's own page (2026-10-02)

Asked: "ensure incorrect LinkedIn URLs are not mapped to accounts or contacts." Every writer now
goes through `enrichment/linkedin.py`: `person_profile_url` (the WHOLE value is a `/in/` URL;
`canonical_profile_url` merely finds one inside a string, right for search results and wrong for a
field) and `company_page_url` (`/company/`, `/showcase/`, `/school/`).
- **Typed or uploaded**: the contact form and the similar-person add 422 with the shape required
  (validated in the endpoint, not on `ContactIn`, which `ContactOut` inherits: a stored bad value
  would otherwise fail every contact list); a contact CSV cell is left blank and reported, the row
  still imports; an account CSV `linkedin_url` must be a company page.
- **Found by search**: `is_own_profile` requires the name in the result's TITLE lead or the URL
  slug. The snippet no longer counts: a colleague's profile mentions the person and the company,
  and passed both the finder and contact search's proof.
- **An account's page** from the web enrichment is kept only when the search returned it and that
  result states the account's own domain (`account.proven_company_page`): measured,
  `linkedin.com/company/vanta` is VANTA - Chauffeurs. Missing beats wrong for a link a rep trusts.
- `scripts/clean_linkedin_urls.py` (dry run by default) moves bad stored values to
  `custom_fields["rejected_linkedin_url"]` and sets account pages a LinkedIn read proved from the
  shared company. Local dry run: nothing bad stored, 121 account pages to fill.

## Account refresh: tiered, and claimed off a stored due-time

Every account used to be refreshed on the same 6h cycle. Measured, that is what made the pipeline
unaffordable: 500 tenants x 1000 accounts demands **23.15 accounts/sec** against a measured drain of
**0.036/sec** on one serial worker. Most of that spend re-crawled accounts where nothing had
happened in months. Numbers, method and what is still open: `deploy/loadtest/README.md`.

- **`accounts.next_refresh_at` is stored, not derived** (migration `0042`). The old claim
  (`last_refreshed_at IS NULL OR <= cutoff`, `ORDER BY ... ASC NULLS FIRST`) cannot use a btree —
  measured at 500k accounts it seq-scanned and sorted 261k rows through a **26 MB external merge on
  disk** to return 100, every tick. Storing the answer makes it an index scan that stops at the
  limit: **489ms → 5-8ms warm**, O(batch) instead of O(estate). NOT NULL defaulting to now, because
  a nullable column would reintroduce the NULLS FIRST ordering that made the old index useless.
- **The claim stamps a conservative 6h default before the pipeline runs**, and the pipeline
  re-stamps the real tier at the end. So an account whose processing dies part-way comes back on
  the old cycle rather than stalling forever — the tier can only ever push it further out from a
  schedule that already exists.
- **`tiering.classify` is biased toward hot, deliberately.** Hot if the crawl just found something,
  or there is a signal in the last `account_hot_signal_window_days`, or someone there is in a live
  engagement sequence (active, awaiting review or snoozed),
  or it is on a list. Cold is only what is left. Wrongly hot costs one crawl; wrongly cold means a
  rep learns about a funding round three days late, which is the failure the product exists to
  prevent — the same asymmetry as `_NEGATION_CUES` in the signal classifier. A classification
  failure returns **hot**, which is the pre-tiering behaviour.
- Ordered cheapest-first and short-circuits: new signals in hand means **zero queries**.
- `last_refreshed_at` is untouched and still written — `pipeline.process_account` reads it to
  decide whether to seed an account from the shared company crawl.

**Sources run concurrently** (`signal_sources_concurrent`, default on). Per-account crawl
**26.98s → 14.94s (1.81x)**, measured as sum-vs-max over 355 real crawls. **Session-bound sources
are excluded from the gather and that is not optional**: a change detector borrows the caller's
TenantSession via `bind_session`, and SQLAlchemy's AsyncSession is not safe for concurrent use —
two coroutines awaiting on one session interleave on a single connection and raise, or return each
other's rows. They run sequentially, after the network-only ones. `_run_one` is shared by both
paths so a source's recorded outcome, timing and provenance cannot drift with the schedule.

**`run_worker` runs N jobs at once** (`resolve_worker_concurrency`: the DB pool less a reserve,
or `NEXUS_WORKER_MAX_CONCURRENCY`); it was strictly serial when the numbers above were measured. So any
sweep that selects work must CLAIM it before acting (account refresh stamps `next_refresh_at`; the
shared crawl compare-and-sets `last_crawled_at`, 2026-10-15), any per-call state must not live on a
shared source or provider instance (the dork source's cached flag and failure are per call), and a
heartbeat driver stays claimed until it FINISHES (`scheduler.job_finished`), so the next tick
cannot start a second copy beside a slow one.

## External source databases (`nexus/sources/`) — platform-wide, superadmin only

A superadmin registers a **read-only** DSN to somebody else's Postgres; we discover its tables,
map columns onto app fields, and prove the mapping with a dry run before anything consumes it. It
becomes an enrichment provider tried *ahead* of the paid APIs — which is where the cost saving is.
Locked decisions and build order: `docs/superpowers/plans/2026-07-31-master-company-data-layer.md`.

**Per-tenant sources are explicitly ruled out.** Results land in the shared `companies` / `people`
stores, so a mis-mapped source is wrong for **every tenant at once**. Platform data in a tenant
table is duplicated N times; tenant data in the shared store is a cross-tenant leak — and which one
you built is not a config change afterwards.

- **`source_databases` carries no `tenant_id`** (migration `0041`), like `companies` / `people`.
  Everything runs through `get_platform_sessionmaker()`.
- **The DSN is an SSRF primitive** (`safety.py`). A form that accepts a DSN and reports whether it
  connected is a port scanner; pointed at the container network or a metadata endpoint, successful
  introspection is a read oracle. Resolve-then-check, so a public name pointing at loopback is
  caught. `source_db_allow_private` is a **local-dev setting, never a request parameter** — an
  admin must not be able to switch off the guard from the form the guard protects — and
  `Settings._reject_private_source_dsn_in_production` refuses to start with it on in staging/prod.
- **Read-only three times over**: `default_transaction_read_only` at the driver, allowlisted
  statements built in code, and `test_connection` *asserts* read-only rather than assuming it. Any
  one alone is one mistake away from a write into a customer's production database.
- **`require_identifier` runs at query-build time, not just at discovery.** A name returned by
  introspection is attacker-controlled if the attacker owns the source database, and re-reading it
  from our own JSON column does not make it ours. "It was safe when we stored it" is exactly the
  assumption that makes stored-value injection work.
- **The status ladder is the safety story**: `registered → connected → introspected → mapped →
  verified`. Only the service functions advance `status`; a request body never carries one, or an
  admin could set `verified` and skip the dry run. Re-introspecting or re-mapping **clears the
  proof** — a source that stays `verified` after its table was rebuilt is the wrong-attribution bug.
- **A dry run that returns rows but no identities does not verify**, and is *not* `failed`: the
  source is reachable and the query ran, so "failed" would send an operator to check the network
  instead of the columns. `usable_rows`, not `rows`, is the number that matters.
- **Verification and activation are separate.** A passing dry run does not switch the source on;
  `enabled` starts false so an operator gets to read the output first. Disabling is never refused —
  during an incident, "stop reading this" must not be blocked by a state machine.
- **Identity mirrors the shared stores**: a company mapping requires `domain`, a person mapping
  requires `linkedin_url` or `email`. A name is not an identity — that is how this subsystem
  shipped six wrong-attribution bugs.
- Gated on **`sources.manage`**, deliberately not folded into `admins.manage`: registering a data
  source and granting platform power are different acts, and only the `superadmin` preset has it.
  Every mutation is audited; the DSN is Fernet-sealed and is in **no** response model.
- **Step 7 — the enrichment provider — is built** (`nexus/sources/provider.py`), read *ahead of*
  the paid APIs at three seams: the phone lookup (`nexus/people/enrich.py`, the priciest
  capability on the rate card), account firmographics (`nexus/enrichment/account.py`) and the
  contact waterfall (`SourceDatabaseProvider`, deliberately first). Hits land in the shared
  `companies` / `people` stores, so an answer is bought once for every tenant.
  - **Failure posture: fall through to the paid provider, never stop collection.** Every entry
    point is total — unreachable, slow, or stale-mapping all return *no answer*, never an
    exception, and sources are tried independently so one dead warehouse cannot hide the answer
    in the next. It is an optimisation, not a dependency.
  - **`WHERE domain IN (...)` is a candidate filter, not proof.** We do not control a foreign
    database's normalisation, so every returned row is re-checked in Python against the identity
    we asked for, through the same normalisers the shared stores key on. Skipping that check is
    the wrong-attribution bug, written into a store every tenant reads.
  - **A hit is metered identically to the paid lookup it replaced**, carrying `attrs.cached` like
    `nexus/people/enrich.py`. The customer is charged for the answer, not for our infrastructure:
    the saving is **COGS, not price**. Charging only on a source hit would make revenue depend on
    our procurement.
  - `phone` is mappable on a person but is **never an identity** — a switchboard is shared by a
    whole company. Lookups may only be keyed on the fields in `engine.LOOKUP_FIELDS`.

## Enrichment billing (`enrich.account`, `enrich.contact`)

Both capabilities were catalogued, priced in `rates.py` and entitled on the Core plan while being
metered at **no call site**. Enrichment is the most expensive thing the product does per unit — a
search request, an LLM completion, a verification credit, sometimes an actor run — and none of it
reached the usage stream, so quota and margin were unmeasurable for the largest line of COGS.

The seam is the enricher, not the call site: `SearchBackedAccountEnricher.enrich` and
`WaterfallEnricher.enrich_contact` each own both the free and the paid branch, so "a source
database hit is billed like the paid provider it replaced" holds by construction rather than by
five call sites remembering to agree.

- **Providers are split by `costs_money`, and that split is the billing boundary.** Free ones
  (today only `SourceDatabaseProvider`) run *outside* the meter; only the paid remainder runs
  inside it. `costs_money` defaults **True** so a new provider that spends money but forgets to
  say so cannot silently become free.
- **`raise_on_block` is the difference between a person and a sweep.** The two `/enrich` endpoints
  pass True and a blocked tenant gets a 402 carrying the upsell — a silent no-op there is
  indistinguishable from "we looked and found nothing". Everything else (the refresh pipeline, ICP
  discovery, lookalike, campaign sourcing) takes the default False: **an enrichment quota must
  never take down signal collection**, and `pipeline.process_account` has no `try/except` around
  its enrichment call, so an escaping 402 would do exactly that.
- **Concurrent batches charge once, up front** (`enrich_batch`). `metered()` reads and writes the
  TenantSession, and the candidate sweeps in `discovery/auto.py` and `lookalike/service.py`
  `gather` over N accounts — metering inside that gather is the AsyncSession concurrency trap this
  file documents for session-bound signal sources. One row with `quantity=N` before any of it
  starts, the same shape as the bulk verifier in `routers/contacts.py`. `meter=False` on `enrich`
  exists **only** for those two callers.
- **Nothing bought, nothing charged.** An account with no name and no domain never issues a
  request, so it is not billed — the rule that also keeps an unconfigured phone lookup off the bill.
- On today's default `NEXUS_BILLING_ENFORCEMENT=shadow` this records and never blocks, so the
  `would_block` counter is what says what flipping enforcement on would cost each tenant.
- **The B2B actor is spaced like the paid web path** (2026-10-14, `_enrich`). It ran on every
  enrichment, ignoring the 30-day `due` check, and the pipeline enriches any account still missing
  industry OR headcount, which the actor rarely supplies: 82 runs in three hours locally, and the
  Apify credits gone within two days. It now runs only when the account is due.

## Email verification through the registry (`DataSourceRegistry.verify_email`)

**Verification is exempt from the registry's budget, breaker and lifetime cache.** Those three exist
so a paid search source cannot run away on cost, but `get_registry()` is a process-wide singleton,
so each became a lifetime limit. Found 2026-09-14 on marketjoy.com: the live Reacher answered
`curtis.bent@` invalid and `curtis@` valid, while the product showed `curtis.bent@` risky/unknown.

- 64 checks per process is about six people through the finder (10 patterns each). After that
  every check answered `unknown` without calling the verifier, so the finder kept first.last.
- A breaker that opened after three errors never closed.
- The cache kept `unknown` and the DNS fallback's `risky`. When Reacher cannot answer, DNS grades
  every address on an MX domain `risky` 0.5, every pattern ties, the finder keeps first.last — and
  that verdict was then replayed for the life of the process, so retrying changed nothing.

Now only a conclusive verdict (`valid`, `invalid`, `catch_all`) is reused, for `VERIFY_CACHE_TTL_S`
(1h). A raising verifier degrades to `unknown` and is asked again next time; Reacher keeps its own
breaker, which closes after a cooldown; spend is metered by `enrich.contact`, not here.

**Do not relabel a DNS-fallback `risky` as `unknown` to make it look "unverified".** The campaign
send gate holds `risky` unless the campaign opts in, and SENDS `unknown` for real (non-sourced)
contacts — so the relabel widens who gets bulk-sent. `tests/test_email_verify_registry.py` pins this.

**Re-verifying one contact is charged once, for whichever it turned out to be** (decided with the
product owner 2026-09-14; `enrichment/reverify.reverify_or_find`, `POST /accounts/contacts/{id}/reverify`).
It re-checks the saved address with a fresh verifier (never the registry cache). Valid, risky or
catch-all keeps the address and charges one `verify.email`. Invalid, unknown or no address runs the
pattern search (first.last, first, ... stopping at the first valid) and charges one `enrich.contact`
INSTEAD. The verdict is written only inside the meter, so a 402 changes nothing on the contact; an
invalid address is REMOVED rather than relabelled (see the save policy below), which also clears the
confidence the waterfall would otherwise compare against. Enrich keeps its 30-day cool-down for a
valid address; Re-verify has none, since it is the explicit "check again".
`tests/test_reverify_one_contact.py` pins it.

## What may be saved as someone's email address (`nexus/enrichment/policy.py`)

Customers reported bulk bounces. The finder was doing what it was built to do and the **save** was
wrong: an address that had verified **invalid** was kept and merely labelled, a pattern guess nothing
had checked was kept at 0.55, and both then appeared on the contact as that person's email — which
is what a rep sends to and what a campaign bulk-sends to.

`keep_address(status, check)` is the single rule, and nothing else decides:

| Verdict | Kept? |
|---|---|
| `valid` | yes |
| `catch_all` | yes — the domain accepts everything, so nobody can prove it wrong |
| `risky` | only when a **mailbox** verifier said so |
| `unknown` | no |
| `invalid` | **never**, under any configuration |

Decided with the product owner 2026-09-22: **whether to send to catch-all and risky is the
customer's call, not ours** — the campaign gate already holds `risky` unless the campaign opts in —
but an address proven dead is not a judgement call and is not kept at all.

- **`risky` turns on WHO said it.** Reacher grading one mailbox risky is evidence about that
  mailbox; the DNS fallback grades *every* address on an MX domain risky 0.5, which is evidence
  about the domain and says nothing about the person. `check_level` reads the verdict's `source`
  (`reacher+...` → mailbox, `dns` → domain), so the same word is trusted in one case and not the
  other.
- **The whole policy is scoped by `verification_configured()`.** With no verifier — the stub, dev,
  the offline suite — nothing is checkable, so the pre-existing behaviour stands. Without that
  scope an unverified deployment would save *nothing* and the finder would look broken. This is
  what keeps a green suite meaningful rather than merely green.
- **A refused address is remembered** (`rejected_emails` on `custom_fields`, newest first, capped at
  `MAX_REJECTED`). The finder is deterministic, so without this it re-derives the same dead address
  on the next crawl and the customer watches it come back. Only *disproved* addresses go in — an
  unverified guess is forgotten, not blacklisted, because nothing proved it wrong.

## The domain a company receives mail on (`nexus/enrichment/mail_domain.py`)

The root cause under most of the invalid addresses: **the website domain and the mail domain are
not always the same**, and everything downstream keyed on `account.domain`. Guessing
`first.last@` on the wrong domain produces an address that is well-formed, plausible, and dead.

An evidence ladder, best evidence first, cached on `account.custom_fields["mail_domain"]` for 30
days. **Every rung except the first must also have MX**, which is what stops a parked or redirect-only
domain being adopted:

1. **`verified_contacts`** — the domain of a colleague's address that a verifier already called
   valid. Mail demonstrably arrives there, which is why it is the one rung exempt from the MX check:
   it has already been proven by something stronger than a DNS record.
2. **`website_emails`** — a real address published on the company's own site (homepage, then
   `/contact`). The company is telling you where to write. Role mailboxes are filtered, and a
   name-shaped local part also seeds `format_index`.
3. **`website_redirect`** — where the website actually landed, when it differs from the stored
   domain. A rebrand moves mail before anyone updates the CRM.
4. **`website_domain`** — the stored domain, recorded *as such*, so a later step can tell that the
   answer was assumed rather than confirmed. That distinction is what `force=` reads.

`infer_format_index` votes across colleagues whose verified address matches one of their own name
patterns — two people at `jdoe@` and `jsmith@` say the format is first-initial+last. It matters most
on a catch-all domain, where the address that comes back cannot be proven and had better be the
company's real format rather than a default `first.last`.

`resolve_mail_domain(..., force=True)` re-runs when a contact already has rejected addresses AND the
cached answer is only website-derived — which is exactly the "we suspect the domain is wrong, go and
find the right one" case, and the reason the cache is not simply trusted. Fetching is SSRF-guarded
and total: unreachable, slow or empty returns *no answer*, never an exception.

`ordered_candidates` / `name_patterns` (`providers.py`) are shared by the pattern finder and the
site scraper, so "which addresses are worth checking for this person" has one definition. A
published address that matches the person's name is tried first, then the company's observed format,
then the ten corporate patterns.

**A scraped address must be about the person asked for.** `SearchEnrichmentProvider` now requires
the host to be the mail domain AND the local part to match one of that person's name patterns. It
used to accept any address on the page at 0.55, which attached `info@`, `careers@` and — worse — a
colleague's address to whoever was being enriched. Same class of mistake as the six
wrong-*company* bugs, aimed at a person.

**Resolving a company's domain from its name** is `company_domain.py`, behind
`GET /accounts/company-domain`, and it only ever **prefills** the similar-person add form for a rep to
confirm. The server deliberately does **not** fill a blank domain on `from-lookalike` itself. It did
for one commit, and that broke two things: `find_existing_account` falls back to a name match only
when there is *no* domain, so a searched domain stopped a bare "globex" from reusing the workspace's
own domainless "Globex Inc." and created a duplicate; and once the form prefills, a blank arriving at
the server usually means the rep *cleared* a suggestion they judged wrong, so filling it back files
the account under a domain a person already refused. A name match cannot say which company was meant
— measured live, "Globex" resolves to a login page at `globex.international`. Directory aggregators
are excluded by the same `_NON_COMPANY_HOSTS` discovery uses.

**A similar person's employer comes from their current role, and its domain from the company's own
LinkedIn page** (2026-10-02, `tests/test_similar_person_employer.py`). Reported as "company and
domain still have to be typed"; measured for one seed, 4 of 10 results had no company and 2 more a
territory or team ("UK&I", "Security & Cloud"). Every LinkedIn snippet Exa returns has an
`## Experience` section whose role marked `(Current)` links the company page; the parser read only
the headline. `lookalike/contacts._current_employer` reads that entry (inline `### Title - [Co](url)
(Current)` or grouped `### [Co](url)` + `#### Title (Current)`), linked companies only, never a past
role, ahead of the title and headline parses. The result carries `company_linkedin_url`, and
`GET /accounts/company-domain?linkedin_url=` resolves it through
`company_domain.resolve_from_linkedin_page`: the shared store, then one company-details run stored
for every workspace, a no-website page remembered 30 days, then the old name search. Still a
prefill: the form says "From X's LinkedIn page. Check it is the right company."

`scripts/clean_invalid_emails.py` clears what was saved before all of this — dry run by default.

## LinkedIn prospecting (`nexus/prospecting/`)

ICP companies, Find similar and Find contacts ask, in order, **the shared database, then the
harvestapi LinkedIn actors, then Exa** (decided with the product owner 2026-09-24). Spec and plan:
`docs/superpowers/{specs,plans}/2026-09-24-linkedin-prospecting*`. Every actor was run once against
real input before its parser was written; the fixtures in the tests are synthetic.

**The domain defines the account** (product owner). `linkedin.company_domain` accepts a row's own
website only: not a social profile, directory, app store, link-in-bio page or shared host
(`sites.google.com`, `github.com`). A row with no usable website is counted as `no_website` and is
**neither delivered nor stored**. A LinkedIn page belongs to an account only when `page_is_for`
says its website IS the account's domain. Measured: `linkedin.com/company/vanta` is *VANTA -
Chauffeurs* (vantaexec.co.uk); Vanta the security company is `vanta-security`. Asked about the
first, the employees actor returned a fleet coordinator at a leasing firm, which is what an
unproven page does to a contact list.

**ICP companies** (`companies.find_icp_companies`), used by populate and by the daily sweep:

1. The shared store, on the ICP's LinkedIn industry codes (a parent code covers its children), any
   office in the ICP's countries (`company_countries`: LinkedIn's location filter matches ANY
   office, measured — a UK search returned US-headquartered companies with a London office), and an
   overlapping size band. "Not held" is an SQL anti-join against the workspace's accounts. The old
   discovery sent at most 256 held domains to Exa as exclusions, so a 3,500-account workspace got
   the same top results every day; pinned with 299 held and the new one still found.
   **Then companies any workspace added any other way** (2026-10-02, decided with the product
   owner): an upload, CRM pull, hand-add or web find lands in `companies` within a tick through the
   backfill, but with no LinkedIn codes, so step 1 could never offer it: 256 of 393 shared rows
   locally. `resolution.derive_codes` now reads the industry code, HQ country and size band off
   the row's own text on every non-LinkedIn write (fill blanks only, `match_term`, never the LLM),
   leaving `linkedin_fetched_at` NULL as the provenance mark. Those rows (source `shared`) are
   offered after the LinkedIn-stored ones and only when `deliver.fit_screen`, the workspace's
   own fit threshold, passes; `deliver` holds them to it again. No screen, no shared step. A
   LinkedIn read replaces the derived values and drops derived countries it does not list.
   `backfill.fill_from_linked_accounts` hands a linked row what its account learned AFTER the link
   (enrichment, a CRM pull, an edit): the link read the account once and never again.
   `scripts/derive_company_codes.py` (dry run by default) covers rows stored before.
2. LinkedIn for the shortfall, from `prospect_cursors`, which is **shared by every workspace**:
   every company on a page read is stored, so the next workspace with the same ICP gets it from
   step 1 and never pays to read the page again. A page is claimed by compare-and-set and released
   if the read fails. Past the 1,000-result ceiling (20 pages of 50) a query splits by size band,
   then country, then industry. An exhausted cursor reopens after 30 days. Page budget
   `1 + need/10`, at most 10.
3. Web search (Exa) for whatever the first two left short, through the same gates, with the old
   sweep's pool, enrichment and fit threshold. LinkedIn and database candidates are ICP matches by
   construction and skip the threshold (`deliver.py`, shared by both callers). **Changed
   2026-09-30:** it used to run only when LinkedIn delivered NOTHING, so a partial LinkedIn day was
   never topped up; it now fills any shortfall, which costs one extra web search on a short day.
   **Exa is never sent `excludeDomains` with `category=company`** (2026-10-14,
   `ExaSearchProvider.search_companies`): measured, that pair answers 200 with zero results and
   zero cost, which emptied this step, Find similar's web fallback and the Orchestrator's web fill
   for any workspace holding an account. Held domains are filtered from the hits instead, and the
   excluded count is asked for on top.

**The daily sweep reaches the workspace's number, or says why not** (`handle_discover_icp_accounts`,
2026-09-30). Reported as "new ICP accounts are not updating daily to the number set in Settings";
measured on the local deploy, a workspace set to 10 a day had received 1-3 a day for two weeks. The
sweep made ONE pass per interval, took what the strict gates let through (most web candidates fail
the 70 fit threshold, and the configured web backend was out of credit), consumed the day's slot
whatever it delivered, and recorded nothing, so nobody could tell a broken provider from a small
market.

- `Tenant.icp_discovery_last_run_at` is now the START of the interval. A short day gets another
  pass for the remainder, at most `icp_discovery_max_attempts` (3) passes, at least
  `icp_discovery_retry_hours` (3) apart, both Runtime settings. A clean pass that found nothing new
  (no source failed) ends the day early, so an exhausted market does not keep spending.
- Every pass is a `ProspectRun(kind="daily")` with requested, delivered, sources, discarded and
  notes. The tally, the retry decision and the reason are all read from those rows (populate's
  one-at-a-time guard filters on `kind="populate"`, so the two never block each other).
  `GET /workspace/automation` returns `today` (`target`, `delivered`, `attempts`, `short_reason`)
  and Settings shows it under the daily number: "Today: 2 of 10 ... web search failed; 12 below
  your fit threshold". A workspace whose plan excludes `discovery.account_added` is recorded as
  `not_entitled`, is not asked again that day, and reads "your plan does not include daily
  discovery" rather than "still looking" (found on the first live run: two Free workspaces).
- **First live run (local deploy, 2026-09-30):** Nexus Platform 20 of 20 from LinkedIn, whose pages
  landed in the shared store; Infojoy then 10 of 10 from that store at no cost. Before the change the
  same workspace had received 1-3 a day for two weeks.
- **One workspace failing no longer stops the rest.** Each tenant runs in its own try; a pass that
  raised is recorded as `failed` in a fresh transaction, opening the interval, so the retry gap
  applies to it instead of re-running it on every tick.
- The industry CSV and the ledger SQL are declared as `[tool.setuptools.package-data]`. The
  pip-installed copy in the image lacked both, and only `/app` coming first on `sys.path` kept the
  app and worker from raising `FileNotFoundError`; a script run from anywhere else did.

**Industry codes** (`industries.py`): the 434 v2 codes are vendored and held in memory. Mapping is
whole-word (a substring match filed "Artificial Intelligence" under *Artificial Rubber
Manufacturing*), synonym-led ("SaaS", "fintech", "AI" match no label), and asks the LLM only for
misses, from a ~20-label shortlist, discarding any id not on it. Saving an ICP stores the result as
`icp.linkedin_industry_ids`, derived and never taken from the request. The relevance engine counts
an account whose industry maps into those codes as an industry match: without it every
LinkedIn-sourced account ("Software Development") scored 0 on industry against "SaaS".

**Populate** (`populate.py`, `POST /discovery/populate`, `manage_relevance`): the Relevance page
asks "how many companies now?" after a save that changed the ICP (`icp_changed`). The balance is
preflighted for the WHOLE count (402 before anything is bought), one populate runs at a time (409),
and `discovery.account_added` (5 credits) is charged once for the companies delivered, keyed on the
run, in the transaction that creates them. **Saving an ICP no longer starts a discovery batch by
itself**: that batch would add, and bill, companies nobody asked for on top of the number chosen.
Free does not include `module.discovery`, so under the default `shadow` its charge is recorded and
burns nothing; with enforcement on it is refused with the upsell. Verified on Launch: 2 delivered
moved the balance 200 -> 190.

**Find similar** (`similar.py`, first in `LookalikeService.find`): the seed's proven page, its
similar pages (LinkedIn lists ~12 and **none carries a website**, 0 of 72 observed), each resolved
to a domain from the shared store or ONE bulk details run, stored for everyone. A page with no
website is remembered in `web_cache` for 30 days so it is not re-bought on every click. 90s budget,
then Exa. **Find contacts** (`contacts.py`, first in `campaigns/sourcing.source_account_contacts`):
the proven page's people with the ICP's titles only (no titles, no LinkedIn call: the only request
left is "everyone"); each row must name this company and fit a title; deduped by normalised
LinkedIn profile and by name, which the web path now also does. Emails come from our verified
finder, which charges `enrich.contact` as before.

Measured prices (Apify, 2026-09-24): company search $0.004 a company in full mode ($0.20 a page),
details $0.004 a company, employees $0.003 a profile plus $0.02 a run. **The first of the two
configured Apify keys answers 401** and rotation skips it; replace it.

## Pay-per-result searches ask first (`preflight`)

Both lookalike endpoints bill per result DELIVERED (`discovery.lookalike_company`,
`discovery.lookalike_contact` with `mode=new`), so the charge can only come after the Exa search —
and `routers/accounts.py` `_meter` swallows its own refusal, because by then the customer is holding
the result. Together that meant a refused plan was never refused. Measured 2026-09-11 on a `free`
workspace with enforcement on: ten people sourced, `QuotaExceeded: dependency` in the log, no usage
row, no upsell. `_meter`'s "enforcement applies to the NEXT call" never held — the next call was
refused and swallowed the same way.

- **`preflight(ts, capability_id, quantity=)`** (`billing/entitlements.py`) answers "would
  `check_and_meter` refuse this?" and records, burns, locks and counts nothing. It runs the same
  `_decide` table as the meter; only the credits branch differs (`_can_cover` reads the balance
  where `_burn_for_usage` burns it), so the two cannot drift. `check_and_meter` itself cannot be
  asked first: it burns credits as part of deciding.
- **Asked for the full `limit`**, not for one result. Whatever the search then delivers was
  affordable, so the charge afterwards is never refused — short of a concurrent spend in between.
  The price of that: a balance short of the limit is refused even when the search would have
  returned fewer. Chosen over delivering the overshoot free (repeatable, since a refused burn never
  moves the balance) and over overdrafting (negative balances left with overage billing).
- **The meter's mode rules, exactly.** `off` evaluates nothing. `shadow` refuses only a platform
  switch — so switching off `module.discovery` now stops lookalikes before the search, where before
  its 402 came after the search and was swallowed. `on` refuses whatever the meter would.
- `mode=existing` is never asked: it reads the workspace's own contacts, spends nothing, and is not
  metered.

## Orchestrator intake (`nexus/orchestration/intake.py`)

**A question is not a launch instruction.** `advance` used to launch on `is_first_turn` alone once
the ICP had no missing slots, so a workspace with a saved ICP turned *any* opening message into a
`discover` run. Opening the orchestrator from an account page and typing "What is the ICP fit for
Marketjoy and why?" re-scored every account in the workspace, never answered the question, and
billed a full run for it.

`_is_question` is deterministic and narrow — a trailing `?` or an interrogative opener — matching
the rest of this controller, where the LLM only phrases and summarises. It deliberately excludes
`find`/`show`/`get`/`list`: "show me fintech CFOs in the UK" **is** an instruction and must keep
launching immediately, which is the whole point of the first-turn shortcut. Being wrong cautiously
costs one "yes" (`_is_affirmative` already handles it); being wrong the other way spends a run.

Note the orchestrator has exactly one destination — it is an ICP intake funnel that ends in
`discover`. There is still no path for a scoped question about an account already in context.

**`discover` adds NEW companies whatever the workspace already holds** (2026-10-01,
`agents/discovery.py`, `tests/test_orchestrator_discovery_adds.py`). Reported as "it says 25
companies and none appear in Accounts"; reproduced on the local deploy as `counts: {own: 25, new: 0}`.
The agent filled its `discovery_max_candidates` (25) slots with the workspace's own accounts by fit
and searched only for the remainder, so any workspace past 25 accounts never got a new one. Own
matches are now listed as context and up to the same number of new companies come from the chain
Populate and the daily sweep use (`find_icp_companies` → `deliver`), at their price and gates:

- **Balance checked for the whole request before anything is searched**, and a refusal is a note in
  the results, not a failed run (the own list still shows). Added companies and their
  `discovery.account_added` charge (keyed `discover:<run_id>`) share a SAVEPOINT, so a charge
  refused at the last moment takes the accounts with it. Decided with the product owner: charged
  like Populate, 5 credits each.
- **"High relevance" is the sweep's threshold**: web candidates must clear `icp_discovery_min_fit`;
  LinkedIn and shared-store candidates are ICP matches by construction. The ICP's industries are
  mapped to LinkedIn codes first, so a "Software Development" account scores as a "SaaS" match.
- **It runs inside the chat request**, so `find_icp_companies(linkedin_budget_s=...)`
  (`discovery_linkedin_budget_s`, 90s like Find similar) stops LinkedIn and lets web search fill in,
  keeping what LinkedIn already delivered. Populate and the sweep are jobs and pass nothing.
- `GET /orchestration/runs/{id}/results` returns `shortfall`, the sweep's own wording for why fewer
  were added than asked, and the panel shows "N of M added" or "No new companies were added: ...".
- On SQLite the chain's platform-session writes (LinkedIn pages into the shared store) wait on the
  request's tenant transaction, which has already written the run; the chain notes LinkedIn
  "failed" and web search fills in. Postgres does not lock (different tables). Tests seed held
  accounts in a committed session first, as the sweep's tests do.

## Two counters, two tables

`analytics.overview` reports **`agent_actions`** (`count(AgentRun)` — one row per individual agent
execution: a research brief, a draft, a scoring pass). The **AI Runs** page lists
`OrchestrationRun` — one row per multi-agent orchestrator session. The key used to be named
`agent_runs`, and the dashboard renders each key through `humanize()`, so the tile read "Agent Runs"
directly beside a nav item called "AI Runs" while counting a different table. A workspace that has
scored accounts but never opened the orchestrator genuinely has many agent actions and zero runs
(observed: 6 vs 0). Neither number was wrong; only the label was.

`RunOut` also carries **`step_total` / `step_done`**. The runs LIST builds it without steps —
shipping every step's `output` blob to render one "3/5" label is not worth it — so it reported
`steps: []` and the UI computed "0/0 steps" for runs that had completed. When steps *are* supplied
the counts derive from them, so list and detail cannot disagree.

**`/analytics/overview` is gated on `manage_accounts` and SCOPED by `view_analytics`, not refused
by it.** It was manager-only while the dashboard every rep lands on called it, so a rep's first
screen was "Role 'rep' lacks view_analytics". Managers+ get the workspace overview unchanged; a rep
gets `overview_for_user`. That shows `open_tasks`, which is tasks assigned to them plus unassigned
ones (the queue they work), and `my_agent_actions`. The second comes from the usage stream, because
`AgentRun` carries no user; refunds net out and `ai.tokens` is excluded. Next to those sit the
shared-book counts a rep can already browse. Agent failures and plays executed are left out rather
than zeroed. The activity feed and outcome attribution keep their manager-only gate.
`test_rep_dashboard.py` pins both halves.

## Feature switches (`nexus/features/switches.py`) — superadmin, platform-wide

A superadmin takes a feature offline for **every** workspace, with a message, without a deploy.
Four states: `enabled | disabled | coming_soon | maintenance`. Gated on `features.manage`,
superadmin preset only — the blast radius is the whole platform and, unlike a price change, it is
immediately visible to every user. Not folded into `pricing.write`: what a feature costs and
whether it runs at all are different decisions, and only the second shows up as an outage.

**Keyed on the existing `module.*` capability ids, not a new page registry.** Those ids already
drive all three enforcement points — the nav item (`nav.tsx` carries `capability`), the route
(`RequireCapability`) and the endpoints behind them (`depends_on`) — so a switch reaches all three
without any of them changing, and a page shipped later is covered the moment it is given a
capability, which it needs anyway to be sellable. A registry would be a fourth source of truth, and
the first thing to drift would be which pages it covers.

**The absence of a row means enabled**, which is what makes `feature_switches` (migration `0055`,
no `tenant_id`) additive. Everything fails open: an unreadable table, an unknown state, a lookup
that raises all resolve to `enabled`. A switch is a restriction, so failing to read one applies
none. 30s TTL, because the worker is a separate container and would otherwise need a redeploy —
the thing this removes. The writing process drops its own cache so the console does not feel broken.

Four placement decisions, each load-bearing:

- **The check is FIRST in `resolve_entitlement`**, before the catalog lookup and before every early
  return. Two of those returns would otherwise be escapes: an `unlimited` plan class bypasses module
  gates by definition — and that is `legacy-unlimited`, every pre-billing tenant — and "no
  subscription → allow".
- **A switch beats the plan escape in `_apply_dependencies`.** That escape says "this customer
  bought this specific action, so a blanket module gate should not take it away", a claim about
  entitlement. A switch says "this does not work right now, for anyone". Left alone, taking Outreach
  offline still ran Free's 20 email drafts against the broken subsystem.
- **It is enforced whatever `NEXUS_BILLING_ENFORCEMENT` says, `off` excepted.** Shadow is a
  statement about billing rollout and production defaults to it, so riding on it would have made the
  control inert exactly where it matters: flip the switch, the panel reports disabled, every
  customer keeps using the feature. `off` still wins because it is documented as a full kill switch.
- **A switched-off call is not metered.** It did no work, and billing for it charges the customer
  for our outage.

**`switch_state` and `switch_message` ride on `ResolvedEntitlement` and on the 402**, because
"coming soon", "we broke it" and "your plan lacks this" are one `mode="disabled"` and three
completely different sentences. Without them the client renders the generic upsell for a feature we
took down ourselves.

**A new permission reaches nobody who has a stored `permissions` list.** Those store the EXPANDED
set on purpose, so redefining "support" tomorrow cannot re-grant power to people provisioned today.
Migration `0055` therefore backfills, testing the PERMISSIONS rather than the role: a row holding
every permission that existed before this one meant "everything" and gets it; a narrowed superadmin
— which `POST /admin/billing/admins` can create — stays narrowed.

**Client-side, a switch and a plan gate need opposite screens** (`FeatureUnavailable`,
`navState`). A plan gate is an upsell and `/settings/billing` is a real destination; a switch is our
decision that no plan reverses, so the route renders in place instead — which also keeps the URL, so
a refresh after the switch is flipped back lands the user where they were going. The nav item is
shown to **everyone**, inverting the agency rule right above it: that rule hides a plan lock from
anyone who cannot upgrade, but a switch is a status message and the person who most needs it is the
rep whose daily driver went quiet. `included` + `gating_active` + `switch_state` is folded into a
server-computed `locked`, because the sidebar and `RequireCapability` are two readers of one
three-way rule.

**Hiding a page is per PAGE, not per module** (`nexus/features/pages.py`, migration `0066`,
2026-09-29). Asked for as "I don't want to show Approvals or Network": Approvals shares
`module.agents` with Orchestrator and AI Runs, so a module switch could not take it alone.
`PUT /admin/features/pages/{key}` (`features.manage`, audited as `feature.page_visibility`) hides a
menu page from every workspace; `hidden_pages` rides on `GET /billing/entitlements`, the Sidebar
leaves it out and `RequirePage` sends the page and every sub-route to Dashboard. **It is
presentation plus the route only**: the endpoints behind the page keep answering, because taking a
feature away is the module switch's job. `HIDEABLE_PAGES` is pinned to `nav.tsx` minus the floor
(Dashboard, Accounts, Contacts, Members, Settings, Billing, the console), and every route under a
hideable path must carry `RequirePage` (`tests/test_page_visibility.py`). No row means shown, and
an unreadable table hides nothing.

**`/api/orchestration/runs` had no billing seam at all** — found by testing the endpoint half.
`workflow.orchestration_run` is catalogued, priced and carried by `module.agents`, and was metered
only in `routers/agents.py` on the account-pipeline endpoint, so the screen the capability is named
for ran free and the module gate reached the nav item and the route guard but not the API. Now
metered around create *and* execute, since the run is driven inline.

## Where the credits went (`nexus/billing/usage_report.py`)

`GET /billing/usage/credits`: period granted/spent/balance, then by capability, by day and by user.
Built from the credit **ledger** rather than the usage stream, so the numbers reconcile against the
balance the customer can check. `granted - spent == balance` deliberately does **not** hold — those
are period figures and a balance can carry over.

**Attribution is partial by construction.** Only usage events carry a user id and a refresh sweep
has nobody to attribute to, so `by_user` cannot sum to `spent`. The remainder is reported as its own
row; dropping it leaves the customer to find the gap themselves against a number they will check.

**Each row names a person, never an id.** The screen was written to show `email` and fall back to
`user_id`, and `CreditUserRowOut` declared only `user_id` and `credits` — pydantic drops what a
response model does not declare, so customers saw rows like `4a88e2a9a2e8438f96f3295fe51ccebd`
(reported 2026-09-23). Rows now carry `name` and `email`, resolved through THIS workspace's
membership rather than the global user table: a usage row's user id is only the workspace's to name
while that person belongs to it, and someone who has left reads "Former member". The same trap hid
`dated` on the call brief (`CallBriefSignal`) until it was declared.

The old `/billing/usage` meters drew "40 of 500" against the plan quota. Under credits-only billing
the quota is not what gates a priced action — the balance is — so a workspace with an empty balance
saw green meters beside a product that had stopped working. `BillingPage` keeps them as **Plan
limits**, scoped to gauges, where a quota is still the literal truth.

## Plan-gated navigation (`frontend/src/app/EntitlementsContext.tsx`)

The sidebar was blind to entitlements, so a `free` workspace saw Network and Campaigns and found
out by clicking and getting a 402. `GET /billing/entitlements` resolves the `module.*` gates
through the real engine and drives the nav.

**The trap is bigger than the bug.** `NEXUS_BILLING_ENFORCEMENT` defaults to `shadow`, which
resolves every entitlement and then **allows anyway**. A UI that hid an item because the policy said
"disabled" would hide a feature that still works — turning a rollout mode whose entire promise is
"changes nothing" into a visible regression. So the endpoint returns `gating_active` (true only when
enforcement is `on`) and the client gates on **that**, never on `included` alone. On today's default
deployment this change is a strict no-op.

- **Hide or upsell? Both — decided by agency.** `admin`/`owner` can change the plan, so a locked
  item is actionable and is the upsell; `rep`/`manager` cannot, so for them it is a permanent
  advertisement for something they may not buy, and it is hidden. Nav is already role-dependent
  (`minRole`), so this is the established model rather than a new concept. To make it uniform,
  change `navState` in `app/nav.tsx` — nothing else moves.
- A locked item routes to **/settings/billing**, not to the feature: sending someone to a page the
  server will 402 is the dead end this change exists to remove.
- Missing/in-flight/errored entitlements resolve to **not locked**, matching the engine's own bias
  that unknown means allow. A billing endpoint blip must never delete the customer's navigation.
- Only coarse `module.*` gates belong in nav. Per-action quotas stay on the action — a menu that
  greyed out at 19 of 20 drafts would be lying about a feature the customer still has.

**Hiding a link is presentation; `RequireCapability` in `App.tsx` is the access control.** Until it
existed, a gated page was reachable by typing the URL, following a bookmark, or being sent one by a
colleague on a richer plan — the nav hid Campaigns and the route served it. Every capability that
gates a nav item now also guards its route, asserted structurally by
`test_the_routes_guard_the_same_capabilities_the_nav_hides` (there is no frontend test runner, so
these tests read the source). It fails **open** for the same reasons `isLocked` does, and the server
remains the real boundary — each of those routes calls endpoints that meter and 402 on their own.
For a `rep` the redirect target is itself admin-only, so the chain is
`/signals → /settings/billing → /dashboard`; it terminates because both hops use `replace`.

**Nine of the twenty-two nav destinations carried no capability at all**, so "sell them Accounts and
Contacts only" was not expressible by any plan: a bespoke deal could disable Outreach, Calling,
Network and Integrations and the customer still saw Inbox, Signals, Alerts, Lists, Plays, Relevance,
Orchestrator, Runs and Approvals. `module.signals` (Inbox + Signals + Alerts), `module.lists`,
`module.plays`, `module.relevance` and `module.agents` (Orchestrator + Runs + Approvals) close it.
All five default to `enabled` and are on **no** plan, and `resolve_entitlement` falls back to the
catalog default for a capability a plan does not list — so adding them is a strict no-op until an
operator disables one, pinned by `test_the_new_module_gates_change_nothing_for_existing_plans`.

**Dashboard, Accounts, Contacts, Members, Settings and Billing are deliberately ungateable**, and
`test_the_floor_of_the_product_is_never_gated` keeps them that way. Billing is the load-bearing one:
gating it behind a plan locks the customer out of the only page where they could change the plan
that locked them out.

**Two ways to sell a restricted product, and they are not interchangeable.** `CustomPlanDialog`
builds a per-tenant plan (`plan_class="custom"`) — module checkboxes seeded from the base plan, and
only *changed* rows are sent, so a module added to the base plan next quarter still reaches the
customer. Custom and enterprise plans are refused by `/billing/checkout` and `/billing/portal` with
a **409** (`_reject_if_admin_managed`) and are billed by `collect_invoice`. A repeatable public tier
is instead a standard plan edited through `PlanEntitlementsDialog`, which reaches self-serve
Checkout. A ninth public tier no longer needs a `plans.py` change and a deploy: `POST
/admin/billing/plans` builds one (see *Authoring a sellable plan* above).

**`core` ($19, sort 18) is the worked example of a restricted tier**: eight modules off, leaving the
ungateable floor plus Lists and Relevance. `ai.scoring` is deliberately **not** tied to
`module.relevance` — relevance scores are the most useful column on the Accounts page, which every
plan includes, so cascading it would sell a page with its point removed. `automation.account_refresh`
is tied to nothing for the same reason.

**A module gate that only hides a menu item is a discount with no cost saving.** `depends_on` is
what makes a cheaper plan cheaper to *serve*: `module.signals` carries the signal scans, `signal.stored`
and `inbox.task`; `module.agents` carries the orchestration run/step and `ai.chat_turn`; `module.plays`
carries `automation.play_run`. Adding those was safe only because no pre-existing plan disables those
modules — pinned by `test_the_new_dependencies_change_nothing_for_existing_plans`.

**`GET /billing/plans` is the customer-side price list, and `PlanPicker` is the screen that buys
one.** `/billing/checkout` and `/billing/portal` existed server-side and **no screen called either**,
so a workspace could not change its own plan from inside the product — which became load-bearing the
moment locked navigation started routing people to `/settings/billing` to "view upgrade options".
The endpoint omits what checkout would refuse (custom, enterprise) plus `unlimited`, `internal` and
`trial`, because listing a plan whose next click 409s is worse than not listing it; a plan leaves the
list by having its `status` changed in Admin, with no deploy. Module inclusion is resolved **per
plan**, not against the caller's subscription — the question a picker answers is "what would I get if
I switched". `UsageOut.plan_class` exists so the client can tell an admin-managed deal from a listed
tier without sniffing the plan id for a `custom-` prefix. **No Stripe object is created when a plan is
seeded**: `create_checkout` calls `ensure_plan_price` on first purchase and caches `price_id` into
`plan.meta`, verified live — `growth` carries a cached price, `core` stays `{}` until someone buys it.

## CRM and telephony: what is actually connected

Both were once reported as "users can't add credentials", and neither had a credential surface at
all. Both now do, and the shape differs because the problems differ.

**CRM credentials are per-tenant** (`nexus/ingestion/crm_credentials.py`, migration `0047`), not
deployment-global env vars. Each workspace connects its own HubSpot or Salesforce, by pasted token
or by OAuth (`nexus/integrations/oauth.py`); the token is Fernet-sealed and never returned. The env
vars remain as the **fallback** — `get_crm_connector()` is the deployment default and
`resolve_crm_connector()` is the per-tenant path, so a deployment that never connects anything keeps
working exactly as before.

**Both providers are live.** HubSpot returned 99 companies on a real token; Salesforce is a real
OAuth2 + REST adapter with SOQL fetch and push, listed in `LIVE_CRM_PROVIDERS`. It spent a release
working while the UI still offered it as "coming soon" and disabled — the inverse of
configured-and-doing-nothing, and `test_the_dropdown_offers_exactly_the_providers_the_server_accepts`
now pins the two lists together.

`_soql_escape` handles the quote, which is the injection. `_soql_like` additionally escapes `%` and
`_`, which are LIKE **wildcards** — not dangerous, but a domain containing an underscore silently
matches accounts it should not, and that query decides which of the customer's accounts a contact
gets written onto.

**Twilio is real** (`nexus/calling/twilio.py`). `build_call_provider` used to return the stub for
*every* input, so `NEXUS_TELEPHONY_PROVIDER=twilio` behaved exactly like leaving it blank, and
`get_call_provider()` had no callers at all — the setting was inert twice over. It now raises
`TelephonyNotConfigured` naming the exact env vars (`NEXUS_TWILIO_ACCOUNT_SID`,
`NEXUS_TWILIO_AUTH_TOKEN`), resolved in `main.py`'s `lifespan` so the mistake surfaces on deploy
rather than on the first rep's first call. `StubCallProvider` is still the default and is **not** a
placeholder: click-to-dial plus manual dispositions is a complete workflow.

**Telephony is per workspace, the platform account is managed from the panel, and its minutes cost**
(`nexus/calling/connection.py`, 2026-09-23). Three reports, each true when made: Twilio was env-only
(every change an edit and a restart), every customer's calls went out on our account under one caller
ID, and `calling.minutes` was priced at 4 credits a minute and charged by nothing. Decided with the
product owner: **both, like CRM.**

- **`resolve_call_provider(ts)` is the only path** dial, disposition and status use: the
  `set_call_provider` override (the test seam) → the workspace's own connection
  (`integration_connections`, kind `telephony`) → the platform's Provider key `twilio` when a
  superadmin has set **Calling (platform account)** to Twilio → the environment exactly as before →
  click-to-dial. `provider.py` keeps an `_override` global beside the env memo for the reason
  `crm.py` does: as one variable, "is a test double installed?" was unanswerable after first use.
- **A workspace connection that cannot be read raises; it never falls back to the platform.** That
  would ring on our caller ID and bill credits after the customer chose their own account.
- **Only the platform account costs credits.** Its calls are preflighted before the dial (a 402
  before the rep's phone rings — once it rings the minutes are spent) and charged at disposition on
  Twilio's MEASURED duration, in started minutes, keyed on the call id so logging a second outcome
  cannot charge twice. The typed duration is a note; it bills only when Twilio could not be asked. A
  workspace on its own Twilio pays Twilio directly, so charging it would bill one call twice. A call
  nobody dispositions is charged by the sweep below.
- **The platform credential is `ACCOUNT_SID:AUTH_TOKEN`** in Provider keys — the pair Twilio
  authenticates with, so they are stored and rotated together. `ProviderSpec.key_format` serves that
  sentence to the Add-a-key form. Probe reads the account; verify also checks the account owns (or
  has verified) the platform caller ID, because an unowned caller ID fails with 21210 after the rep's
  phone has already rung. Neither places a call.
- **The key alone is not a switch.** `telephony_provider` (Runtime settings) must also say Twilio, so
  an operator adding a key to test it does not start live, billed calls for every workspace. Its
  `_ON_CHANGE` hook drops the env-built provider `get_call_provider` memoized, or the panel would read
  Click-to-dial while a live Twilio kept dialling. `telephony_from_number` is validated E.164 before
  it is stored.
- **The caller ID is not a secret**, so it lives in `integration_connections.config` (migration
  `0060`), never in the sealed bundle: showing it from there would mean unsealing inside the response
  builder, the one place that keeps "the secret never leaves the server" checkable. The SID comes back
  only as `AC...1234`, and a blank SID or token keeps what is saved.
- **Every platform call is charged, logged or not** (decided 2026-09-23). A live platform call is
  recorded at dial in `placed_calls` (migration `0061`). The `charge_unlogged_calls` sweep, enqueued
  every tick beside the billing sweeps, charges any still uncharged two hours on, on Twilio's
  measured duration, under the same `call:<id>` key as the disposition — whichever runs first stamps
  `charged_at`, and the other charges nothing. It asks the PLATFORM provider
  (`platform_call_provider`), never a workspace's. Live calls wait until finished; one Twilio still
  cannot describe after 7 days is given up on, nothing charged.
- **Each workspace can have its own number on the platform account** (`tenants.platform_caller_id`,
  `PUT /admin/billing/customers/{id}/caller-id`, `providers.manage`, the customer directory's Caller
  ID panel). One shared number let one heavy customer's spam flag lower answer rates for all. Checked
  against the platform account before saving; blank is the platform default.

## Provider keys and models (`nexus/providers/`) — superadmin, no redeploy

Every pooled credential — Groq, Anthropic, OpenAI-compatible, Exa, Firecrawl, Brave, Serper, Apify,
GitHub — plus the LLM **model**, managed from the panel instead of by editing `deploy/.env` and
redeploying. Gated on **`providers.manage`**, superadmin preset only: a holder can spend money
through somebody else's API key, so it is not folded into `admins.manage`. Same argument that keeps
`sources.manage` separate.

**The motivating outage was not a bad key.** On 2026-08-21 all five Groq keys authenticated and
404'd on every completion, because `llama-3.3-70b-versatile` had been withdrawn. `llm_provider="auto"`
falls back to the stub, so the stub wrote every outbound email — to real prospects — and nothing
reported a problem. Hence two things this subsystem does that a plain key CRUD would not:

- **Two test depths, and `probe_ok` is never rendered as a tick.** `probe` is the cheapest call that
  proves the credential authenticates; `verify` makes a real request of the kind the product issues
  and costs credits, so it is opt-in per key and never swept. A panel with one depth would have shown
  five healthy keys. For search providers the probe *is* a real query, so `verify` upgrades the
  result to `verified` — leaving them permanently amber would turn the badge from "real calls
  untested" into "this provider cannot be verified", a different fact wearing the same badge.
- **The model is chosen here too**, stored in `provider_settings` (migration `0045`) and resolved by
  `model_for()` on the same 30s TTL as the keys. A wrong model is exactly as fatal as a dead key.
  `GET /{provider}/models` asks the **provider** what it currently accepts — a list we maintained
  would go stale the same way the model did. An unlisted model is **accepted on write**: refusing one
  would mean a withdrawn-model outage could not be fixed from here, which is the situation the
  endpoint exists for.

Rules that are load-bearing:

- **The key is in no response model, ever** — not even for the superadmin who typed it. `key_hint`
  (last four) is all the UI gets. A panel that can display a credential leaks it through a screenshot
  or a support session.
- **`status` is written in exactly two places**, `mark_tested` and `mark_failed_by_digest`. No
  mutation function accepts it and the request bodies are `extra="forbid"`, so an admin cannot mark a
  dead key working by hand. Same ladder discipline as `nexus/sources/service.py`.
- **`key_digest` (sha256) exists because Fernet is randomised.** One key seals differently every time,
  so the ciphertext cannot carry the uniqueness constraint, and a runtime rejection — which arrives
  holding plaintext, not a row id — could not find its row.
- **Cryptographic roots are deliberately absent from the catalog**: `secret_key`,
  `network_token_enc_key`, `mfa_secret_enc_key`, `source_db_dsn_enc_key`, plus `stripe_secret_key`
  and `hubspot_access_token`. Rotating the key that seals the table from a form served by that table
  is a lockout, not a feature. Pinned by test.
- **`managed_pool()` and `key_pool()` are separate on purpose.** An explicitly-passed key must win:
  folding them together made `_refresh_keys` overwrite a caller's key, caught by
  `test_firecrawl_rotates_to_the_next_key_on_rate_limit`.
- **A pinned key is tried first, so rotation is the failure path rather than the resting state.**
  Disabling clears the pin, so the resolver never reasons about a pinned key it may not use.
  Disabling is never refused — during an incident "stop using this" must not be blocked.
- **The 30s TTL is what reaches the worker.** It is a separate process; without a TTL a new key would
  need a restart, which is the redeploy this subsystem removes. Verified live: a running worker
  picked up a key added through the API without restarting.
- **`resolver` falls back to the environment pool when no managed key exists**, so adding this
  changed nothing for a deployment that has not used it. The model endpoint falls back the same way,
  or the first thing an operator wants to look at would require adding a key first.

**OpenAI-compatible is configured entirely from the panel: endpoint, key and model** (2026-09-29).
"OpenAI-compatible" names a protocol, not a service, so a key means nothing without the endpoint it
belongs to, and the endpoint was `NEXUS_LLM_BASE_URL` only. Worse, found while adding the field:
nothing read the panel for this provider at all. `_build_llm_chain` added `OpenAICompatProvider`
only when `NEXUS_LLM_API_KEY` was set, from the env model and URL, so a key and model saved here
did nothing.

- `provider_settings.base_url` (migration `0065`, `PUT /admin/provider-keys/{provider}/base-url`),
  offered only for `catalog.BASE_URL_PROVIDERS`. Resolved by `resolver.base_url_for` on the model's
  30s TTL. Empty means `NEXUS_LLM_BASE_URL`, exactly as an empty model means the env model.
- **Validated before it is written** with the email verifier URL's guard (`_validate_service_url`):
  Test and the model list call it and report how it answered. http is allowed (self-hosted model
  servers often have no TLS); private, loopback and link-local addresses only on a local or test
  stack; cloud metadata host names never. A pasted request URL (`.../chat/completions`) is refused
  with the base URL to use, rather than silently trimmed.
- `ManagedOpenAICompatProvider` (`agents/llm.py`) re-reads endpoint, key (managed pool first, then
  env) and model before every call, and raises before sending when no key exists, so the chain moves
  on. It is in the chain whenever `llm_provider` is `auto` or `openai_compat`, like Groq, or a
  panel-only key could never be used. A 401/403 marks the key red (`record_rejection_from_response`).
- Probe, verify and the model list ask the chosen endpoint (`testing._endpoint`), never the env URL.
- **Anthropic has the same gap and is not fixed yet**: `AnthropicLLMProvider` is built only from
  `NEXUS_ANTHROPIC_API_KEY` and the env model, so an Anthropic key or model saved here is unused.

`GET /admin/billing/overview` is the platform-wide counter beside it: users, workspaces, requests
this period and all-time, credits granted vs spent. **`requests_with_a_user` is reported separately
because attribution is partial by construction** — only usage events carry a user id, and background
work has nobody to attribute it to (live: 18 total, 11 attributable). Its first version returned
`requests_total: 0` against a database holding 18 events: `billing_usage_events` is tenant-scoped, so
a cross-tenant aggregate on the RLS-bound app role returns **zero rows rather than raising**. The
documented trap, walked into anyway — it now runs on `get_platform_sessionmaker()`, pinned by a test
that writes an event for a tenant the caller is not.

**Runtime write-back marks, it does not disable.** A key rejected mid-crawl records itself against
its row (Exa, Firecrawl, Groq and Apify all call `record_rejection_from_response`), so a credential
revoked at 3am is red by morning without anyone pressing Test — which is the point of the panel.
But the resolver filters on `enabled`, not on `status`, so a red row is still in the pool:
auto-disabling on a runtime error would let one bad minute — a provider 403ing during an incident, a
billing hiccup arriving as 402 — take the last working key out of rotation with nobody watching.
Rotation already routes around a dead key inside the same request; what was missing was the
evidence, not the reaction. Apify deliberately records `_describe_error` rather than the generic
extractor, so `full-permission-actor-not-approved` reads as the console approval it is instead of
sending an operator to rotate credentials that were never wrong.

## Apify actors (`nexus/integrations/apify.py`)

The seam for lookups with no compliant public API. **Adding an actor is a line in `ACTORS`, not a new
integration.** Key rotation (`NEXUS_APIFY_API_KEYS`) is sticky and mirrors `exa_api_keys`; a 401
rotates past a revoked key without retrying it, a 429 rotates and backs off only after cycling the
pool. Unkeyed raises `ApifyNotConfigured` rather than returning `[]` — "not configured" and "this
person has no phone" must never look the same. Uses `httpx` against
`run-sync-get-dataset-items`, which is the REST call `apify_client.actor().call()` wraps; no new
dependency. Actor output is **not a contract**: `extract_phone` reads six key spellings plus nested
shapes, because reading one hard-coded key makes an upstream rename look like "no phone number".

Phone numbers are stored E.164 (`nexus/contacts/phone.py`). The region is a **cascade, never a
constant**: the person's country, then the account's, then US. An unparseable number is kept raw
rather than dropped — losing a contact's only phone number to keep a column tidy is the worse
outcome.

**Read actor keys by meaning, not by a hand-written list** (`nexus/core/keys.py`). Measured
2026-08-05: `phone_finder` returns `first_mobile_number` / `mobile_numbers`, neither of which was
in the key list, so a working actor extracted **nothing** — silently, reading as "this person has
no phone". A fixed list of spellings loses against output that is not a contract. Extraction now
sweeps any key whose *segments* name the thing wanted, and what makes that safe is that the
**value** must still pass a shape gate (`looks_like_phone`), so `phone_status: "valid"` and
`mobile_country_code: "1"` contribute nothing. Both halves are required: the sweep alone is
reckless, the gate alone missed a real number.

Match on segments, never substrings. `updates` contains **`date`**, so a substring exclusion built
to skip `lastPostDate` silently dropped one of the commonest activity-feed key names; and
`activities` singularises to `activity` only with an `-ies` rule, not by stripping an `s`. Both
were real misses, caught by tests rather than by reasoning.

**Every actor row must prove it is about the person asked for.** The actors take a *list* of
profile URLs and return a dataset, so taking row zero attaches a stranger's phone — or reads a
stranger's posts onto a call script. Rows naming a different profile are discarded and are never a
fallback; rows naming no profile are used, because a single-result actor that does not echo the
input is the common benign case.

**Actor permissions are approved per Apify account, not per key.** Several actors (`phone_finder`,
`linkedin_profile`) demand *full account access* and 403 with `full-permission-actor-not-approved`
until someone clicks approve in the console. That is not a bad key, and reporting it as one — or
worse as a rate limit, which the code used to do — sends an operator to rotate credentials that
were never wrong. `_describe_error` carries Apify's own `error.type` into the log and the raised
exception.

Registered actors and their state:

| Logical name | Actor | Consumer |
|---|---|---|
| `phone_finder` | `code_crafter/mobile-finder` | `people/enrich.py` — live, verified end-to-end |
| `linkedin_profile` | `dev_fusion/Linkedin-Profile-Scraper` | `personalization/apify_provider.py` |
| `linkedin_posts` | `A3cAPGpwBEG8RJwse` | `personalization/apify_provider.py` |
| `b2b_enrichment` | `teodor_banea/b2b-lead-enrichment-free` | `enrichment/b2b_actor.py`, at most once per 30 days per account |
| `linkedin_company_search` | `harvestapi/linkedin-company-search` | `prospecting/linkedin.py` — run once, 2026-09-24 |
| `linkedin_company` | `harvestapi/linkedin-company` | `prospecting/linkedin.py` — run once, 2026-09-24 |
| `linkedin_company_employees` | `harvestapi/linkedin-company-employees` | `prospecting/linkedin.py` — run once, 2026-09-24 |

**Every registered actor has a consumer, and that is now a test**
(`test_every_registered_actor_has_a_real_caller`) rather than a habit. It resolves the argument
handed to `run_actor` through the AST, because both grep spellings are wrong in opposite
directions: `run_actor("<name>"` misses `linkedin_profile` (passed via the module constant
`apify_provider.ACTOR`), and the bare quoted name falsely matches `company_search`, which is also
an unrelated module, a registry method, and a cache-key literal.

`crunchbase_org` and `company_search` were registered and called by nothing for months, and were
**removed on 2026-08-20 rather than wired**. Registered-but-unused is the worst available state: it
does nothing while still printing as a capability in `scripts/verify_apify_actors.py`. Why removal
won over wiring, since "add Crunchbase enrichment" recurs as an idea:

- `crunchbase_org` keys on a **Crunchbase organisation URL**, but shared-company identity is the
  normalised domain and nothing else, so there is no domain → Crunchbase-URL step to feed it — and
  `crunchbase.com` is in `_NON_COMPANY_HOSTS` because discovery deliberately filters directory
  aggregators out. Its output would land in `companies`, which carries no `tenant_id`: a wrong
  firmographic there is wrong for *every* tenant.
- `company_search` had 42 total runs across 2 users. An abandoned actor is a dependency that
  disappears without notice, and this one would have fed net-new Account creation.
- Neither could be run even once (see the operator blocker below), so wiring meant writing a parser
  against an output shape nobody has observed — exactly how `phone_finder` shipped reading key
  spellings the actor never emitted.

Re-adding either is one line plus a consumer. Doing it on a guess is the part that costs.

**Operator blocker, 2026-08-20:** both remaining actors 403 `full-permission-actor-not-approved` on
*both* configured Apify accounts, and the key that previously worked now 401s. The integration
therefore delivers nothing in production right now. Key rotation cannot fix either half — approval
is per account and needs a click in the console; a revoked key needs replacing. Until both are
cleared, `--run` cannot pass and no new actor can be validated against real output.

## Drafted copy (`nexus/agents/copy.py`)

**Everything the agents generate is sent to a real buyer**, including the offline stub's output:
`llm_provider="auto"` builds `FallbackLLMProvider([Groq, Stub])`, so the stub is what a deployment
with a dead key sends. It is not a placeholder.

**One variable must not carry two grammatical forms.** `pains_solved` holds problem NOUNS, the
template read `use {vp} to {pain}` — which needs a verb — and the no-value-props fallback was
`"hit pipeline goals"`, a verb phrase. So the sentence read correctly for workspaces that had
configured nothing and broke for every workspace that had filled the field in:

> Teams like Marketjoy use Accurate Lead Generation to Stale lists, Duplicate records, No signal on
> in-market accounts, Wasted time chasing wrong leads.

`format_pains` guarantees the noun form and the connective takes nouns (`to get ahead of ...`);
fixing only one half would leave the other free to break again. It caps at two — four problems in
one sentence reads as a list being recited — and `first_pain` exists because a discovery question
naming four problems is not answerable. `_downcase_lead` tests the **second** character, so `SOC2`
and `CRM` keep their capitals; a blanket `.lower()` mangles every acronym a customer typed.

**Prompt rules live in one place and are stated as constraints, not style notes.** Distilled from
four public GTM prompt libraries (Prospeda, gtm-skills, gtmagents, sidchaudhary) — their text is not
copied; what transfers is that a hard word cap, a banned-phrase list, observation-before-ask, and
no-invented-facts each map to a failure this product can have. The highest-value rule is in none of
them: **`OUTPUT_CONTRACT` asks for the `Subject:` line that `_split_subject` has always parsed.**
The prompt never requested it, so a model that opened with the body produced a blank subject and
nothing reported a problem.

The system grounding (`RelevanceContext.to_prompt`) names the specific fabrications — a customer
name, a metric, a percentage, a case study, an integration — because "never invent value props"
alone is not enforceable, and this product *has* the real facts, so omission is always available.

**The B2B cold email playbook is the shape, by touch** (supplied by the product owner, 2026-10-01;
`tests/test_email_playbook.py`). `copy.email_rules(touch)` builds the rules for one email from the
engagement kind; anything else (composer, plays, orchestrator) is a first email:

- **Subject** 2-4 lowercase words like an internal note (`SUBJECT_RULE`), not on threaded touches,
  whose subject is the thread's "Re:". **Body** in short blank-line paragraphs (greeting, why now,
  the problem as an observation, proof only if the context names a real result, the ask on its own
  line, "Best," then the first name), plain text, no em dashes, one link at most. **One signal**,
  used rather than recited, no flattery or fake familiarity (`SIGNAL_RULE`).
- **The ask is one interest question in any email nobody has answered** (`EMAIL_CTA_RULE`: "Worth
  a look?"), never a meeting, duration or day. This reverses the 2026-09 rule that asked for "15
  minutes on Thursday", on the playbook's evidence that a stranger answers a one-word question far
  more often than a calendar ask. A reply to someone who wrote back (`response`) offers **two
  specific times** in their timezone instead (`REPLY_CTA_RULE`). The call script keeps its meeting
  ask (`CALL_CTA_RULE`): a call is already a conversation.
- **Each follow-up adds something new** (`drafting/context.followup_instruction`): email 2 a new
  angle or a real peer result, 3 a pointed question about what the status quo costs, 4 two or three
  lines, and the last of a sequence of three or more (or anything past the fourth) closes the loop
  politely. A template's own step angle is listed after it and wins.
- **`check_draft(touch=...)` enforces the parts a reader can point at**: a meeting request in the
  ask SENTENCE of an unanswered email (a duration, a weekday, call/meeting/demo; a sentence, so "cut
  it to 20 minutes" in the proof is not one), a subject over 6 words on a non-threaded touch, over
  40 words with no blank line, and the empty follow-up phrases ("just following up/bumping/checking
  in"). Callers passing no touch are checked as before. A failed check costs one regeneration, and
  on an auto follow-up it holds the email for review (D17), so a new check must not false-positive.
- The offline stub writes the same shape, and passes its own checks.
- **Two workspace choices override the playbook, and both the prompt and the check read them**
  (`email_style.draft_options`, 2026-10-01). Settings → Email style's target length is the word cap
  (`email_rules(word_cap=)`, `check_draft(word_cap=)`); it used to be "aim for about N" beside a
  fixed "under 90" rule and a 105-word check, so a 150-word setting was trimmed back. And
  **"First email asks for"** (`style.first_ask`: `interest` default, `meeting` opt-in) swaps
  `EMAIL_CTA_RULE` for `MEETING_CTA_RULE` and stops the meeting-ask check; replies still offer two
  times. The engagement drafter's final check reads the same options, or it would hold a draft the
  agent was told to write. `PUT /workspace/email/style` keeps a saved `first_ask` when the body
  omits it, so an older client cannot reset it.
- **Both drafting paths name the sender** through `copy.sender_block`: the engagement drafter from
  the mailbox, and `POST /agents/messaging/run` (the contact composer) from the signed-in rep's
  connected mailbox, else their own name. The composer path was missed by the 2026-09-30 fix and a
  live draft still signed "Best, Alex". Measured on the local deploy, 2026-10-01, same contact:
  before, a title-case subject, one block with markdown line breaks, "a 15-minute call on Friday",
  `U+2011`/`U+202F`; after, "hg capital buy", paragraphs, "Open to seeing how?", the rep's name, no
  look-alike characters, `check_draft` clean.

## Drafted email: structure, signature, and the mailbox that sends it

Four reports on 2026-09-16, one chain: the draft had no salutation, no signature, did not read like
an SDR wrote it, and the mailbox said "SMTP not connected" after the rep had connected one.

**The prompt never asked for a greeting.** `OUTPUT_CONTRACT` named the subject and the body, and
"open with a specific observation about THEM" was followed literally, so emails began mid-thought.
`STRUCTURE_RULE` (`agents/copy.py`) now names greeting → observation → value line → ask → sign-off,
and says the sign-off is the FIRST NAME ONLY: the block under it is appended at send, and a model
inventing a title and phone number would put fiction beneath a real person's name.

**A rule in a prompt is a request; a check is a guarantee.** `agents/email_quality.check_draft`
inspects the draft for the things a reader notices — missing greeting, the WRONG first name, no ask,
no sign-off, over the word cap, a banned phrase — and `MessagingAgent` regenerates ONCE with the
complaints attached (`retry_instruction`). The retry is kept only if it breaks fewer rules than the
first attempt, so a worse second draft is discarded rather than shown. Every rule is something a
person can point at in the text; a quality score would reject good drafts for reasons nobody can act
on. One retry, never more: two more completions per draft is a bill, not a fix.

**The workspace's own sample emails are style, never facts** (`agents/email_style.py`). Asked for by
the product owner: reps paste emails they consider good and the agent adapts. A real SDR email names
a real customer and a real number, so copying one into a draft for a different buyer is a fabricated
case study reaching a prospect — the failure `EMAIL_RULES` exists to prevent, arriving through a door
it was not watching. Samples therefore travel with the instruction that they are a shape to follow,
the fact rules still sit *after* them in the prompt, and `MAX_SAMPLES` is 3 because every sample is
tokens on every draft.

**Signatures belong to the mailbox** (`outreach/signature.py`), with a workspace default for the rep
who has not written one. The mailbox is what identifies the sender — a reply goes back to whoever
sent it — so the name under the message has to be that person's. `append_signature` is idempotent
(the composer shows it and the send path adds it) and leaves a rep who typed their own sign-off
alone. Plain text for now, decided with the product owner: `_build_message` writes a text part and
adds HTML only when a caller passes one, so markup would reach the buyer as literal tags. Style,
samples and both signatures live in `Tenant.email_settings` beside the mailboxes — workspace
preference, not schema, so no migration and nothing new for `apply_rls.py` to enrol.

**An engagement email is plain text AND HTML, sent with CRLF, signed once, by the real sender**
(`engagement/sending/mime.py`, 2026-09-30). A real Outlook delivery read "Scaling pro=uct
development ... resource co=straints ... a 15-minute cal= Thursday", signed "Best, Alex" above the
rep's "Kind Regards, Amit Singh", with a raw unsubscribe URL (also broken by `=`) underneath:

- **CRLF on the wire** (`to_bytes` = `policy.SMTP`). The body is quoted-printable, whose soft break
  is `=` + CRLF; serialised with bare LF, Exchange read `=` + LF + the next character as a bad escape,
  kept the `=` and dropped the letter, once per ~76 characters. Gmail tolerated it, Graph's MIME
  import did not. The SMTP path's IMAP APPEND of drafts had the same bug (`smtplib` itself converts).
- **multipart/alternative, text first.** The HTML is the same words as paragraphs, no template
  (designed cold email is filtered as marketing), with the opt-out line in 11px grey and
  "unsubscribe" as its only link; the text part keeps the full URL. This supersedes "plain text for
  now" for engagement sends; the SMTP composer path still adds HTML only when a caller passes one.
- **One sign-off, the real name.** The structure rule asked for "your first name" and nothing said
  what it was, so the model invented one. `drafting/context._sender_block` now names the sender (the
  mailbox's display name, else its owner), and `compose_message_text` lets a signature REPLACE the
  model's closing (keeping its "Best," only when the signature has no closing of its own); with no
  signature the closing is signed with the sender's first name.
- **`agents/copy.tidy_text`**, applied where every draft is parsed (`messaging._split_subject`) and
  again at send: non-breaking hyphens and spaces become plain ones, zero-width marks go. Curly quotes
  and dashes stay.

**`send_email` takes A MAILBOX, not a settings blob wrapping one.** `resolve_smtp` reads
`provider`, `host`, `username` and `password` off the TOP LEVEL of whatever it is given. The send
path passed `{"accounts": [mailbox]}`, where all four are a level down, so the resolved host was
empty and every real send answered "smtp not configured" before opening a socket — while the
Send-test button, which passes the mailbox dict, worked. Reported 2026-09-16 as "send test
successful, sending to a contact says mailbox not configured"; the shape mismatch is invisible
because `resolve_smtp` returns defaults rather than raising. Every other test of that path replaced
`send_email` itself, so nothing saw it: `test_a_real_send_reaches_smtp_with_the_mailboxs_own_credentials`
now stops at `_send_blocking`, the last function before the socket, and asserts on the config that
would have been dialled.

**Send it, or keep it: `draft_for_contact` is the other half of the composer** (`POST /contacts/
{id}/save-draft`). `save_to_drafts` (IMAP APPEND) had existed since the sender did and was reachable
only from the orchestrator's tool, so a rep reading a draft had one button that did anything with it.
Same mailbox, same signature, two deliberate differences: **it is not metered** (`outreach.email_send`
prices a message that left the building, and charging for a draft bills a customer for pressing save)
and **a doubtful address does not block it** (nothing leaves, and a rep correcting the address by hand
needs the draft to exist first).

**A custom SMTP mailbox could not be configured at all.** `resolve_smtp` has always merged overrides
over the provider preset, but the modal posted a provider, a username and a password and nothing else
— and `provider: "smtp"` has **no preset**, so it resolved to an empty host and answered "smtp not
configured" forever. The form now asks for SMTP host/port, security, IMAP host/port and the drafts
folder; they are folded away for Gmail/Outlook/Microsoft 365 and open by default for Custom SMTP,
where they are required rather than advanced. Three rules hold it together:

- **Blank stays blank.** `resolve_smtp` reads a preset only when the stored value is empty, so
  writing the resolved host back would pin today's Gmail preset onto the mailbox for good. What is in
  force is reported separately as `server_summary`, computed from the resolved config — one field
  rather than four `effective_*` ones, and computed on the server because a frontend copy of
  `PROVIDER_PRESETS` would be a second source of truth for what the mailbox does.
- **Implicit TLS is its own setting** (`use_ssl`), not `port == 465`. STARTTLS and implicit TLS are
  different protocols and a self-hosted server can offer either on any port. It DEFAULTS to
  `port == 465`, which is the rule this module applied when the port was all it could read, so every
  mailbox stored before the field existed behaves exactly as it did.
- **The form refuses to save a custom mailbox with no host**, because the failure otherwise arrives
  later, from a different screen, as "SMTP is not fully configured".

The same edit found that the modal collected a `signature` and never posted it, while the field
defaults to `""` on the server — so every mailbox edit silently deleted the rep's own sign-off block.

**"SMTP not connected" was four different states wearing one sentence.** `resolve_rep_mailbox` needs
a mailbox that is YOURS, enabled and credentialled; the old message said "you have not connected a
sending mailbox" for all of them, which is false for three and sends the rep to add a second mailbox
nobody will use. It now names the actual state — switched off, missing its app password, owned by
nobody (claim it), or owned by a colleague (add your own) — and
`POST /workspace/email/accounts/{id}/verify` signs in to the SMTP server **without sending
anything**, storing `verified_at` or the server's own refusal in `last_error`. The existing test
SENDS an email, which needs a recipient and an inbox; "can this mailbox sign in?" is the question the
rep is actually asking.

## Importing a list (`nexus/imports/csv_ingest.py`)

Creates accounts and contacts from a CSV, mapped column by column. Identity is the normalised
domain (accounts) and the normalised email (contacts), for the reason the shared stores give.

**A file the operator can open is not a file the parser could read**, and every failure in this
subsystem has looked identical from the screen: *nothing imported, no reason*. Three were reported
together on 2026-09-16 as "uploads in accounts are failing":

- **The delimiter is the locale's list separator, not a comma.** Excel writes `;` on a German,
  French, Dutch, Spanish or Indian machine, and tabs for "Text (tab delimited)". Read as commas that
  is ONE column whose header is the whole line, so every row is skipped for having no name and no
  website. `sniff_delimiter` reads the HEADER line — the one line guaranteed present, single-record
  and free of prose — counting only separators outside quotes, with ties going to the comma so an
  ordinary CSV is never re-interpreted.
- **The preview must decode the file the way the server does.** `File.text()` is always UTF-8 and
  Excel on Windows writes cp1252, so a `Société` header reached the mapping as `Soci<?>t<?>` while
  the server read it correctly. The mapping is keyed BY HEADER TEXT, so the column the operator
  mapped to Company name was, server-side, not mapped at all. `readCsvText` mirrors `_decode`'s
  ladder, using U+FFFD as the tell.
- **One row the database refuses used to lose the whole file.** The write was flushed straight onto
  the request's transaction, so a value longer than its column or a domain already held by another
  account raised out of the loop, rolled everything back and returned a 500 for a file that was
  4,998 good rows and two bad ones. Each row now has its own SAVEPOINT and a bad one is reported with
  the database's own first line (only the first: SQLAlchemy appends the statement and its
  parameters, which would put the row's data into a response and a log).

`employee_count` is a Postgres `integer` and SQLite has no such limit, so a revenue figure typed into
that column passes every test here and raises `NumericValueOutOfRange` in production. Out-of-range is
treated as unparseable, which is what it is.

**`SKIP_TARGET` ("don't import this column") exists because the default is to KEEP.** An unmapped
column is stored under its own header — right for the territory/tier/owner columns an ops list is
built around, and it left no way to exclude anything: an internal note, a stale owner, a field
somebody should not have exported. It is mapped explicitly rather than by omission, so "I chose to
drop this" and "I forgot about this" stay different statements, and the server sends the sentinel
back in `/imports/fields` so the picker's spelling and the server's cannot drift.

## Feed text (`nexus/ingestion/sources.py`)

**RSS is double-encoded far more often than not**: the publisher HTML-escapes the content and the
XML layer escapes it again, so `&amp;#8211;` survives XML parsing as the literal text `&#8211;`.
Measured on live data: **73 of 139 stored RSS signals carried raw entity codes and 74 carried HTML
tags** — `&#8217;s` and WordPress's `[&#8230;]` in front of reps on the Signals list, the account
Signals tab and the dashboard feed.

`clean_feed_text` runs unescape → strip tags → unescape. The order matters in the opposite
direction to `webwatch.normalise`: unescaping first turns `&lt;p&gt;` into a real tag so it can be
stripped, and the second pass catches entities that were hidden inside the markup layer. Two passes,
not a loop to a fixed point — an unbounded loop on third-party text is how a display bug becomes a
hang. It preserves case (unlike `normalise`, which lowercases because it feeds a hash) and leaves
URLs untouched. `scripts/repair_feed_text.py` fixes rows stored before it existed; it is idempotent.

## Signal dates and the day window (`nexus/core/dates.py`, `nexus/ingestion/window.py`)

**The day filter worked, on the wrong date.** Reported 2026-09-23 as "signal filtering by number of
days not working". No source ever set `occurred_at`, so `RawSignal`'s default stamped every signal
at collection: measured on the local database, 1,099 of 1,112 dated within ten minutes of being
collected and none older than the day collection began. Every window showed the same list. RSS
`pubDate`, SEC `filed_at` and Hacker News `created_at` were read and discarded.

- **Each source passes the date it has**, and `dated` says which kind it is: `event` (the source's
  own date, or an observation of the present — open roles, a changed page, active repos, where
  collection time IS the date) or `found` (only when we collected it). Decided with the product
  owner: an undated signal keeps its collection date and is labelled, never hidden. Hiding them was
  the alternative, and about 90% of search-found signals carry no date (626 of 692 locally).
- **One reader** (`parse_when`, standard library only) for RSS, ISO, `YYYY-MM-DD`, "Mar 4, 2025"
  and "3 days ago"; `date_from_url` for `/2025/03/04/`. Anything unreadable is `None`, never a
  guess; a future date clamps to now; before 1995 is an unset epoch. Search hits carry the
  provider's raw string (`SearchHit.published_at`): Exa, Brave, Serper and Firecrawl give one,
  DuckDuckGo HTML — the web-news browser — never does, so web news dates only from its URL.
- **Scoring was already written to decay a signal to zero over 90 days** and the bug defeated it;
  so was `_age_phrase` in the drafting prompt. Both now work as written, which means intent scores
  FALL for accounts whose signals are old news found recently. That is the correction, not a
  regression. `signal_age` tells the model "date unknown" for a `found` signal instead of "in the
  last few days".
- **The window is a superadmin policy** (Runtime settings, Signals): `signal_window_user_choice` (on:
  each user picks from the top bar, starting at the default) and `signal_window_default` (Weekly …
  Yearly, All time; default All time, a strict no-op). Off is ENFORCED by the server on `/signals`,
  `/inbox` and `/alerts` whatever the client sends; the top bar shows a locked label instead of a
  picker. `GET /signals/window` serves the options from the same list the panel offers.
- **Where it applies, decided:** Signals, Dashboard, Account page, Inbox and Alerts (a task or alert
  no signal raised always shows), and AI drafts, research and the call brief — which always use the
  platform DEFAULT, having no top bar. **Not scoring or plays.** The agents filter where they READ
  signals, not where the context loads them, because that same list feeds scoring.
- `scripts/repair_signal_dates.py` re-dates stored rows from a URL date, an SEC key, or (`--feeds`,
  fetches) the post date in the company feed. Dry run by default.
- **A dated event is de-duplicated by its own month** (`event_bucket`, decided 2026-09-23).
  Bucketing on the month we FOUND it let an old article found this month take this month's `funding`
  slot, and a real round found later in the month was dropped with no trace. Undated items keep the
  found month, the only date they have; job boards stay on the current month, being observations of
  now. One-time effect on deploy: an event stored under its old found-month key can be stored once
  more under its event month.
- **An old event goes on the timeline, not into anyone's attention** (`asks_for_attention`,
  `signal_alert_max_age_days`, default 30). An event older than that by its own date raises no
  alert, no channel ping and no Inbox task; undated signals always may, and Plays are untouched. One
  check read by both `raise_alerts_for` and the pipeline's Inbox loop, so they cannot disagree.
- **Latest event on top, ties to the most recently found** (decided 2026-09-23). Every newest-first
  read orders by `occurred_at` then `created_at`. A 2023 round found this morning sits among 2023
  items: it is history, and it no longer alerts.
- `signal_age` says "date unknown" for a recent undated find, and "date unknown, found over six
  months ago" for an old one: the day we found it is the LATEST it can have happened, so an old
  find is known to be old. Saying only "date unknown" threw that away; the full suite caught it.

## Person-level personalization (`nexus/personalization/`)

A contact's headline, recent posts and interests, fetched once and folded into both the email
(`agents/messaging.py`) and the call script (`agents/call_script.py`) via `brief.to_prompt`. The
whole chain existed from the start except the provider: `build_personalization_provider` carried a
comment describing the Apify branch and returned the stub for every input, so
`NEXUS_PERSONALIZATION_PROVIDER=apify` silently did nothing. An unknown provider name still falls
back to the stub — a typo must cost personalization, not the ability to send email — but it now
logs, because "configured and doing nothing" is the state this codebase keeps having to diagnose.

- **A post has to be worth saying out loud.** `_is_substantive` rejects reactions, reshare stubs
  and single-emoji replies. An SDR opening "I saw your post" and then referencing a thumbs-up reads
  as automation, which is worse than referencing nothing — the failure is louder here than
  elsewhere because the text is *spoken on a call*.
- Long posts are **trimmed, not dropped**: the opening sentences carry the subject, and an essay
  would dominate the prompt and the token bill.
- Reshares are de-duplicated on a text prefix, or the same story appears three times.

## Alerts (`nexus/alerts/`) — M21

**`signal.created` had no subscriber.** It was published on every ingested signal and the only
listener on the bus handled `account.scored`, so alerts were created in exactly three places, none
of them from an incoming signal. Everything the collection pipeline gathered landed in a table
nobody was notified about — a rep learned about a customer's Series F by scrolling the account page.
`signal_alerts.register_signal_alert_subscriber()` is that wire, registered in both `main.py` and
`workers/worker.py`.

- **Not every signal is an alert.** `signal_alert_floor` (0.5) keeps the classifier's 0.4 weak-mention
  tier off the inbox. An alert costs attention, and attention spent on a press mention is attention
  not spent on a funding round.
- **Every alert carries the next action**, derived deterministically from the signal kind in
  `rules.py` — no LLM on the ingestion path, nothing invented.
- **An unknown signal kind alerts quietly rather than vanishing** (`news`/`info`), the same bias that
  makes an unknown billing capability resolve to allow.
- **Alert dedupe is separate from signal dedupe.** Signals dedupe on the *event*; alerts dedupe on
  *attention*, per category per account per day. Two different job postings are two real signals and
  one notification.
- **An alerting failure never rolls back ingestion.** Losing the signal to save the notification
  would be exactly backwards.
- `ALERT_CATEGORIES` is derived from `_RULES`, so a category a user can subscribe to but nothing ever
  emits cannot exist.

`notification_preferences` (migration `0032`) is per user, per category, per channel. **The absence
of a row means "no preference expressed"** and the tenant-level configuration continues to apply —
adding the table mutes nobody. Quiet hours are stored as minutes from local midnight so the
overnight wrap (22:00 → 07:00) is arithmetic rather than a special case; a naive `start <= now < end`
silently disables quiet hours for exactly the people who set them overnight. Critical alerts override
quiet hours by default, and the user owns that trade.

## Alert routing and account ownership (migration `0056`)

An alert did not know whose account it was on, and a shared Slack received nothing unless somebody
routed their own alerts there. Three additive pieces fix that:

- **`accounts.owner_user_id`** is whoever adds the account: create, lookalike add, a similar-person
  add that creates the account, and both CSV imports. NULL is unowned, and every account from before
  this is unowned, because nothing can infer an owner for an old row. A rep may claim an unowned
  account or release their own (`PUT /accounts/{id}/owner`). Anything else needs `assign_accounts`
  (manager+), and the target must be a member of the workspace (422). `transfer_ownership` in
  `accounts/merge.py` moves owned accounts too, and a merge fills a blank owner from the other row.
- **`notification_preferences.scope`** is `all` or `mine`. The server default is `all`, because that
  is what every row saved before it meant. `PUT /notifications` treats an omitted scope as "keep what
  is saved", so a client that predates the field cannot change a route's reach by editing its timing.
  `mine` means the alert's account is owned by that member, and an unowned account is nobody's.
- **`alert_channel_rules`** are a manager's rule that a shared channel receives a category for the
  whole team (`PUT /alert-connections/{kind}/rules`; the list replaces what was saved). Nobody's
  personal `off` or quiet hours mute a rule, because it is a decision about a shared channel. A rule is
  also the only way an alert on an unowned account reaches a channel. `fanout.channels_for` unions
  rules and personal routes and posts **once per channel**.

Connecting a channel moved from `manage_workspace` to `manage_alert_channels` (manager+). A team lead
setting up the Slack their team works from is normal; a rep replacing the one workspace Slack would
redirect everybody's alerts, which is why it stopped at manager.

**Alert settings is its own page, `/alerts/settings`, gated like Alerts (`module.signals`) rather
than like Settings.** Settings is admin-only, so the rep-level routes API had a screen no rep or
manager could open. Settings keeps a card pointing to the new page, and the team-channel editor on it
renders for manager+ only.

**The frontend reads the signed-in user's id from the token's `sub` claim**
(`app/useCurrentUserId.ts`). The login response carries no user id, and a restored session has only
the token. That id drives "You", the "My accounts" filter and Claim/Release, and the server re-checks
every one of them. `/workspace/members/directory` (manager+) is the assignee list, separate from the
admin-only members screen. `PageHeader` renders a non-string description in a `<div>`, because the
owner control is a `<select>` and a `<p>` may only hold phrasing content.

Pinned by `tests/test_account_owner.py`, `test_alert_routing_api.py`,
`test_alert_routing_delivery.py` and `test_alert_routing_ui.py`.

## SDR engagement engine (`nexus/engagement/`)

Replaced Campaigns and Cadences at the cutover (phase 15): `nexus/campaigns/`, `nexus/cadences/`,
their routers, workers and pages are gone; their tables stay as read-only history. Spec:
`docs/superpowers/specs/2026-09-17-sdr-engagement-design.md`; plans:
`docs/superpowers/plans/2026-09-17-sdr-engagement/`; runbook: `docs/engagement/cutover-runbook.md`.
`engagement_campaigns_enabled` is **on by default**, and off is the emergency stop.

- **The cutover script is the real run, rolled back, when dry** (`scripts/migrate_engagement.py`,
  `nexus/engagement/cutover/migrate.py`): both go through one `_apply` in a SAVEPOINT, so the report
  cannot drift from the run. Idempotent on the legacy ids 0057 added; a re-run after an SDR connects
  a mailbox attaches it (`sequences.service.attach_mailbox`, which also backs the campaign page's
  "Send from this mailbox") and imports the old emails' thread history. No ledger events: statuses
  are assigned directly, because cutover-time "enrollment.started" events describe the migration.
  Finished old campaigns are listed read-only (`GET /engagement/legacy-campaigns`).
- **Contact sourcing moved to `nexus/contacts/sourcing.py`**; the orchestrator goal `setup_cadence`
  keeps its stored name and now creates a sequence template.

- **Rules live in pure modules, and nothing re-derives them.** `subjects.reply_subject` is the only
  way to build a follow-up subject (exactly one "Re:", D16). `dates.resolve_date` is the only way a
  reply's "try me in June" becomes a date, and an ambiguous phrase resolves to `None` so a person
  decides (D8). `timekeeping` owns business days (Mon–Fri, no holiday table) and the recipient's
  timezone (contact `custom_fields["timezone"]` → account country/region → SDR mailbox).
- **Double sends are prevented by the schema, not by care.** An outbound message row is written
  `queued` before the provider call; a partial unique index on `(enrollment_id, step_index)` where
  `direction = 'out'` and one on `(tenant_id, idempotency_key)` make a second send of the same step,
  re-engagement or response an `IntegrityError`.
- **No mocks (D21).** New tests use real inputs, real SQLite rows, or the live suites in
  `tests_live/engagement/`. `tzdata` is a dependency because `zoneinfo` has no zone database on
  Windows.
- **The training & insights ledger** (`nexus/engagement/ledger/`): `emit()` writes to
  `ledger_outbox` in the caller's transaction, only for workspaces whose newest `training_consents`
  row is `on`, and never raises. `ship_ledger` seals events into the archive store;
  `build_ledger_datasets` derives the training and insights stores from it, so both are
  rebuildable. The Gmail push URL carries no token of ours — `tests/test_credential_leaks.py`
  refuses a credential in a query string, and the Pub/Sub OIDC signature is the trust.
- **The Ledger tab sets the stores up, and an erasure removes a person for good** (2026-10-14,
  `ledger/erasures.py`, migration `0071`, `tests/test_ledger_erasure.py` and the Postgres half in
  `tests_integration/test_ledger_stores_pg.py`). Reported as "everything is not configured and
  there is no option to configure it": the connection strings and the pseudonymisation secret were
  Provider keys and the tab never said so. `PUT /admin/ledger/setup` (`providers.manage`) stores
  them through the provider-key service, sealed and pinned like Mailbox apps' secrets, after the
  SSRF guard `stores.connect` uses; the secret is GENERATED once and replacing it is a 409, because
  the archive is sealed with it and every person key is an HMAC under it. Erasure had four holes,
  all closed:
  - **The outbox was never cleared.** It holds raw email text until shipped and seven days after,
    so a waiting event shipped the person straight back. `erase_person` now sweeps it, resolving
    each row as the shipper does, and counts again afterwards.
  - **Only the contact could be found.** Archive `person_keys` are now everyone the event names
    (`erasures.everyone`: contact, sender, every address in its text, searched string by string),
    and the derived stores are cleared by the deleted archive rows' event ids too, kept across
    retries in the report: a training example keyed to the contact still carried a Cc'd colleague.
  - **Nothing remembered it.** A `ledger_erasures` row (HMAC keys only, never the address) makes the
    shipper DELETE any event naming the person instead of archiving it, and the builder skip one, so
    the erasure holds for later events and rebuilds. Its key is an HMAC under `secret_key`, not the
    ledger secret, so erasure works before the ledger is configured, which is when the outbox fills.
  - **Nobody saw the result.** The report lands on the row; the tab lists recent erasures as
    waiting, done (nothing left) or incomplete. A store that fails leaves it open and the job raises,
    so it is retried and finally dead-lettered. The shared people store's erasure queues the same
    job with the keys and lets the job write the row, because it runs inside its own transaction.
- **The screens gate on the engine, not on a plan.** `GET /engagement/settings/status` is the one
  engagement route that answers while the engine is off; `EngagementProvider` reads it once at the
  shell. Nav items carry `engine: "on"` and leave only while the engine is CONFIRMED off; unknown or
  unreadable reads as on (fails open, like entitlements) now that there is no other campaign screen,
  and the server's 404 is the boundary. `RequireEngine` says "switched off" in place rather than
  redirecting. `/campaigns` and `/cadences` forward to the new pages for old bookmarks.
- **Insights (`nexus/engagement/insights/`) apply D26 on the server; the screens only render.** A
  pattern needs history from three or more workspaces, below that only the last reply's speed band,
  and nothing for a workspace that has not opted in. The client selects named columns so the
  profiles' `workspace_keys` never enter the process, and `test_engagement_screens_ui.py` refuses
  `workspace_count` anywhere in the engagement screens. Likelihood is the viewer's own fit and
  signals; with neither it is `unknown` and no badge shows, because a neutral fit alone read as
  "less likely to reply" for every unscored prospect. Today ranks within a kind by likelihood BAND,
  then age.
- **The §19 enhancements (`nexus/engagement/enhancements/`) never act on a guess or send on their
  own.** A referral's person comes from the Cc line or an address on the account's own domain, a
  name is matched only among that account's contacts, and the intro waits in the review queue. The
  CRM activity log writes only against an account the account sync already put in that CRM
  (`crm_id` + `crm_source`), once per row (`crm_logged_at`, migration `0058`), behind "Push to CRM".
  Signal re-engagement is a suggestion: never someone who declined, unsubscribed, was emailed in the
  last 14 days, or is live in another campaign, and the send is keyed on the enrollment and signal.
- **A connected Gmail/Outlook mailbox is what the contact composer uses**, ahead of any SMTP
  app-password mailbox (`outreach/send.py`). Send goes through `engagement.sending.send` (do-not-
  contact check, exactly-once row, one `outreach.email_send` charge, a thread so replies reach the
  desk); Save to Drafts goes through `engagement.sending.drafts.save_draft`, unmetered, which the
  reply desk uses too. SMTP remains the fallback and behaves as before.

**When campaigns stop, Platform health says which stage** (2026-10-02, `engagement/health.py`, the
`campaigns` row). Reported from staging as "no drafts, first emails never send, follow-ups never
send, replies not picked up". The whole engine was then run once end to end on the production image
against a restored copy of the local Postgres, API phase as `nexus_app` under RLS and worker phase
as the owner, the role split staging uses. Every stage worked: drafts, approval, launch, first
sends, follow-ups in-thread, a reply read, classified and shown on the desk. So the code is not
what stops; the deployment's state is, and nothing reported it. Everything after approval is worker
work, so the row reads the evidence the worker leaves rather than asking it:
- **a step due more than 30 minutes ago in a running campaign was not sent.** Everything that
  legitimately waits moves the due time or the status, so only a job that is not running, or one
  that raises on that enrollment every tick, leaves it in the past. A "later" date or an
  out-of-office return that has passed counts too.
- **a connected mailbox not read for 30 minutes is not having its replies picked up.**
- the switch off while campaigns run, mailboxes needing reconnecting, a mailbox's own fault, and
  failed sends in the last week. Platform sessionmaker, or it counts zero of everything.

**A mailbox that cannot be read now says so.** `sync_mailbox` caught an expired grant and a limit
and let every other provider refusal escape, and the sweep's tenant session rolled back with it,
discarding what the failure had written: `fresh_access_token`'s `error` status for a misconfigured
mailbox app (a wrong client secret), and nothing at all for a read refused outright (a Google
project without the Gmail API enabled answers 403). The mailbox read "Connected" while no reply was
ever picked up. Renewal of the push subscription happened to catch the first case, but only when
the subscription was missing or expiring. Now the refusal is `last_error` ("Replies could not be
read: ..."), shown on the Mailboxes page, and a successful read clears it. A draft that cannot be
written also names its reason on the Review screen instead of only "try again".

**A suggested reply reads the conversation the reply came in** (2026-10-02, asked "do automated
replies have complete context?"). `build_context` took the conversation from the ENROLLMENT's
current thread; once that had moved on (a reply to an older email, a later thread), the pack said
"Nothing has been sent or received yet: this is the first email" and the model answered a buyer's
reply with a cold opener. A `thread` passed by the caller now wins, and `drafter.draft` passes the
one it has. Inbound messages are shown through `parse.strip_quoted`: they are stored whole, quoting
our emails back underneath, which are already in the block, and a long quote was what survived the
1,500-character clip instead of the buyer's words. The answer's shape (two specific times, greeting,
paragraphs, sign-off) is the playbook's, pinned in `test_email_playbook.py`.

**Do-not-contact takes a whole domain, and an uploaded list** (2026-09-30,
`engagement/suppression/service.py`). A domain block is stored in the same `email` column as
`@acme.io`, so it shares the one-active-block index and needed no migration. Every READ
(`active_block`, `active_reasons`, and through them the send gate, enrolment, re-engagement and
campaign candidates, which used to query the table itself) matches an address against its own row
and its domain's and every parent domain's (`sam@eu.acme.io` by `@acme.io`). `suppress` matches the
EXACT key only: otherwise one person unsubscribing under a blocked domain upgraded the domain's
block to permanent. `POST /engagement/do-not-contact/bulk` takes up to 1,000 lines (the page
batches), reads each as an address or a domain via `classify_entry`, and reports the rest rather
than guessing. **The page previews before it blocks, and domains start unticked when the file also
holds addresses**: a contact export's website column would otherwise block whole companies.

**The Mailbox apps tab is where the Google and Microsoft apps are set up, not only read**
(`PUT /admin/engagement/setup`, `providers.manage`, audited as `engagement.setup`). It said "set it
under Configuration" for the ids and Provider keys for the secrets, and the operator who reported
it could not find Configuration: the Superadmin tab strip scrolled sideways and the last six tabs
sat past its edge (it now wraps, `Tabs wrap`). The six non-secret settings go through
`runtime_config.service` (`validate` checks every field before any is written, then
`set_override`/`clear_override`), the two client secrets through the provider-key service, sealed,
and PINNED, because `config.secret` takes the first of the pool and would keep using the oldest
key. A blank secret keeps what is stored; the response carries only `secret_hint`.
`campaigns_enabled` stays on Configuration: it is the engine's emergency stop.

**Replies uses one set of buttons** (`test_the_reply_desk_actions_are_one_set_of_buttons`). Every
action is a secondary `Button` with an icon or a `ButtonLink`, Send is the one primary, and ghost is
only a dialog's Cancel. `ButtonLink` (`components/ui`) exists because a hand-styled `<Link>` never
matched the button beside it: "Reply settings" sat 8px taller and a size larger than "Show team".

## Lists (`nexus/lists/`, `routers/lists.py`, `/lists`, `/lists/:listId`)

A list used to be only a saved filter over accounts: built once from the Relevance filter, never
opened, never edited, unable to hold a person. It now has a **kind** (`account` | `contact`,
migration `0064`), holds exactly the members someone put in it, and is the start of a campaign.

- **Membership is fixed.** A filter-built list keeps the accounts that matched when it was saved and
  never re-runs the filter, so a campaign built from it cannot change underneath its author.
- **Kinds do not mix** (`service.add_members` 422s). An account list's campaign emails everyone with
  an address at its companies; a contact list's emails exactly its people. `ListItem.account_id` is
  set on a contact item too (the person's company), which is what lets `tiering._on_a_list` keep
  the company hot without knowing the kind.
- **Ids are validated through the TenantSession; a stranger's id is `skipped`**, never raised, so a
  300-row bulk add does not fail for one row.
- **Archive, never delete, the row.** `campaigns.list_id` (old campaigns) is NOT NULL and
  `engagement_campaigns.source_list_id` points at it. Deleting removes the members and sets
  `archived_at`, so an archived list stops keeping its companies on the hot refresh cycle.
- **Changes belong to the list's maker or a manager** (`_can_edit`, served as `can_edit`). Anyone
  rep+ can make and fill their own lists.
- **A campaign takes a list as itself**: `POST /engagement/campaigns/{id}/contacts` accepts
  `list_id`, expands it with `service.campaign_contact_ids` (deleted and address-less contacts
  never) and records `source_list_id`. The list page's "Start a campaign" hands the id through router
  state; `CampaignBuilder` adds it once the campaign exists, and nothing is drafted or sent until
  review, as before.
- **Add to list** (`components/lists/AddToListModal.tsx`) is the one dialog, offered from Accounts,
  Contacts and discovery results, and it only offers lists of the matching kind that the viewer can
  change. `GET /accounts?q=` searches name and domain across the workspace, because the list page's
  picker would otherwise only ever see the newest 200 accounts.

**Discovery saves what it finds, and its results panel now says so** (2026-09-29). New companies are
`Account(source="discovery")` and new people are contacts as the run goes, but the panel said
neither, and offered a per-row **Research** button that started a `research_account` run: research,
score, draft, then an approval-gated send through the old `SendMessageTool` path, which the
engagement engine's reply desk never sees. The button is gone; the panel states how many were added
with a link to `/accounts?source=found` (Accounts' Source filter now groups raw `source` codes into
Found by AI / Imported / From your CRM / Added by hand), and every row can go on a list.
The account page's **Run pipeline** is now **Refresh now**: "pipeline" read as a sales pipeline.

## Reply switches: acting alone, and the AI sending replies (2026-10-13)

Asked: "a switch the user can enable or disable for auto reply; ensure the user can really set it".
Decided with the product owner: **two switches, each allowed by a manager and chosen per mailbox by
its SDR**, the confidence bar's split (D23). `engagement/settings.reply_automation` is the one place
that combines them; the API, the screens and the engine all read it. `tests/test_auto_reply.py`.

- **Act on clear replies by itself** (`auto_actions_allowed`, default on; `MailboxConnection
  .auto_actions`, default on): today's behaviour. Off, `classify.finalize(act_alone=False)` turns a
  later/declined/unsubscribe into `unclear` however confident, so the sequence pauses (nothing more
  is sent, an unsubscriber included) and a person decides on the desk. Out-of-office and bounces
  stay automatic: they only pause or stop sending.
- **Let the AI send replies** (`auto_send_allowed`, default OFF; `.auto_send_replies`, default off).
  It reverses D22 only where both halves say so. Ingestion only MARKS a reply
  (`ReplyClassification.auto_reply = "pending"`): a confident `interested` or `question` from a
  human, not a colleague, in a thread with no earlier automatic answer. The `send_auto_replies`
  sweep (every tick, `replies/auto_reply.py`) does the work after that commit, and re-checks at
  send time: the switch again (turned off a minute ago is obeyed), the reply still open and
  unanswered, the draft clean under `check_draft`, and **no clock time in it**
  (`names_a_time`), because the AI cannot see the rep's calendar. Its prompt asks which days suit
  instead of offering times (`copy.AUTO_REPLY_CTA_RULE`, `email_rules(automatic=True)`). It sends
  through `desk.send_response`, the SDR's own Send path (do-not-contact gate, exactly-once row,
  `outreach.email_send` charge), raises a `reply_auto_sent` alert, and anything short of that is
  `held` with a sentence in `auto_reply_note` and the draft left on the desk. **One automatic
  answer per thread**, so two programs can never write to each other.
- Stored values are strict booleans: a JSON string "false" is truthy in Python, so `read_settings`
  falls back to the default for anything not a real bool, and both endpoints take `StrictBool`.
  Turning a mailbox switch ON where the workspace does not allow it is a 422; turning one OFF is
  never refused. `MailboxOut` reports the choice, what is allowed, and what is in force
  (`acts_alone`, `sends_alone`) separately, because "chosen" and "in force" are different facts.
- Screens: the allows on Settings > Replies (managers), each SDR's switches on their mailbox card in
  Settings > My mailboxes, and turning on sending asks for confirmation with the four rules above.

## MCP server and OAuth (`nexus/mcp/`) — AI apps

AI assistants (Claude on every surface, ChatGPT, Cursor, VS Code, the model APIs) use the product
through MCP at `POST /mcp`, authenticated by a per-user API key (`nxk_`) or by NEXUS's own OAuth 2.1
server (`nxo_` access, `nxr_` refresh). Spec, plan and marketplace steps:
`docs/superpowers/{specs,plans}/2026-10-13-mcp-server*`, `docs/mcp/listing-guide.md`.

- **A tool is an `/api` request, not a function call** (`gateway.py`). It runs in-process with a
  5-minute token carrying the actor's LIVE role and token version, so RBAC, tenancy, `preflight`,
  metering, validation and the refusal messages are the app's own. Only the (method, path) pairs in
  `gateway._ALLOWED` are reachable, checked before sending; a new tool adds its pair there, and
  `tests/test_mcp_tools.py` fails if a handler sends anything it did not declare.
- **Four platform-global tables** (migration `0068`, no `tenant_id`, workspace in
  `subject_tenant_id`): a bearer is resolved before any workspace is known, the reason `user_mfa`
  has none. So there is NO RLS behind `/api/connections`: every query filters on
  `subject_tenant_id` in code, and a stranger's or colleague's id is a 404. Keep the
  cross-workspace tests when touching it.
- **Every call re-reads the connection, the user's suspension and the membership role**
  (`credentials.resolve`, 30 s cache, `forget()` on revoke or permission change). Demoting someone
  shrinks their apps; removing them from the workspace stops them there.
- **Scopes are `read` / `research` / `write`, chosen per connection.** `tools/list` shows only
  what the connection may use; a call outside its scope is a readable tool error, not a protocol
  fault. Never sending email, launching campaigns, deleting, billing, settings or admin.
- **36 tools reach most of the product** (2026-10-19, branch `feat/mcp-coverage`; 11 read, 13
  research, 12 write). Owner decisions: campaign drafts (opening emails written, never approved
  or launched), Populate, queueing and logging calls (never dialed), removing list members (records
  stay), and the reply desk's read, draft and save. Newer tools live in one module per area under
  `nexus/mcp/tools/` over `common.py`; `catalog.py` keeps the `tools/list` order.
  `test_nothing_reaches_approve_launch_send_dial_or_delete` refuses any tool path past the line. A
  502/503/504 carries the app's own sentence to the assistant; a 500 stays generic.
- **Deploying and listing**: `docs/mcp/listing-guide.md` (Anthropic, OpenAI plugins, Microsoft
  certification, Google, Mistral, Perplexity, xAI, the MCP Registry). Microsoft has no DCR: it needs
  a confidential client from `POST /api/admin/oauth-clients` (no screen yet).
- **Two protocol dialects in one endpoint** (`protocol.py`): the 2025 handshake revisions
  (`initialize`, `MCP-Protocol-Version` header, absent = `2025-03-26`) and the stateless
  `2026-07-28` (`_meta` version, `server/discover`, `resultType`, `ttlMs`/`cacheScope`). No SDK at
  run time; `scripts/mcp_conformance.py` runs the official client (`pip install "mcp==2.3.0"`, in
  its own environment) against any URL in both dialects. It passed against the branch on
  2026-10-13. Note SDK 2.x models are snake_case (`is_error`, `structured_content`).
- **OAuth rules that are load-bearing** (`oauth.py`, `cimd.py`): an unknown client or an
  unregistered redirect is an error PAGE, never a redirect; PKCE S256 required; codes 60 s,
  single-use, and a replayed code revokes what it issued; refresh tokens rotate and reuse revokes
  the family; loopback redirects match on any port. Client ID Metadata Documents are fetched
  through `sources/safety._is_blocked_host` with no redirects, 5 s, 64 KB, and the document must
  name its own URL. `/mcp`'s 401 carries `WWW-Authenticate: Bearer resource_metadata=...`, which is
  how Claude and ChatGPT discover sign-in; the protected-resource `resource` must equal
  `{app_base_url}/mcp` exactly, so production needs the App public address set.
- **Verified means proven, not named**: a superadmin-registered client, a metadata document served
  from a known host, or every redirect https on a known host (a code only reaches a registered
  redirect). A self-registered "Claude" pointing anywhere else reads Unverified.
- **Customer-facing name is InfoJoy GTM** (`nexus/mcp/brand.py`, `frontend/src/lib/brand.ts`):
  tool messages, server info, consent screen, plugin and registry entries. Tool descriptions and
  public docs name no vendor (`tests/test_mcp_tools.py`, `test_mcp_distribution.py`).
- **Gate and switches**: `module.mcp` (on by default, on no plan) is checked with `preflight` on
  `tools/list` and `tools/call`; a platform switch always refuses, a plan only under enforcement.
  Revoking is never refused. Runtime settings: `mcp_rate_limit_per_minute`,
  `mcp_allowed_origins`, `openai_apps_challenge_token` (served at
  `/.well-known/openai-apps-challenge`).
- Screens: AI apps (Settings > AI apps, `/settings/ai-apps` since 2026-10-13; `/connected-apps`
  forwards; every member, hideable page) and the consent screen
  (`/oauth/consent`, outside the app shell; only that path survives login via `nexus.returnTo`).
  Marketplace artifacts: `distribution/claude-plugin/` (validated with Claude Code 2.1.296; older
  CLIs reject `displayName`) and `distribution/mcp-registry/server.json`.

## Prospector (`nexus/prospector/`, `/prospector`) — cold prospect search

Reps search the InfoJoy Search API (a B2B database of contacts and companies, billed to us per
record returned), browse partially masked results for free, and pay per field delivered to save,
list or export. Spec, 25 owner decisions (D1-D25), plan and the first real calls:
`docs/superpowers/{specs,plans}/2026-10-16-prospect*`, `.../2026-10-16-prospector-observed-api.md`.

- **Masking is structural.** `ProspectRowOut` (and the frontend `ProspectRow`) declares no raw email,
  phone, domain or LinkedIn field; `presenter.py` is the one builder. A held row carries the
  workspace's OWN values in `record`. `test_no_unmasked_value_appears_in_any_search_response` (and the
  MCP twin) prove no fixture value reaches the browser or the model. Row keys are tenant-bound HMACs,
  never `sha256(email)`.
- **Pay per field, once** (D4-D6): `prospector.email` and `prospector.company` 1 credit each,
  `prospector.phone` 1 credit only when "Include direct phones" is ticked; companies taken through
  the Contacts view are free; `prospector_purchases` (the ledger) makes re-adding, re-exporting and
  retries free; held rows are linked, never rewritten. `quote` -> `preflight_all` (402 before work)
  -> one savepoint {records, list items, ledger, `metered(..., idempotency_key=prospector:<req>:<cap>)`}.
  Browsing is recorded as `prospector.search_rows` (observe-only, COGS) and never charged.
- **Cost guards** (D11, D21, D22): `prospector_rate_limit_per_minute` (30/person), a daily row budget
  per workspace counted in `prospector_daily_rows` (enforced whatever billing enforcement says): the
  plan's quota on `prospector.daily_rows` (raise it per plan or per customer in the plan editor), else
  `prospector_daily_rows_default` (2,500). Net new is a capped scan (`prospector_net_new_scan_rows`,
  500) with a signed, tenant-bound cursor. Total and Net new do not search until a filter is set.
- **The shared cache** (`prospector_pages`, D10) is platform-global, Fernet-sealed, 24 h, pruned every
  tick; a failed call is never cached. Cache writes finish before any tenant write in a request.
- **The Saved tab reads workspace data**, never the provider (D13); the NEXUS-only filters (email
  status, lists, signals, funding, job postings) work only there (D14). Email Opened does not exist:
  NEXUS does not track opens.
- **Vendor emails are kept unverified and held out of campaigns** (D19): contacts are saved
  `enrichment_source="sourcing:prospector"`; `engagement/sequences.enroll` skips them
  (`unverified_prospector_email`) until `email_status` is valid, catch_all or risky. `POST
  /prospector/verify` releases them, free for Prospector-delivered addresses (D20), `verify.email`
  for any other contact; >25 go to the `prospector_verify_contacts` job.
- **Exports are formula-safe** (`csv_export.safe_cell`) and charged like delivery, committed before
  the file is returned. A value made only of digits and phone punctuation is written as it is, so
  phones never gain an apostrophe (owner, 2026-10-19); `savedCsv.ts` applies the same rule in the
  browser. The Accounts and Contacts exports adopt the same `safe_cell` on branch
  `fix-csv-formula-injection`, whose copy of it is byte-identical, so the two merge cleanly.
- **The provider answers since 2026-10-22, slowly and without counts** (observed shape and faults:
  `docs/superpowers/specs/2026-10-16-prospector-observed-api.md`, "Third look";
  `tests/test_prospector_observed_provider.py`). A contact's `Phone` is the office line, so only
  `DirectPhone` is a direct phone (reading `Phone` sold a switchboard as a direct dial, 29 rows in
  50); the person's `EmployeeLinkedIn` is masked like the rest and delivered with the contact,
  through `person_profile_url`. Seniority, size and revenue are checkbox lists of the observed
  values, sizes sent with every stored spelling (`FieldSpec.aliases`). No count is asked for
  (`client.COUNT_SUPPORTED`; contacts 500s after 120 s, companies return no total), so "Show
  total" is hidden. Cold starts took 14-124 s: the read timeout is 60 s, and every provider wait
  hands the request's database connection back to the pool (`core.tenancy.connection_released`,
  which refuses to commit unwritten work and re-binds RLS after; measured: one connection "idle in
  transaction" for the whole wait before). A Net new scan stops after `NET_NEW_DEADLINE_S` (45 s)
  with a cursor.
- MCP: `prospector_search` (read, masked) and `prospector_save` (write, charged), both through
  `gateway._ALLOWED`.
- UI: `frontend/src/pages/prospector/` (filter state lives in the URL `f=` param; the rail, table,
  selection bar and confirm dialog are separate files). The nav item, the route and
  `features/pages.py` all carry `module.prospector`.

## Frontend skills — USE THESE for any UI work

Four design/frontend skills are installed in `~/.claude/skills/`. Invoke them via the `Skill`
tool when designing or building UI. Pick by task:

| Skill | Invoke for | Notes |
|-------|-----------|-------|
| **impeccable** | Dashboards, product UI, app shells, components, forms, settings, onboarding, empty states, UX audits, design systems/tokens, responsive behavior, theming. | **Primary skill for NEXUS UI.** Reads `PRODUCT.md` / `DESIGN.md` for grounding. |
| **ui-ux-pro-max** | Style/visual direction (50+ styles, 161 palettes), and the 99 UX guidelines. Accessibility is priority #1: contrast ≥ 4.5:1, visible focus rings, full keyboard nav, ≥ 44×44px touch targets. | Use for palette/style decisions and UX-rule checks. |
| **framer-motion** | Motion, transitions, micro-interactions in React. Respect `prefers-reduced-motion`. | Animate transform/opacity; motion must communicate, not decorate. |
| **design-taste-frontend** (taste-skill) | Landing pages, marketing sites, portfolios **only** — NOT product/dashboard UI. | Rarely needed here; the app is product UI. |

Grounding docs for the design skills:
- `PRODUCT.md` — what NEXUS is, who uses it, the screens.
- `DESIGN.md` — the design system: tokens, type scale, components, accessibility rules.

## Settings is tabs (`frontend/src/app/settingsTabs.tsx`, 2026-10-13)

Asked: "move connected apps, members and mailboxes into Settings with well-structured tabs". My
mailboxes, AI apps and Members were separate main-menu items beside an admin-only Settings page.
Settings is now one frame for every member (`pages/settings/SettingsLayout.tsx`), its tabs grouped
by whose setting it is: **You** (My mailboxes, AI apps, Notifications), **Team** (Replies, Members),
**Workspace** (Workspace, Email & signature, Billing; admin).

- **`SETTINGS_TABS` is the one list**: the tab menu, the page titles (`AppShell.titleFor`, with an
  optional `title` where a label alone is ambiguous in the top bar) and the superadmin's page
  switch (`features/pages.py`, pinned by `tests/test_page_visibility.py`) all read it.
- A tab uses the main menu's own rules (`canSee`, `navState`, `isPageHidden`): `minRole`,
  `capability`, `engine`, and `page` for the switch that hides it. A tab that moved keeps the
  switch it had: Notifications hides with Alerts, Replies with the Replies desk. Page-visibility
  rows are stored by KEY, so a hide set on `/mailboxes` or `/connected-apps` still applies.
- Each tab is its own route nested under `/settings` (absolute child paths, so the route tests'
  regex still sees them) and keeps its old guards. The old addresses forward with their query
  string (`Forward` in `App.tsx`): the mailbox OAuth callback returns with `?connected=`, and now
  lands on `/settings/mailboxes` directly. `/settings` opens the Workspace tab for admins and owners
  and the first open tab for everyone else.
- The menu is a `<nav>` of links with `aria-current`, not an ARIA tablist: every tab changes the
  address. Below 900px it becomes one scrolling row. Inside the frame `PageHeader` renders an h3
  and drops its back link (`NestedPageContext`), so there is one page title.
- `components/ui/Switch.tsx` (`Switch`, `SwitchRow`) is the on/off control for settings that apply
  the moment they flip; a Save button saves only the fields beside it.

## Frontend conventions (`frontend/`)

- Stack: React 18 + TypeScript (strict) + Vite. Curated deps only: `react`, `react-dom`,
  `react-router-dom`, `framer-motion`. No UI kit — we own our component library.
- Styling: CSS custom-property design tokens in `src/styles/tokens.css` + CSS Modules.
  No inline magic numbers; consume tokens (`var(--space-4)`, `var(--color-accent)`).
- Components: reusable primitives in `src/components/ui/` (typed props, `forwardRef`,
  loading/disabled/empty/error states). Screens compose primitives — never re-style ad hoc.
- Accessibility is non-negotiable: semantic HTML, labelled controls, keyboard support,
  visible focus, `aria-*` where needed, reduced-motion fallbacks.
- **A loading indicator is essential motion: under reduced motion it slows, it never stops.**
  `global.css` sets `animation-duration: 0.01ms` and `animation-iteration-count: 1` on `*` with
  `!important`, which froze every spinner into a still ring after one tick. That was reported as
  "spinners get stuck", and measured in Chromium as a `1e-05s x 1` animation the browser drops.
  `Spinner.module.css` and `WorkingIndicator.module.css` override it with `!important`. At equal
  importance a class outranks `*`; without `!important` the override is dead code. Keep both.
  `tests/test_loading_indicators.py` pins this.
- Long actions (lookalikes, similar people, find contacts, AI actions) use `WorkingIndicator`, not a
  bare `Spinner`. It shows a moving bar, elapsed seconds, and one line about the work. Any duration
  it states must be measured (drafts: p90 5s over 30 days) or a service ceiling. It never shows step
  names on a timer, because the server does not report stages.
- Every data view handles **loading (skeletons), empty, and error** states explicitly.
- Build output (`frontend/dist`) is served by FastAPI as static with SPA fallback.

When you touch UI, invoke `impeccable` (and `ui-ux-pro-max` for visual/UX decisions,
`framer-motion` for animation) before writing code, and follow `DESIGN.md`.

## 🔍 Code Navigation & Analysis — Code-Review-Graph & Superpowers

This repo is indexed by **code-review-graph** (a Tree-sitter knowledge graph in `.code-review-graph/`).
It is registered as an MCP server and cuts code-review token use by ~90% (returns scoped slices
instead of full files).

### How to Use Code-Review-Graph for Every Task

**Before implementing or reviewing:**
```bash
# Find where a function/class is defined and what calls it
code-review-graph search "TenantSession"

# See what changed (for code review context)
code-review-graph detect-changes --brief

# View the interactive graph (start here!)
open .code-review-graph/graph.html
# or on Windows:
start .code-review-graph/graph.html
```

**In Claude** (automatic):
- `/code-review` — automatically uses graph to focus on affected files only
- `/code-review medium` — medium effort (less token use, uses graph)
- `/code-review ultra` — deep review (multi-agent, still uses graph for context)

### Superpowers Analysis — Always Available

Three comprehensive guides were generated and are always available for reference:

| File | What It Contains | Use When |
|------|------------------|----------|
| **SUPERPOWERS-ANALYSIS.md** | Complete system architecture, all 6 subsystems, security review, code review methodology, risk areas | Understanding any part of the system; before major changes |
| **NEXUS-GTM-QUICK-START.md** | Commands, structure, environment setup, testing, Docker, code quality standards | Quick reference, local dev setup, running tests |
| **nexus-gtm-code-review-setup.md** | Graph statistics, initialization details, MCP config, entry points | Understanding graph setup; API entry points |

**Read these first** before implementing or reviewing — saves re-deriving architecture.

### Graph Maintenance

- **After pulling code**: `code-review-graph update` (incremental re-index)
- **After major refactoring**: `code-review-graph build` (full re-index)
- **The indexer only sees git-TRACKED files.** A brand-new file is invisible to it — including to a
  full `build` — until it is at least `git add`ed. Measured 2026-08-04: after adding nine files for
  the source-database subsystem, both `update` and `build` reported success and indexed **none** of
  them; `git add -N` then `build` picked all nine up (517→528 files, 4916→5076 nodes). Incremental
  `update` is worse still: it re-indexes *modified* tracked files only, so it silently misses new
  ones even when they are staged. After adding files, run `git add` then `build`, and verify a new
  symbol is actually present rather than trusting the summary line.
- The `.code-review-graph/` directory is gitignored; never commit it.
- If MCP tools aren't visible, restart Claude to load the server.
