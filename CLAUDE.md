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
  (`addopts = "-n auto --dist loadfile"` in `pyproject.toml`): ~1,350 tests where `fresh_db` drops
  and recreates every table before each one, so the run is dominated by per-test schema work that
  parallelises almost perfectly — serial it is ~55 min. `conftest.py` gives each worker its own
  SQLite file via `PYTEST_XDIST_WORKER`, which is what makes this safe; without it one worker's
  `drop_all` would wipe another's tables mid-test. `--dist loadfile` keeps a file on one worker,
  because several suites share module-level state (ingestion service, agent runtime, seeded
  catalogs) and splitting a file turns that into flaky cross-talk. Use `-n0` when debugging — serial
  tracebacks are easier to read.
- `docs/` — specs and design docs.

## Backend conventions

- Env config uses the `NEXUS_` prefix (pydantic-settings). Production must reject the insecure
  default JWT secret (enforced by a config validator).
- Reduce external dependencies. Prefer the standard library and what's already vendored.
- Every endpoint is tenant-scoped and RBAC-gated. Never bypass `TenantSession`.

## Relationship Graph (`nexus/network/`)

A tenant-scoped, deduped graph of each rep's real network (contacts + calendar), used for
NL "who do we know" search (A1) and warm-intro path mapping (A4). Additive subsystem — never
touches Account/Contact/Inbox/Cadence.

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
- **Salesforce is known but not live.** `SalesforceConnector.fetch_accounts` returns an injected
  sample, so `PUT /crm/connection` 400s for it and `test_connection()` says so plainly. Storing a
  credential we cannot use would be a silent no-op for the customer.
- The resolution cache keys on `updated_at|provider|api_base` and **still reads the row every
  call** — that lookup is how a worker notices a credential the API just changed. It buys instance
  stability (the `MAX_RECORDED_PUSHES` buffers) and a skipped decrypt, not a skipped query. N+1
  pressure is handled by hoisting resolution out of inner loops.

## Migrations

Alembic under `migrations/versions/`. Head: `0049_integration_connections`. The chain is
`0020_baseline_schema` (a **frozen, literal-DDL squash** of the old 0001–0020) → `0021`–`0026`
(the Billing tables below) → `0027` (`dead_letter_jobs`, job durability) → `0028` (`user_mfa` +
`mfa_recovery_codes`) → `0029` (`platform_admins.permissions`) → `0030` (`signal_source_runs`) →
`0031`–`0040` (page snapshots, notification preferences, feature flags, contact soft-delete,
`companies`, proration, shared `people`, `crawl_verdict`, user suspension, digest delivery) →
`0041` (`source_databases`) → `0042` (`accounts.next_refresh_at`) → `0043` (`signal_events.subtype`) →
`0044`–`0046` (`provider_keys`, `provider_settings`, `payment_credentials`) → `0047`–`0049`
(`crm_connections`, `audit_log`, `integration_connections`). Every tenant-scoped table gets RLS via
`scripts/apply_rls.py` on deploy — no manual policy work needed for new tables.

**Two feature branches both claimed 0044–0046 and merging them produced two alembic heads**, which
`upgrade head` refuses to run against. Both had branched from `0043_signal_subtype`. Resolved by
renumbering one chain to `0047`–`0049` and rebasing it onto the other's head, rather than adding a
merge revision — neither had been applied anywhere, so no stamped database remembered the old ids.
If you branch for more than a day, check `ScriptDirectory.get_heads()` returns exactly one before
merging; the collision is invisible until a deploy.

Migrations are **additive only**, and the chain **is** replayable onto an empty database —
`tests/test_migrations_replay.py` builds one from nothing but `alembic upgrade head` and diffs
the result against `Base.metadata`. Do not reintroduce a `create_all()` inside a revision: the
old `0001_initial` did that, so it materialized whatever models existed *at run time* rather
than a frozen historical schema, and the chain could never be replayed. Production still runs
`bootstrap_db.py` → `alembic upgrade head` → `apply_rls.py`.

### Two traps that only bite against real Postgres

The suite runs on SQLite, which has **no RLS**. Both of these pass every test and fail in prod:

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
- **Money flows through one seam.** `metered()` → quota → credits → overage price → block.
  Credits are pre-paid, so rating deducts what a period's burns already covered — otherwise the
  customer pays twice for one overage. Collection is keyed by invoice id at the provider, so a
  retry can never double-charge. Dunning (`nexus/billing/dunning.py`) retries on a config
  schedule and escalates to `past_due`; it never silently voids a debt.
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
- **Gauges are not counters.** `seat.member` resolves to live membership count; summing events
  would only ever climb, so a customer could never get back under a seat limit.
- Platform-global tables carry no `tenant_id` and no RLS policy: `billing_capabilities`,
  `billing_plans`, `billing_plan_entitlements`, `billing_rate_cards`, `billing_cost_rates`,
  `platform_admins`, `billing_audit_log`, `billing_webhook_events`. The audit and webhook tables
  deliberately name their tenant column `subject_tenant_id` so `apply_rls.py` — which enrolls any
  table having `tenant_id` — does not hide them from the operators who must read them.
  `dead_letter_jobs` (below) follows the same rule for the same reason.

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
- **No Stripe object is created.** `create_checkout` calls `ensure_plan_price` on first purchase, so
  a draft nobody bought does not litter the Stripe account with products.

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
- **A failed re-verification also deactivates.** Leaving a now-broken credential live because it
  passed last month is how a silent billing outage lasts a month.
- The active credential **refuses deletion**; deactivation is **never** refused, because during an
  incident "stop taking money through this account" must not be blocked by a state machine.

`resolve_payment_provider()` is async with a **30s TTL**, mirroring `providers/resolver.py` — the
worker is a separate process, so without a TTL a credential change would need a restart. It falls
back to the environment on any failure, which is also what makes the table additive: a deployment
that never opens the screen behaves exactly as before. `get_payment_provider()` stays for the
synchronous callers and for `set_payment_provider` test injection, which always wins.

`stripe_publishable_key` is read with `getattr` — Settings has no such field, because the publishable
key has never been needed server-side.

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
Repointing the global setting to diversify signal collection takes those down silently — the base
`find_similar` returns `[]`, so lookalikes report "no results" with nothing in the logs.

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
  or there is a signal in the last `account_hot_signal_window_days`, or it is in an active cadence,
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

**`run_worker` is still strictly serial** (measured effective concurrency 0.99) and there is one
replica. That is the remaining gap: ~15.65s per account is 0.064/s against 5.11/s demand at a 15%
hot ratio. Bounded in-flight concurrency is the obvious next lever — `process_account` is ~99%
await-on-network — but it must be capped by the DB pool, since each in-flight job holds a session.

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
Checkout. There is **no endpoint to create a new sellable plan** — a ninth public tier still needs a
`plans.py` change and a deploy.

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

## Frontend conventions (`frontend/`)

- Stack: React 18 + TypeScript (strict) + Vite. Curated deps only: `react`, `react-dom`,
  `react-router-dom`, `framer-motion`. No UI kit — we own our component library.
- Styling: CSS custom-property design tokens in `src/styles/tokens.css` + CSS Modules.
  No inline magic numbers; consume tokens (`var(--space-4)`, `var(--color-accent)`).
- Components: reusable primitives in `src/components/ui/` (typed props, `forwardRef`,
  loading/disabled/empty/error states). Screens compose primitives — never re-style ad hoc.
- Accessibility is non-negotiable: semantic HTML, labelled controls, keyboard support,
  visible focus, `aria-*` where needed, reduced-motion fallbacks.
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
