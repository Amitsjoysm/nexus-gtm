# SDR Engagement Engine: Implementation Roadmap

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement each phase plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Execute phases in the order below; a phase may start only when every phase it depends on is merged into `feat/sdr-engagement` with the full suite green.

**Goal:** Replace today's account-first Campaigns and Cadences with an engine that runs an SDR's real workflow end to end: people-first lists, personalised first emails reviewed in a queue, threaded follow-ups sent through the SDR's own Gmail or Microsoft 365 mailbox, replies read and classified, long-horizon re-engagement, a reply desk with AI-drafted responses, reporting, and a consented training and insights ledger.

**Spec:** [`docs/superpowers/specs/2026-09-17-sdr-engagement-design.md`](../../specs/2026-09-17-sdr-engagement-design.md) (approved 2026-09-17). Decision numbers (D1–D29) and section numbers (§1–§19) below refer to it.

**Architecture:** A new package `nexus/engagement/` built as a functional core with an imperative shell. Every decision (subject normalisation, date resolution, business-day scheduling, reply matching, bounce and auto-reply detection, the enrollment state machine, the credit estimate, the action rules) is a pure function tested directly on real data. Side effects (Gmail API, Microsoft Graph, the LLM, the three Supabase stores) sit at the edge behind small adapters that are exercised by live suites against real accounts. There are no fakes (D21).

**Tech Stack:** FastAPI, async SQLAlchemy 2.0, Alembic, Pydantic v2, httpx (Gmail REST, Microsoft Graph, OAuth token endpoints), Python stdlib `email` (MIME build and parse), `zoneinfo` + `tzdata`, asyncpg (ledger stores), python-jose (Pub/Sub OIDC verification), React 18 + TypeScript strict + CSS Modules.

---

## 1. Phase order

| # | Plan | Delivers | Depends on | Release |
|---|---|---|---|---|
| 01 | [01-foundation.md](01-foundation.md) | Migration `0057_engagement`, all models, `tzdata`, RBAC permissions, pure libraries: ids, subjects, timekeeping, date resolution | — | A (dark) |
| 02 | [02-platform-configuration.md](02-platform-configuration.md) | Superadmin OAuth app keys (Google, Microsoft), ledger store credentials and pseudonym secret, runtime settings with validators, health rows, Mailbox apps tab, guided setup docs, live-suite scaffold + CI job | 01 | A |
| 03 | [03-mailbox-connections.md](03-mailbox-connections.md) | `MailProvider` protocol, Gmail and Graph adapters, OAuth connect/refresh/revoke, Settings → Mailboxes | 01, 02 | A |
| 04 | [04-suppression.md](04-suppression.md) | Do-not-contact list, signed unsubscribe links, RFC 8058 one-click endpoint, lift with audit, Settings → Do-not-contact, contact badge | 01 | A |
| 05 | [05-ledger-capture.md](05-ledger-capture.md) | `training_consents`, sign-up consent, existing-workspace prompt, settings toggle, `ledger.emit()` transactional outbox, emit seams across today's app | 01 | A |
| 06 | [06-ledger-stores.md](06-ledger-stores.md) | Archive/training/insights stores, versioned SQL, `ship_ledger`, pseudonymisation + scrubber, dataset builders, export script, opt-out deletion, person erasure, outbox retention | 02, 05 | A |
| 07 | [07-sending.md](07-sending.md) | MIME builder, headers, opt-out footer, message log, pre-send checks, idempotent send with reconciliation, provider-limit pause, volume warning | 03, 04, 05 | B (dark) |
| 08 | [08-sequences-and-drafting.md](08-sequences-and-drafting.md) | Templates, campaigns, steps, enrollments, context pack, personalisation check, first-email drafting, review queue, credit estimate + gate, due claim, JIT follow-ups, timing modes, overrides, duplicate-outreach guard, `advance_engagement` | 07 | B (dark) |
| 09 | [09-reply-ingestion.md](09-reply-ingestion.md) | Pub/Sub and Graph webhooks, `sync_mailboxes`, notification renewal, parsing, matching, privacy filter, bounce/auto-reply detection, classification, confidence bar, action rules, engagement alerts | 03, 04, 07, 08 | B (dark) |
| 10 | [10-reply-desk.md](10-reply-desk.md) | Reply desk service and API, decisions, AI response drafts, send in thread, save to provider Drafts, colleague resume/stop, reassign, reply-speed reminder | 09 | B (dark) |
| 11 | [11-engagement-ui.md](11-engagement-ui.md) | Campaign builder, review queue, launch, campaign detail, reply desk page, sequence templates page, conversation timeline, workspace confidence + consent settings | 08, 10 | B (dark) |
| 12 | [12-reporting.md](12-reporting.md) | Funnel and step reports, per-SDR response time, Outcome writes, Today plan, mailbox health | 09, 11 | B (dark) |
| 13 | [13-insights.md](13-insights.md) | Insights client, display rules (D26), reply likelihood, best-time suggestion, badges | 06, 12 | B (dark) |
| 14 | [14-enhancements.md](14-enhancements.md) | Referral follow-through, CRM activity logging, signal re-engagement | 10, 13 | B (dark) |
| 15 | [15-cutover.md](15-cutover.md) | `scripts/migrate_engagement.py` with dry run, rewiring every consumer of the old engine, removal of old code paths and pages, release verification | 01–14 | B (cutover) |

"Dark" means merged and deployed with `engagement_campaigns_enabled = false` (runtime setting added in 02): workers skip engagement jobs, the new nav items and routes are hidden, and today's Campaigns and Cadences keep running untouched. Mailbox connection (03), do-not-contact (04) and the ledger (05, 06) are live in Release A because the cutover needs connected mailboxes and the ledger should start collecting before it.

---

## 2. Ground rules for every phase

1. **Worktree and branch.** Work happens in `.claude/worktrees/sdr-engagement` on `feat/sdr-engagement`. Rebase onto `master` before each phase starts. Before merging a phase, check `alembic heads` returns exactly one head (`tests/test_migrations_replay.py::test_chain_has_exactly_one_head`).
2. **No mocks, no fakes (D21).** Do not add `httpx.MockTransport`, monkeypatched providers or a fake `MailProvider` in new tests. Pure logic is tested with real inputs; database behaviour with real SQLite rows through `tenant_session`; provider behaviour in `tests_live/engagement/` against real mailboxes and real Supabase projects. Where a test needs an LLM answer it either runs in the live classification suite or feeds the pure function the parsed result it would have received.
3. **Functional core.** When a task mixes a decision with IO, split it: a pure function that returns a plan (`SendPlan`, `ReplyActions`, `ScheduleResult`) and a thin shell that executes it. Test the plan offline.
4. **Tenant safety.** Every table added carries `tenant_id` unless the plan says otherwise, so `scripts/apply_rls.py` enrols it. Request paths use `get_tenant_session`; workers use `nexus.workers.tasks.tenant_session`. Cross-tenant sweeps read only tenant ids with a raw session (the worker connects as owner) and then open a `tenant_session` per tenant. API cross-tenant reads use `get_platform_sessionmaker()`.
5. **Never break the caller.** `ledger.emit()`, alert raising, metering, audit writes and insight reads never raise into the action they accompany (same rule as `record_audit` and `metered`).
6. **Secrets.** OAuth client secrets, Supabase connection strings and the pseudonym secret live only in the provider-keys store (sealed, never returned). Nobody pastes them into chat, env files committed to git, or tests. Live suites read them from environment variables injected by CI secrets.
7. **Billing seam only.** Application code calls `metered()` or the engagement credit gate in `nexus/engagement/sequences/credits.py`; it never names a plan or a price.
8. **Frontend.** Invoke the `impeccable` skill before any UI task and follow `DESIGN.md`. Every data view handles loading (skeletons), empty and error states. Long actions use `WorkingIndicator`. UI behaviour is pinned by source-reading tests in `tests/` (there is no frontend test runner).
9. **Verification before a phase is done.**
   - `ruff check nexus tests`
   - `pytest -n auto -p no:cacheprovider --timeout=120 -q` (full suite, not a subset)
   - `cd frontend && npm run typecheck && npm run build` when the phase touches the frontend
   - `pytest tests_live/engagement -q` when the phase adds live tests and the secrets are present
10. **Commits.** One commit per task minimum, message style `feat(engagement): …` / `test(engagement): …`, ending with the `Co-Authored-By` trailer. Nothing is pushed unless the owner asks.

---

## 3. Implementation decisions that refine the spec

These do not change any product decision; they settle details the spec left to implementation. Each is repeated in the phase that implements it.

| Topic | Decision | Why |
|---|---|---|
| Migration shape | One migration `0057_engagement` in phase 01 creates every engagement and ledger table, plus `mailbox_connections.timezone`, `pending_registrations.training_consent` and `call_tasks.engagement_enrollment_id`. | The spec names one migration; creating tables early is harmless (additive, unused) and avoids a second revision colliding with other branches. |
| Timezones | Add `tzdata` to `[project].dependencies`. The SDR's timezone is `mailbox_connections.timezone` (IANA, captured from the browser at connect, editable). Contact timezone: `contacts.custom_fields["timezone"]` if valid → `accounts.country` (+ `accounts.region` for US, CA, AU, BR, MX) → the mailbox timezone. | Local Python has no zone database (`ZoneInfoNotFoundError` on Windows, measured 2026-09-17); contacts have no timezone column. |
| Permissions | New `Permission.run_engagement` (rep+) and `Permission.manage_engagement` (manager+). | SDRs are reps; today's `manage_campaigns` is manager-only. Managers set the confidence range (D23) and see the team desk. |
| Rollout flag | Runtime setting `engagement_campaigns_enabled` (bool, default `false`). | Release A ships dark; the cutover flips it. |
| Outcome attribution | `Outcome.meta` carries `engagement_campaign_id`, `enrollment_id`, `message_id`. `Outcome.campaign_id` stays the old FK. | Spec §13: Outcome rows unchanged. |
| Alerts | Engagement categories live in `nexus/alerts/rules.py::_ENGAGEMENT_RULES`; `ALERT_CATEGORIES` becomes the union. Engagement alerts carry `meta.owner_user_id` and route with the mailbox owner, not the account owner. | Categories must stay derived (existing rule); the mailbox owner is who acts (D12). |
| Webhook trust | Gmail push: verify the Pub/Sub OIDC JWT (RS256, Google certs, audience = our push URL) **and** a per-deployment path token. Graph: validation-token echo on subscribe, `clientState` = HMAC of the subscription id checked on every notification. | Both endpoints are public; a forged notification must not trigger a pull for someone else's mailbox. |
| Provider drafts | "Save to Drafts" for OAuth mailboxes uses Gmail `users.drafts.create` and Graph `createReply` (draft). IMAP is not used by the engine. | D1/D2: OAuth only. |
| Ledger store driver | asyncpg through SQLAlchemy async engines built per DSN; `postgres` extra already includes asyncpg. Parquet export uses an optional extra `export = ["pyarrow>=15"]`; the script exits with a clear message when it is missing. | No new runtime dependency for the app image. |
| Old sourcing module | `nexus/campaigns/sourcing.py` moves to `nexus/contacts/sourcing.py` during cutover (it is used by `routers/accounts.py` and `routers/agents.py`, not only by campaigns). | Removing `nexus/campaigns/` must not break contact sourcing. |

---

## 4. File structure

### New backend package

```
nexus/engagement/
  __init__.py
  ids.py                 01  ULID generation (event ids, X-Nexus-Ref values)
  subjects.py            01  normalize_subject, reply_subject
  timekeeping.py         01  zone resolution, business days, next send times
  dates.py               01  resolve_date(phrase, received_at, tz)
  settings.py            01  workspace engagement settings (confidence bar, reminder hours) in Tenant.email_settings["engagement"]
  config.py              02  OAuth app + public base URL resolution from provider keys and runtime settings
  mailboxes/
    provider.py          03  MailProvider protocol, SentRef, ThreadRef, InboundMessage, ProviderLimit, errors
    tokens.py            03  seal/unseal token bundles, refresh-if-expiring
    oauth.py             03  scopes, authorize URL, code exchange, state
    gmail.py             03  GmailProvider (Gmail REST v1)
    graph.py             03  GraphProvider (Microsoft Graph v1.0)
    registry.py          03  provider_for(connection)
    service.py           03  connect, list, disconnect, status transitions
  suppression/
    tokens.py            04  signed unsubscribe tokens
    service.py           04  suppress, lift, is_suppressed, list
  messages/
    mime.py              07  build_outbound_mime
    parse.py             09  parse raw RFC 5322 into InboundMessage
    service.py           07  thread + message persistence, sent_today
  sending/
    checks.py            07  pre-send checks
    service.py           07  send_message: queue, send, reconcile, limits
  sequences/
    state.py             08  enrollment state machine (pure)
    schedule.py          08  next_action_at computation (pure)
    templates.py         08  sequence template service
    campaigns.py         08  campaign service
    enrollments.py       08  enrollment operations and overrides
    credits.py           08  worst-case estimate + gate (D18)
    guard.py             08  duplicate-outreach guard (§19)
    claim.py             08  due-enrollment claim (SKIP LOCKED)
    engine.py            08  advance one enrollment
  drafting/
    context.py           08  context pack (§7)
    personalisation.py   08  specific-fact check (D17)
    drafts.py            08  first email, follow-up, re-engagement drafts
  replies/
    detect.py            09  bounce + auto-reply detection
    match.py             09  five matching rules
    classify.py          09  AI classification, output parsing, confidence gating
    actions.py           09  action plan (pure) + apply
    webhooks.py          09  Pub/Sub OIDC + Graph clientState verification
    ingest.py            09  per-mailbox sync pipeline
  alerts.py              09  raise engagement alerts
  desk/
    service.py           10  desk queries, decisions, responses, reassign
    responses.py         10  AI response drafts (ai.reply_draft)
    reminders.py         10  reply-speed reminder
  reporting/
    service.py           12  funnel, per step, categories, time to first response
    today.py             12  Today plan
    health.py            12  mailbox health
  insights/
    client.py            13  read profiles from the insights store
    rules.py             13  display rules (D26)
    likelihood.py        13  reply likelihood from own ICP fit
    best_time.py         13  best-time suggestion
  enhancements/
    referral.py          14  referral follow-through
    crm_log.py           14  CRM activity logging
    signal_reengage.py   14  signal re-engagement suggestions
  ledger/
    envelope.py          05  envelope builder + schema version
    emit.py              05  emit() into ledger_outbox
    consent.py           05  consent state, record decision
    stores.py            06  store engines per DSN, permission checks
    sql/                 06  versioned SQL for archive/, training/, insights/
    schema.py            06  apply versioned SQL
    shipper.py           06  ship_ledger batches to archive
    pseudonym.py         06  HMAC keys
    scrub.py             06  text scrubber
    datasets.py          06  dataset builders
    deletion.py          06  workspace + person deletion, verification report
  cutover/
    migrate.py           15  old engine → new engine
```

### New models

- `nexus/models/engagement.py` (01): `MailboxConnection`, `SequenceTemplate`, `EngagementCampaign`, `EngagementStep`, `EngagementEnrollment`, `EngagementThread`, `EngagementMessage`, `ReplyClassification`, `DoNotContact`.
- `nexus/models/ledger.py` (01): `TrainingConsent`, `LedgerOutbox`.

### New API routers

| Router | Phase | Auth |
|---|---|---|
| `nexus/api/routers/engagement_mailboxes.py` | 03 | tenant, `run_engagement` |
| `nexus/api/routers/engagement_suppression.py` | 04 | tenant, `run_engagement` / `manage_engagement` to lift |
| `nexus/api/routers/unsubscribe.py` | 04 | public, signed token |
| `nexus/api/routers/engagement_settings.py` | 05 | tenant; consent `manage_workspace`, confidence `manage_engagement` |
| `nexus/api/routers/admin_ledger.py` | 06 | platform `providers.manage` |
| `nexus/api/routers/engagement_campaigns.py` | 08 | tenant, `run_engagement` |
| `nexus/api/routers/engagement_templates.py` | 08 | tenant, `run_engagement` |
| `nexus/api/routers/engagement_webhooks.py` | 09 | public, OIDC / clientState |
| `nexus/api/routers/engagement_desk.py` | 10 | tenant, `run_engagement`; team view `manage_engagement` |
| `nexus/api/routers/engagement_reports.py` | 12 | tenant, `run_engagement` |
| `nexus/api/routers/engagement_insights.py` | 13 | tenant, `run_engagement` |

### Worker jobs (added to `nexus/workers/tasks.py::HANDLERS`, scheduled in `nexus/workers/scheduler.py`)

| Job | Phase | Scheduled | Gate |
|---|---|---|---|
| `refresh_mailbox_tokens` | 03 | every tick | always (connections only) |
| `ship_ledger` | 06 | every tick | always (consented rows only) |
| `build_ledger_datasets` | 06 | hourly | always |
| `ledger_delete_workspace` | 06 | on opt-out | — |
| `ledger_outbox_retention` | 06 | daily | always |
| `advance_engagement` | 08 | every tick | `engagement_campaigns_enabled` |
| `sync_mailbox` | 09 | on notification | `engagement_campaigns_enabled` |
| `sync_mailboxes` | 09 | every 5 min | `engagement_campaigns_enabled` |
| `renew_mail_notifications` | 09 | hourly | `engagement_campaigns_enabled` |
| `engagement_reminders` | 10 | every tick | `engagement_campaigns_enabled` |

### Frontend

| File | Phase |
|---|---|
| `frontend/src/pages/settings/Mailboxes.tsx` (+ `.module.css`) | 03 |
| `frontend/src/pages/settings/DoNotContact.tsx` | 04 |
| `frontend/src/components/engagement/DncBadge.tsx` | 04 |
| `frontend/src/pages/settings/TrainingConsent.tsx`, `frontend/src/components/engagement/ConsentPrompt.tsx`, `LoginPage.tsx` consent line | 05 |
| `frontend/src/pages/admin/LedgerTab.tsx` | 06 |
| `frontend/src/pages/engagement/CampaignsPage.tsx`, `CampaignBuilder.tsx`, `ReviewQueue.tsx`, `LaunchPanel.tsx`, `CampaignDetailPage.tsx`, `SequenceTemplatesPage.tsx`, `ReplyDeskPage.tsx`, `frontend/src/components/engagement/ConversationTimeline.tsx`, `CreditEstimate.tsx`, `VolumeWarning.tsx`, `frontend/src/pages/settings/EngagementSettings.tsx` | 11 |
| `frontend/src/pages/engagement/ReportsPanel.tsx`, `frontend/src/components/engagement/TodayPlan.tsx`, `MailboxHealth.tsx` | 12 |
| `frontend/src/components/engagement/InsightBadge.tsx`, `BestTimeHint.tsx` | 13 |
| `frontend/src/lib/api.ts`, `frontend/src/lib/types.ts` | every UI phase |

### Tests

- Offline tests live flat in `tests/` as `tests/test_engagement_<unit>.py`. `tests/` is not a package and has no sub-directories; pytest's default import mode names a module by its basename, so every file name must be unique across the tree, and the `test_engagement_` prefix guarantees that.
- `tests_integration/test_engagement_claim.py` (08): two concurrent workers against real Postgres.
- `tests_live/engagement/` (03 onwards): real Gmail and Microsoft 365 test mailboxes, real Supabase test projects, real LLM.

---

## 5. Dependency map: consumers of today's engine and what each phase does to them

Mapped with code-review-graph (`importers_of nexus/campaigns/service.py`, `importers_of nexus/cadences/service.py`) and a code-reference grep at `79e7c18`. Nothing in this table changes before phase 15 except where a phase is named.

| Consumer | Reference today | Change | Phase | Pinned by |
|---|---|---|---|---|
| `nexus/api/routers/campaigns.py` | `CampaignService`, `CampaignTarget`, cadence service | Removed; routes re-served by `engagement_campaigns.py` | 15 | `tests/test_engagement_cutover_routes.py` |
| `nexus/api/routers/cadences.py` | `CadenceService`, enrollment ops | Removed; `engagement_templates.py` + enrollment ops in `engagement_campaigns.py` | 15 | same |
| `nexus/campaigns/service.py`, `schemas.py`, `__init__.py` | old engine | Removed | 15 | `test_old_engine_is_gone` |
| `nexus/campaigns/sourcing.py` | used by `routers/accounts.py:716`, `routers/agents.py:163` | Moved to `nexus/contacts/sourcing.py`; both imports updated | 15 | `tests/test_contact_sourcing.py` (import path updated) |
| `nexus/cadences/*` | old engine | Removed | 15 | `test_old_engine_is_gone` |
| `nexus/models/campaign.py`, `nexus/models/cadence.py` | ORM for old tables | **Kept** (tables are read-only history, D13); `Campaign` still backs `Outcome.campaign_id` FK | — | `test_migrations_replay.py` |
| `nexus/api/routers/outcomes.py` | validates `campaign_id` against `Campaign` | Also accepts `engagement_campaign_id`, stored in `meta` | 12 | `tests/test_engagement_outcome_attribution.py` |
| `nexus/workers/tasks.py` | `handle_run_campaign`, `handle_advance_cadences` | Removed in 15; engagement jobs added in 03/06/08/09/10 | 03–15 | `tests/test_engagement_worker_jobs.py` |
| `nexus/workers/scheduler.py` | `enqueue_advance_cadences` | Engagement jobs enqueued (gated); cadence enqueue removed in 15 | 03–15 | same |
| `nexus/calling/service.py`, `nexus/models/calling.py` | `cadence_enrollment_id` | `enqueue(..., engagement_enrollment_id=)`; call steps created by the engine | 08 | `tests/test_engagement_call_steps.py` |
| `nexus/ingestion/tiering.py::_in_active_cadence` | `CadenceEnrollment` active | Also true for an active/paused/snoozed `EngagementEnrollment`; old read removed in 15 | 08, 15 | `tests/test_refresh_tiering.py` + new case |
| `nexus/orchestration/tools.py::setup_cadence` | `get_cadence_service` | Creates a `SequenceTemplate` | 15 | `tests/test_engagement_orchestrator_setup_sequence.py` |
| `nexus/models/__init__.py` | registers old models | Registers new models (kept old) | 01 | `test_migrations_replay.py` |
| `nexus/runtime_config/catalog.py` | `cadence_enabled`, `cadence_batch_size`, `cadence_max_duration_days`, `campaign_sourced_min_send_confidence` | Removed from catalog in 15 (stored rows are skipped by rule); `campaign_sourcing_enabled` stays (used by contact sourcing); engagement settings added in 02 | 02, 15 | `tests/test_runtime_control_plane.py` updated |
| `nexus/core/config.py` | cadence fields | Engagement fields added (02); cadence fields removed (15) | 02, 15 | `tests/test_reacher_verifier.py:316` keeps sourcing defaults |
| `nexus/billing/catalog.py`, `rates.py`, `plans.py` | `outreach.campaign`, `outreach.cadence_touch` | `ai.reply_classify`, `ai.reply_draft` added with rate cards (08/09/10); old capabilities kept (history); module gates unchanged | 08–10 | `tests/test_credit_floor.py`, new rate tests |
| `nexus/alerts/rules.py` | `ALERT_CATEGORIES` from signal rules | Union with engagement categories | 09 | `tests/test_engagement_engagement_alerts.py`, `tests/test_alert_routing_api.py` |
| `nexus/api/routers/auth.py` (`/signup`, `/workspaces`), `nexus/auth/registration.py` (OTP verify) | tenant creation | Record consent row in the same transaction | 05 | `tests/test_engagement_signup_consent.py` |
| `nexus/people/store.py::forget_person`, `routers/contacts.py` delete | erasure | Delete engagement message bodies + ledger person data | 06, 07 | `tests/test_engagement_erasure.py` |
| Ledger emit seams: `agents/runtime.py::AgentRuntime.run`, `billing/meter.py::metered`, `orchestration/engine.py::execute_run`, `outcomes/service.py::record`, `calling/service.py::log_disposition`, `ingestion/service.py::ingest`, `agents/scoring.py` score write, `enrichment/waterfall.py::enrich_contact`, `enrichment/account.py::enrich`, `research/provider.py` via `agents/research.py`, `core/audit.py::record_audit` | — | One `ledger.emit()` each, inside the existing transaction | 05 | `tests/test_engagement_ledger_seams.py` |
| Frontend `App.tsx`, `app/nav.tsx`, `components/layout/AppShell.tsx`, `lib/api.ts`, `lib/types.ts`, `lib/display.ts` | old routes/types | New routes behind flag (11); old removed (15) | 11, 15 | `tests/test_plan_gated_nav.py`, `tests/test_engagement_engagement_ui.py` |
| `pages/CampaignsPage.tsx`, `pages/CadencesPage.tsx` | old pages | Replaced by `pages/engagement/*` | 11, 15 | same |
| `pages/CallsPage.tsx` | cadence enrollment link | Engagement enrollment link | 15 | `tests/test_engagement_engagement_ui.py` |
| `pages/ListsPage.tsx`, `pages/AccountsPage.tsx`, `pages/ContactsPage.tsx`, `components/discovery/ResultsPanel.tsx` | "launch campaign" / "add to cadence" | "Add to campaign" opens the new builder with the selection | 11, 15 | same |
| `pages/SettingsPage.tsx` | SMTP mailboxes | Adds Mailboxes (OAuth), Do-not-contact, engagement settings, training consent; SMTP mailboxes labelled "one-off sends only" | 03, 04, 05, 11 | `tests/test_engagement_engagement_settings_ui.py` |
| Tests referencing the old engine: `test_cadence_engine.py`, `test_campaign_engine.py`, `test_campaign_sourcing.py`, `test_cold_calling.py`, `test_launch_from_selection.py`, `test_continuous_automation.py`, `test_job_durability.py`, `test_runtime_config.py`, `test_runtime_control_plane.py`, `test_billing_metering_coverage.py`, `test_feature_switches.py`, `test_plan_gated_nav.py`, `test_refresh_tiering.py`, `test_admin_health.py`, `test_sdr_adoption.py`, `test_delete_export.py`, `test_lists_listing.py` | old engine | Each case replaced or retargeted, listed test-by-test in phase 15 | 15 | the suite itself |

---

## 6. External work the owner does (never Claude)

Delivered as numbered guided steps inside the task that needs them. Claude never creates these accounts or handles the credentials.

| When | What | Guide |
|---|---|---|
| Before phase 03 live tests | Google Cloud project, OAuth consent screen (external, test users), Gmail scopes, OAuth web client, Pub/Sub topic + push subscription; start Gmail verification + CASA | `docs/engagement/setup-google.md` (02, Task 9) |
| Before phase 03 live tests | Azure app registration, redirect URI, Graph delegated permissions, client secret; start publisher verification | `docs/engagement/setup-microsoft.md` (02, Task 10) |
| Before phase 03 live tests | One Gmail and one Microsoft 365 test mailbox; refresh tokens stored as CI secrets via `scripts/engagement_live_token.py` | `docs/engagement/live-tests.md` (03, Task 14) |
| Before phase 06 live tests | Three Supabase projects (archive, training, insights), one role per store | `docs/engagement/setup-supabase.md` (06, Task 1) |
| Before phase 15 | Legal review of the pre-selected consent box for EU customers (spec §16) | — |

---

## 7. Definition of done for the whole project

- Every requirement in spec §1–§19 is covered by a task (coverage table at the end of each phase plan).
- `pytest` full suite, `tests_integration`, and `tests_live/engagement` pass on the release commit.
- `migrate_engagement.py --dry-run` report reviewed with the owner, real run verified by counts.
- Every flow in spec §9 exercised end to end against the real test mailboxes before the flag is switched on.
