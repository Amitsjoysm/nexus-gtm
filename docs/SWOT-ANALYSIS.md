# NEXUS GTM — SWOT Analysis & Resolution Log

Reviewed flow-by-flow from two seats: **Solution Architect** (can this run reliably for
thousands of teams?) and **Senior SDR** (would I actually live in this every day?). Every
weakness below is either **RESOLVED** (with the shipped change) or carries an explicit
mitigation and owner-decision note. Date: 2026-06-11.

---

## 1. Signal → Inbox triage (the rep's daily driver)

**Use case:** An SDR opens NEXUS at 8am, works the prioritized queue: see why an account
is hot, open it, act, complete, next.

- **Strengths:** Priority-ranked queue generated from real signals; triage strip shows
  recency, deliverability, and research-readiness; four-state UI (no dead spinners).
- **Weaknesses → resolutions:**
  - *Tasks could rot silently — no aging visible.* **RESOLVED:** SLA chips ("In queue 2d",
    warning ≥24h, danger ≥72h) from `age_hours` now on every task.
  - *Mouse-only triage is too slow for 50+ tasks/day.* **RESOLVED:** keyboard-first triage
    (J/K move, E complete, D/Enter open account) with visible selection and form-field
    guards.
  - *A task was a dead end — no path to the account.* **RESOLVED:** "Open account" deep
    link on every task with an account.
- **Opportunities:** snooze/assign actions; bulk-complete; SLA breach feeding the manager
  digest.
- **Threats:** queue bloat if plays over-fire — mitigated by play trigger thresholds and
  priority decay; watch tasks-per-rep-per-day in analytics.

## 2. Live dashboard + activity feed

**Use case:** A manager keeps NEXUS on a second monitor; the feed is the team's pulse.

- **Strengths:** visibility-aware polling (zero hidden-tab cost, jittered fleet-wide),
  bounded single-round-trip aggregates, manager-gated cross-entity feed.
- **Weaknesses → resolutions:**
  - *The feed was observational — you could see, not act.* **RESOLVED:** feed rows with an
    account deep-link straight into the Account 360 (signal → action in one move).
  - *Nothing reached reps outside the app.* **RESOLVED:** opt-in browser notifications for
    hot signals (strictly opt-in, baseline-on-first-load so old signals never replay,
    burst-capped) + the daily digest (below).
- **Opportunities:** feed filters (per rep / per list); SSE push when scale justifies it.
- **Threats:** notification fatigue → only `strength ≥ 0.66` signals notify, capped at 3
  per poll, and the bell toggles off in one click.

## 3. Daily digest (retention loop)

**Use case:** A rep who didn't open NEXUS yesterday gets one email: "12 new signals, 5
tasks waiting" — and comes back.

- **Strengths:** rides the automation heartbeat; idempotent per interval; quiet
  workspaces are skipped (no empty digests teaching reps to ignore it); per-tenant opt-in.
- **Weaknesses → resolutions:**
  - *No outbound channel existed at all.* **RESOLVED:** digest lands as an email-channel
    Alert (`source="digest"`), the same fan-out seam the alert system already defines.
  - *SMTP delivery is not yet wired.* **Mitigation (deliberate):** the Alert email channel
    is the single integration point; plugging an SMTP/SES sender there requires no product
    change. Until then digests are visible in-app under Alerts.
- **Opportunities:** per-user digest preferences; manager weekly rollup with SLA breaches
  and campaign ROI.
- **Threats:** digest content going stale/generic — content is computed from the tenant's
  actual last-24h activity, never templated filler.

## 4. Account 360 + CRM trust

**Use case:** Before a call, the rep checks the account: firmographics, signals timeline,
research, and *whether what they see matches the CRM*.

- **Strengths:** one-page account context; agent actions (research/draft) in place;
  manual + automatic CRM push paths.
- **Weaknesses → resolutions:**
  - *Reps couldn't tell if CRM data was fresh — the #1 trust killer in SDR tooling.*
    **RESOLVED:** trust chip in the header — "Synced to Salesforce · 2m ago" (green) /
    "From Salesforce · not pushed yet" (neutral) — driven by `crm_source`/`crm_synced_at`
    now on every account payload.
  - *Change-aware sync used to re-push everything* (perf pass G fixed the perpetual-due
    bug) — sync timestamps shown to reps are now meaningful.
- **Opportunities:** field-level sync provenance; conflict surfacing (CRM changed vs
  NEXUS enriched).
- **Threats:** a real CRM API outage would silently stall syncs → `/crm/sync-status`
  pending count is the alarm; surface it on Settings (done) and alert on growth (future).

## 5. Campaigns + cadences (outbound engine)

**Use case:** A manager drafts AI outreach over a saved list, spot-checks the approval
sample, approves once, follows live progress; follow-ups run on a cadence with stop-on-reply.

- **Strengths:** human approval gate before anything sends; per-campaign `send_risky`
  opt-in; grounded-draft policy with skip reasons; live SSE progress; cadence stop
  conditions (reply/undeliverable/manual/cap).
- **Weaknesses → resolutions:**
  - *No ROI story: replies/meetings/wins floated free of campaigns — the #1 question a
    sales leader asks ("what did this campaign produce?") was unanswerable.* **RESOLVED:**
    `outcomes.campaign_id` attribution (migration 0011), per-campaign rollup on the
    campaign detail (`outcomes: {replied: n, meeting: n, won: n}`), and Results chips in
    the campaign panel. The cadence engine already stops enrollments on replied outcomes,
    so attribution and stop-on-reply now share one source of truth.
  - *Reply capture is manual (Log outcome) or API-driven.* **Mitigation (deliberate):**
    automatic reply detection requires mailbox/ESP webhooks — an integration, not a
    product gap; the attribution path it would feed is now built and tested.
- **Opportunities:** ESP webhook ingestion → automatic replied outcomes; A/B sequence
  comparison off the same rollup.
- **Threats:** deliverability damage from over-sending — mitigated by verification
  waterfall, risky-skip default, per-touch review option, and the approval gate.

## 6. Multi-tenancy, security, operations (architect's seat)

- **Strengths:** TenantSession + Postgres RLS on every endpoint; RBAC everywhere; JWT
  secret rejected in prod if default; offline-deterministic test suite (318 tests); EventBus
  + queue seams already abstracted.
- **Weaknesses → resolutions:**
  - *API and worker each had an in-memory queue — in a two-container deployment, API-enqueued
    jobs would never run.* **RESOLVED in deployment:** the stack pins
    `NEXUS_QUEUE_BACKEND=redis` + Valkey, so API and worker share one durable queue (the
    `RedisTaskQueue` was already implemented; the compose file makes it the production
    default).
  - *No production packaging existed.* **RESOLVED:** `deploy/` — multi-stage Dockerfile,
    compose stack (Postgres 16 + Valkey 8 + app + worker + Caddy), domain-parameterized
    TLS, one-script `deploy.sh` (secret generation, migrations, health gate), release-zip
    packaging, ops runbook.
  - *No rate limiting / abuse controls.* **Mitigation:** Caddy fronts everything (per-IP
    limits addable in one Caddyfile block); auth endpoints are bcrypt-hashed; flag for a
    dedicated pass before public self-serve signup.
- **Opportunities:** managed Postgres/Valkey swap via two env vars; horizontal worker
  scaling (`--scale worker=N`) is already safe.
- **Threats:** single-VM blast radius — runbook documents backup/restore; the same
  artifacts split to multi-host without code change.

## 7. Onboarding & adoption (mass-adoption lens)

- **Strengths:** signup → seeded demo account → scored pipeline in under a minute;
  activation checklist; stub LLM means a pilot needs zero API keys.
- **Weaknesses → resolutions:**
  - *Day-2 return loop was missing.* **RESOLVED:** digest + hot-signal notifications.
  - *Value was visible but not actionable fast enough.* **RESOLVED:** actionable feed +
    keyboard triage + deep links shrink signal-to-action to seconds.
- **Opportunities:** CSV import surfacing during onboarding; team-invite nudge after first
  won outcome; public template gallery for plays/cadences.
- **Threats:** churn if the CRM connector stays stubbed in real pilots — prioritize the
  Salesforce adapter behind the existing connector interface.

---

## Verdict

The platform now closes the three loops that decide real-world SDR adoption: **act** (feed
→ account → outreach in one move, keyboard-speed triage), **trust** (CRM sync recency on
the record, approval gates, grounded drafts), and **return** (digest + hot-signal
notifications). The remaining open items are integrations (SMTP sender, real CRM/ESP
adapters) that plug into seams that are already built, tested, and documented — not
product gaps.
