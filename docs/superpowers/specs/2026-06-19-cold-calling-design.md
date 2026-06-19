# Sub-project G: AI Cold Calling — Design Spec

**Date:** 2026-06-19
**Status:** Approved (design) — pending implementation plan
**Author:** NEXUS engineering (with the user as product owner)

## Goal

Give cold-calling SDRs a first-class workflow in NEXUS — AI-generated call scripts, a
priority call queue, one-tap outcome logging, and "call" steps inside multi-touch cadences —
**without changing any existing email behavior**. Architect it so real telephony (tier 2) and
an autonomous AI voice agent (tier 3) plug in later behind a provider interface, with no rework.

## Scope

**v1 (this spec) — AI cold-calling workflow, no telephony:**
- AI call-script generation per contact (opener, signal hook, value prop, discovery questions,
  objection handling, CTA, voicemail), grounded in the same research/signals the email composer uses.
- A priority-ranked **call queue** ("power list") with one-tap dispositions and follow-up actions.
- **Call dispositions** logged as activities; they advance cadences and feed analytics.
- Multi-channel cadences: unlock `channel="call"` steps (a call step creates a call task instead
  of sending an email).
- Dialing is **click-to-dial** (`tel:` link) + manual outcome logging — zero new infrastructure,
  fully offline-capable.

**Designed-for-future (interfaces only in v1, no implementation):**
- **Tier 2 — Telephony:** `CallProvider` interface for click-to-call, recording, transcription,
  and post-call AI summary/coaching. Opt-in behind `NEXUS_TELEPHONY_PROVIDER` (default `stub`).
- **Tier 3 — Autonomous AI voice agent:** a `VoiceAgentProvider` variant that places and conducts
  calls. Same boundary; added later.

**Out of scope (v1):** real phone calls, recordings, transcription, autonomous dialing, SMS.

## Non-breaking guarantee

- All new tables and columns are additive; **no existing table is modified destructively**.
- `CadenceStep.channel` already exists (`default="email"`); email-only cadences are byte-for-byte
  unchanged. Call steps are only created when a cadence explicitly sets `channel="call"`.
- New endpoints are additive and RBAC-gated; no existing endpoint changes shape.
- The whole feature is offline-safe (stub provider), so CI stays zero-network and green.

## Architecture

Cold calling reuses the existing rails: **agents** (new `call_script` agent), **orchestration
tools** (new `CallScriptTool`), **cadence engine** (new `call` channel handler), **inbox-style
prioritization**, **providers** (new `CallProvider` seam), **analytics/activity feed**, and the
**TenantSession + RBAC** spine. The only genuinely new surface is the call queue + dispositions.

### Data model (`nexus/models/calling.py`, new — one Alembic migration)

```
CallTask            # a queued call (the SDR's power list)
  id, tenant_id, contact_id, account_id
  reason            # why this call surfaced (signal/cadence/manual)
  priority          # int 0-100, ranked like the Inbox
  status            # "open" | "done" | "skipped"
  source            # "manual" | "cadence" | "play"
  owner_user_id     # nullable (assignment)
  due_at            # nullable (callbacks / cadence timing)
  cadence_enrollment_id, cadence_step_index   # nullable cadence linkage
  script_cache      # nullable JSON: last generated script (avoid re-calling the LLM)
  created_at, updated_at

CallActivity        # a logged dial attempt + outcome (one task -> many activities)
  id, tenant_id, call_task_id, contact_id, account_id
  disposition       # connected | voicemail | no_answer | callback | meeting_booked
                    #   | not_interested | bad_number | gatekeeper
  notes             # free text
  duration_s        # nullable int
  next_step         # nullable free text
  occurred_at, created_at
  # Tier-2-ready (nullable, unused in v1): recording_url, transcript, ai_summary, sentiment, provider_call_id
```

Disposition constants live in the model module (like cadence's `STOP_*` / outcome constants).

### Config (`nexus/core/config.py`, additive)

```
calling_enabled: bool = True            # workflow available (offline-safe; gate cadence call-steps on this)
telephony_provider: str = "stub"        # stub | twilio | ...  (tier 2)
telephony_from_number: str = ""         # caller ID (tier 2)
call_queue_default_limit: int = 50
```

### Provider seam (`nexus/calling/provider.py`, new)

```python
class CallProvider(ABC):
    name: str
    async def place_call(self, *, to: str, from_: str, context: dict) -> CallHandle: ...
    async def get_recording(self, provider_call_id: str) -> str | None: ...
    async def get_transcript(self, provider_call_id: str) -> str | None: ...

class StubCallProvider(CallProvider):   # v1 default: no network; click-to-dial + manual logging
    name = "stub"
    # place_call returns a CallHandle marking "manual" mode; recording/transcript -> None

def build_call_provider(name: str) -> CallProvider:   # stub now; twilio later
    ...
```

This mirrors `build_email_verifier` / `build_crm_connector_from_settings` exactly. Tier 2 adds
`TwilioCallProvider`; tier 3 adds a `VoiceAgentProvider`. **No interface change needed later.**

### AI call-script agent

- New `call_script` agent (`nexus/agents/call_script.py`) + `CallScriptTool`
  (`nexus/orchestration/tools.py`), registered like `messaging` / `compose_message`.
- Grounded in the account's research brief + recent signals + ICP + the contact's title/seniority.
- Output: structured JSON — `opener`, `hook`, `value_prop`, `discovery_questions[]`,
  `objections[] {objection, response}`, `cta`, `voicemail`.
- Offline (stub LLM): deterministic template, so CI is zero-network. Uses the existing LLM chain
  (now with Groq key rotation), so it benefits from the fallback + rotation automatically.

### Service (`nexus/calling/service.py`, new)

- `CallQueueService`:
  - `enqueue(ts, contact, *, reason, source, priority, due_at=None, cadence_*=None) -> CallTask`
    (idempotent: at most one OPEN task per (contact, cadence_step) — mirrors the inbox dedupe fix).
  - `list_queue(ts, *, owner=None, status="open", limit)` — priority-ranked.
  - `generate_script(ts, call_task) -> dict` — calls the agent, caches on the task.
  - `log_disposition(ts, call_task_id, disposition, notes, duration_s, next_step) -> CallActivity`
    — closes the task (or re-queues on callback/no_answer), creates the follow-up, advances any
    linked cadence, and publishes an activity event for analytics.

### Cadence integration

- In the advance tick (`nexus/cadences/service.py`), a step with `channel="call"` **creates a
  `CallTask`** (source=`cadence`, linked to the enrollment+step) instead of composing/sending an
  email. The touch is recorded as usual (idempotent per step). Logging a terminal disposition on
  that task advances the enrollment to the next step.
- The `channel="email"` path is untouched. `CadenceService.create` is relaxed to also accept
  `"call"` (currently guarded to email-only).

### API (`nexus/api/routers/calling.py`, new — all tenant-scoped + RBAC `manage_accounts`)

```
GET  /calling/queue?status=&owner=mine&limit=     -> list[CallTaskOut] (with contact/account context)
POST /calling/tasks                               -> create a manual call task (contact_id, reason)
POST /calling/tasks/{id}/script                   -> generate/return the AI script (cached)
POST /calling/tasks/{id}/disposition              -> log outcome (-> CallActivity, advances cadence)
POST /calling/tasks/{id}/skip                     -> skip
GET  /calling/contacts/{contact_id}/activities    -> call history for a contact
```

### Frontend (`frontend/src/pages/CallsPage.tsx`, new + small additions)

- **Calls page**: priority-ranked queue (reuses `DataTable` with the new sortable headers) + a
  focused call panel showing the AI script, one-tap **disposition buttons**, a notes field, and
  one-tap follow-ups (book meeting / schedule callback / send follow-up email — reusing existing
  actions). Click-to-dial via `tel:` link.
- **Account/Contact**: a "Call" action that enqueues a manual call task.
- **Cadences**: show a call-step badge; **Settings**: telephony status (stub / opt-in) read-only in v1.
- Nav item + route + role guard. Loading/empty/error states per the design system.

### Analytics

- Call dispositions publish to the existing **activity feed** and **manager dashboard**: calls
  made, connect rate, meetings booked. Additive metrics; existing analytics untouched.

### RBAC / tenancy / safety

- Every endpoint is `TenantSession`-scoped and gated on `manage_accounts` (same as accounts/contacts).
- Telephony/recording is **off by default**; when a real `CallProvider` is enabled, compliance
  hooks (recording-disclosure, AI-disclosure for tier 3, DNC list + quiet-hours guard) live inside
  the provider boundary so they can't be bypassed.

## Orchestrator-driven cadence setup & decisions (added 2026-06-19)

The conversational orchestrator must be able to set up and run cadences — including call steps —
and make channel decisions automatically, not just via the UI.

- New orchestration tools (`nexus/orchestration/tools.py`), registered like the existing ones:
  - `setup_cadence` — create a multi-touch cadence from a high-level brief (e.g. "3-touch:
    email, call day 2, email day 4"); the tool picks sensible channels/delays when unspecified.
  - `enroll_cadence` — enroll an account/contact (or a list/segment) into a cadence.
- The orchestrator can therefore answer "set up a cold-calling cadence for my hot accounts and
  enroll them" end-to-end. Decisions (channel mix, who to call first) reuse account scores +
  signals already in context. All gated by RBAC (`manage_campaigns`/`manage_accounts`) and
  tenant-scoped; outbound still respects the existing approval gate (autonomy = gated by default),
  so the orchestrator can *set up and queue* but human approval still governs actual sends.
- Existing orchestrator tools/recipes are untouched; these are additive tool registrations.

## Phone numbers in the Contacts list (added 2026-06-19)

Numbers from data platforms (InfoJoy today; future providers) must surface so SDRs can call.

- Enrichment already writes `contact.phone`/`phone_confidence` (web-search provider); the InfoJoy
  / future data-source providers populate the same fields via the existing `DataSourceRegistry`
  enrichment waterfall — **no new field needed**, just ensure providers map their phone into it.
- `WorkspaceContactOut` (and the Contacts page) currently omit phone — **add `phone` /
  `phone_confidence`** to the schema, the `/contacts` response, and a sortable "Phone" column.
- The call queue uses `contact.phone` for click-to-dial; a contact with no number still queues
  (the SDR can add one) but is flagged "no number."

## Testing (offline, zero-network)

- `call_script` agent: deterministic stub output shape.
- `CallQueueService`: enqueue idempotency, priority ordering, disposition → activity + task close,
  callback re-queue, cadence advance on a call step.
- Cadence: a `channel="call"` step creates a `CallTask` and does **not** send email; email steps
  unchanged (regression).
- API: tenant isolation + RBAC on every new endpoint.
- Migration applies cleanly; full suite stays green.

## Rollout

- One Alembic migration (additive). `calling_enabled=True` by default (offline-safe).
- Telephony stays `stub` until a deployment sets `NEXUS_TELEPHONY_PROVIDER` — no infra needed for v1.
- Ships as one sub-project (spec → plan → task-by-task build), deployed and verified like prior
  sub-projects.
```
