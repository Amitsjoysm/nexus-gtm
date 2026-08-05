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

## Migrations

Alembic under `migrations/versions/`. Head: `0042_account_next_refresh`. The chain is
`0020_baseline_schema` (a **frozen, literal-DDL squash** of the old 0001–0020) → `0021`–`0026`
(the Billing tables below) → `0027` (`dead_letter_jobs`, job durability) → `0028` (`user_mfa` +
`mfa_recovery_codes`) → `0029` (`platform_admins.permissions`) → `0030` (`signal_source_runs`) →
`0031`–`0040` (page snapshots, notification preferences, feature flags, contact soft-delete,
`companies`, proration, shared `people`, `crawl_verdict`, user suspension, digest delivery) →
`0041` (`source_databases`) → `0042` (`accounts.next_refresh_at`). Every tenant-scoped table gets RLS via
`scripts/apply_rls.py` on deploy — no manual policy work needed for new tables.

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
- Still to build (step 7): the enrichment provider itself. **Failure posture when it lands: fall
  through to the paid provider, never stop collection.** It is an optimisation, not a dependency.

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
| `crunchbase_org` | `pratikdani/crunchbase-companies-scraper` | **none** |
| `company_search` | `bhansalisoft/crunchbase-scraper-without-login` | **none** |

The last two are registered and called by nothing. Before wiring `company_search`, note it has 42
total runs across 2 users — an abandoned actor is a dependency that disappears without notice.

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
