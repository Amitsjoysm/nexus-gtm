# Sub-project C — Channel & Cadence (Multi-touch Email Cadences)

**Date:** 2026-06-09
**Status:** Approved design
**Improvement:** #6 (Channel & Cadence)
**Depends on:** Sub-project A (Segment Campaign Engine), Sub-project B (Contact Sourcing + Real Email Verification)

## Problem

Campaigns today send a **single email per target**. Real outbound is a *cadence*: several
touches spaced over days, each with a distinct angle, that stops the moment the prospect
replies or the address bounces. NEXUS owns no multi-touch state — the "sequence" string is
just a label handed to an external SEP. This sub-project makes NEXUS the native owner of
multi-touch email cadences, with full automation plus per-need human control.

## Goals

- A reusable **Cadence** definition: an ordered list of steps, each with a delay and an angle.
- Enroll a campaign's approved targets into a cadence; drive touches autonomously over time.
- **Stop** on: reply (via `Outcome`), undeliverable/bounce, manual pause/stop, max-touches or
  duration cap.
- **Approve-once-then-auto-run**, with an opt-in per-campaign `review_each_touch` manual gate.
- Built for scale (designed for a million users): DB-as-queue claiming, horizontal workers,
  bounded batches, structural idempotency.
- Stay **offline/green, zero-network, email-only** in v1.

## Non-goals (v1)

- Non-email channels (LinkedIn, SMS, calls). Schema reserves a `channel` field but guards it
  to `"email"`.
- Inbound email ingestion / reply parsing. "Stop on reply" reuses the existing `Outcome` model
  (set manually or by CRM sync).
- Cadence/campaign **frontend UI** (built later with the `impeccable` skill).
- A/B testing of steps, branching cadences, send-time optimization.

---

## Architecture overview

DB is the durable source of truth. Each `CadenceEnrollment` carries a `next_touch_at` and a
composite index `(status, next_touch_at)`. A periodic **advance tick** (`handle_advance_cadences`)
claims a bounded batch of *due* enrollments and processes each: stop-check → per-step AI compose
(angle threaded) → idempotent touch insert → send-policy/grounded gate → send or park → advance.

Why a tick (not per-touch timers): the existing queue (`nexus/workers/queue.py`) has **no native
delayed/scheduled execution** (`InMemoryTaskQueue` / `RedisTaskQueue` only do immediate
enqueue/dequeue). A periodic due-scan is the scalable, crash-safe alternative.

### Scale & concurrency (the claim query)

Production (Postgres) — DB-as-queue, N workers claim disjoint batches:

```sql
SELECT ... FROM cadence_enrollments
WHERE status = 'active' AND next_touch_at <= :now
ORDER BY next_touch_at
LIMIT :batch
FOR UPDATE SKIP LOCKED
```

`FOR UPDATE SKIP LOCKED` lets workers scale horizontally with no double-claim; `LIMIT` is
built-in back-pressure. Offline (SQLite) uses a simpler single-worker `UPDATE … RETURNING`
claim (no `SKIP LOCKED`), mirroring the existing `InMemoryTaskQueue` vs `RedisTaskQueue` split.

The claim **scan is global** (the scheduler is infra, not a tenant user), but each claimed
enrollment is processed **inside `tenant_session(tenant_id)`** so all compose/send reads and
writes obey RLS. A cadence never sees another tenant's data.

### Time via injectable clock

All "now" comparisons and `next_touch_at = now + delay_days` go through a clock dependency.
Production uses wall-clock; tests inject a fake clock and advance days in milliseconds — a
multi-week cadence is exercised with **zero `sleep`**.

---

## Data model (`nexus/models/cadence.py`)

All tables tenant-scoped (`TenantScoped`, RLS). New models:

### `Cadence`
- `id`, `tenant_id`
- `name: str(200)`, `description: str | None`
- `is_active: bool = True` (soft-disable; off keeps definition but blocks new enrollments)
- `created_by_user_id: str | None` (FK users, nullable)

### `CadenceStep`
- `id`, `tenant_id`
- `cadence_id` (FK cadences, indexed)
- `step_index: int` — 0-based, contiguous
- `delay_days: int` — wait *before* this step fires (step 0 may be 0 = send immediately on enroll)
- `angle: str(Text)` — the per-touch compose angle threaded into `research_compose`
- `channel: str(16) = "email"` — v1 guard: only `"email"` accepted
- Unique `(cadence_id, step_index)`

### `CadenceEnrollment`
- `id`, `tenant_id`
- `campaign_id` (FK campaigns, indexed), `campaign_target_id` (FK campaign_targets)
- `account_id` (FK accounts), `contact_id: str | None` (FK contacts)
- `cadence_id` (FK cadences)
- `current_step_index: int = 0`
- `status: str(16)` — `active | paused | completed | stopped`
- `stop_reason: str(16) | None` — `replied | undeliverable | manual | max_touches | null`
- `next_touch_at: datetime` — when the current step becomes due
- `started_at: datetime`, `completed_at: datetime | None`
- Composite index `(status, next_touch_at)` — the claim query's index

Status constants: `ENROLL_ACTIVE/PAUSED/COMPLETED/STOPPED`; `ENROLL_TERMINAL = {COMPLETED, STOPPED}`.
Stop reasons: `STOP_REPLIED/UNDELIVERABLE/MANUAL/MAX_TOUCHES`.

### `CadenceTouch`
- `id`, `tenant_id`
- `enrollment_id` (FK enrollments, indexed)
- `step_index: int`
- `run_id: str | None` — the research_compose run that produced this touch's draft
- `status: str(20)` — `sent | skipped | failed | awaiting_approval`
- `skip_reason: str | None`
- `draft: JSON` — snapshot off the run blackboard (survives the run; approval UI + audit)
- `sent_at: datetime | None`, `error: Text | None`
- **Unique `(enrollment_id, step_index)`** — structural idempotency: a step is touched once.

Touch status constants: `TOUCH_SENT/SKIPPED/FAILED/AWAITING_APPROVAL`.

### `Campaign` additions (`nexus/models/campaign.py`)
- `cadence_id: str | None` (FK cadences, nullable) — **NULL = backward-compatible single-touch path**
- `review_each_touch: bool = False` — opt-in per-touch manual approval gate

When `cadence_id` is NULL, `approve_and_send` runs the existing single-send path unchanged, so
all current `test_campaign_engine.py` behavior stays green.

---

## Per-touch flow

`CampaignService.approve_and_send` (extended): for each `DRAFTED` target, if the campaign has a
`cadence_id`, create a `CadenceEnrollment` (`status=active`, `current_step_index=0`,
`next_touch_at = clock.now() + step0.delay_days`, `started_at=now`) instead of single-sending.
If `cadence_id` is NULL, single-send as today.

The advance tick, per claimed due enrollment (all inside `tenant_session`):

1. **Stop-check first** (cheap, indexed, pre-compose) — see Stop conditions.
2. **Load the current step** (live read of `CadenceStep` at `current_step_index`).
3. **Compose** — run grounded `research_compose` recipe with the step's `angle` threaded into
   inputs (same inputs-threading pattern B used for `contact_id`). Reuses research → score →
   message with the grounded-send gate.
4. **Insert idempotent `CadenceTouch`** `(enrollment_id, step_index)`. Unique violation ⇒ this
   step already handled ⇒ skip insert, treat as done (crash-safety).
5. **Apply send policy** — B's `_send_policy(draft, campaign)` (invalid→undeliverable stop,
   risky→hold unless `send_risky`, sourced-unverified→skip) **and** the grounded-send gate in
   `SendMessageTool`.
6. **Send or park** — if `review_each_touch`, set touch `awaiting_approval` (no send) and **do
   not advance**; else push via `get_sep_connector().push_contact`, set touch `sent`,
   `sent_at=now`, record `Outcome("sent")`.
7. **Advance** — `current_step_index += 1`; if more steps,
   `next_touch_at = clock.now() + next_step.delay_days`; else `status=completed`,
   `completed_at=now`.

---

## Stop conditions

Evaluated before composing (cheap) and at send (deliverability). All map onto existing infra —
no inbound-email machinery.

1. **Reply / positive outcome** — before composing, query `Outcome` for the enrollment's
   `(account_id, contact_id)` since `started_at`; any stage in `{replied, meeting, won}` ⇒
   `stop(replied)`. Reuses the `Outcome` model.
2. **Undeliverable / bounce** — at send, `_send_policy` + `SendMessageTool` refuse
   `STATUS_INVALID`; the enrollment goes `stop(undeliverable)` (a hard-invalid address won't
   self-heal on touch 3).
3. **Manual pause / stop** — `pause` ⇒ `status=paused` (claim query's `status='active'` never
   selects it); `resume` ⇒ `status=active`, recompute `next_touch_at` from clock; `stop` ⇒
   terminal `stopped(manual)`. Pause reversible, stop not.
4. **Max touches / duration cap** — steps exhausted ⇒ `completed` (natural finish). Independently,
   `clock.now() - started_at > cadence_max_duration_days` ⇒ `stop(max_touches)` even mid-sequence
   (safety bound against a misconfigured long delay).

The stop-check runs **first** in the per-enrollment work function, so a replied/paused/expired
enrollment costs one indexed query and never touches the LLM or SEP.

---

## Control / API surface

All routes tenant-scoped + RBAC-gated.

### Cadence definitions (`/cadences`) — manager+ write, rep+ read
- `POST /cadences` — create with ordered steps `[{delay_days, angle, channel:"email"}]`.
  Validates: ≥1 step, contiguous 0-based indices, `delay_days >= 0`, `channel == "email"`.
- `GET /cadences` / `GET /cadences/{id}` — list / detail with steps.
- `PATCH /cadences/{id}` — edit name/description/steps/`is_active`. Step edits affect only
  *future* enrollments and future touches of live ones (we read the step row live each tick;
  `current_step_index` is preserved).
- `DELETE /cadences/{id}` — soft (`is_active=false`) if referenced by any enrollment; hard only
  when unused.

### Campaign wiring
- `POST /campaigns` gains optional `cadence_id` + `review_each_touch`. Set ⇒ `approve_and_send`
  enrolls DRAFTED targets; NULL ⇒ existing single-touch path.

### Enrollment control (`/campaigns/{id}/enrollments`, `/enrollments/{id}`) — manager+
- `GET /campaigns/{id}/enrollments` — list: status, `current_step_index`, `next_touch_at`,
  `stop_reason`.
- `POST /enrollments/{id}/pause | /resume | /stop`.
- `GET /enrollments/{id}` — detail incl. `CadenceTouch` history (angle, sent/skipped/failed, run_id).

### Review-each-touch approval (opt-in)
- Touches land `awaiting_approval` (draft staged on the touch).
- `POST /enrollments/{id}/touches/{step_index}/approve` — optional edited draft body; sends +
  advances.
- `POST /enrollments/{id}/touches/{step_index}/reject` — skips the touch + advances; `stop=true`
  param stops the enrollment instead.

### Progress report
- `GET /campaigns/{id}/cadence-report` — enrollments by status, touches sent per step,
  stop-reason histogram, reply count. Mirrors the existing campaign `report` JSON shape so the
  future dashboard (sub-project E) consumes both uniformly.

---

## Worker, scheduler & config

### `handle_advance_cadences` (`nexus/workers/tasks.py`)
Runs on an interval. Each tick: claim a bounded batch of due enrollments (claim query above),
then per enrollment — stop-check → compose with angle → idempotent touch → policy/grounded gate
→ send or park → advance + recompute `next_touch_at`. Scan global, work tenant-scoped.

### Config (`NEXUS_`-prefixed, `nexus/core/config.py`)
- `cadence_enabled: bool = False` — master switch (safe opt-in, like `campaign_sourcing_enabled`).
- `cadence_tick_interval_s: int = 60` — production due-scan cadence.
- `cadence_batch_size: int = 100` — max enrollments claimed per tick per worker (back-pressure).
- `cadence_max_duration_days: int = 30` — the duration-cap safety bound.

---

## Error handling & failure isolation

1. **Per-enrollment isolation** — each enrollment processed in try/except; a failure records
   `CadenceTouch.status="failed"` + error, leaves the enrollment `active`, does **not** advance.
   Next tick retries. One poisoned enrollment never sinks the batch.
2. **Structural idempotency** — unique `(enrollment_id, step_index)` ⇒ a retried/double-claimed
   touch collides on insert, caught as "already handled." A crash *after* send but *before*
   advancing cannot double-send: next tick collides on the same key and advances without
   re-sending. Same guarantee B relies on.
3. **Crash-safe reclaim** — claiming flips a row inside a transaction holding `FOR UPDATE SKIP
   LOCKED`; a worker that dies releases its locks on connection drop, rows return to
   `active, next_touch_at <= now`, reclaimed next tick. No stuck in-flight state, no reaper.
4. **Bounded compose** — same one-shot grounding contract as B; ungrounded ⇒ touch `skipped`
   (reason), enrollment advances (a missing touch 2 shouldn't kill the sequence). Repeated
   failure is visible in touch history, not retried forever.

---

## Testing plan (offline, zero-network, green)

- **Model/migration** — four tables + Campaign columns create cleanly; uniques
  `(cadence_id, step_index)` and `(enrollment_id, step_index)` enforced; composite index
  `(status, next_touch_at)` present.
- **Happy path** — 3-step cadence, fake clock. Enroll → tick → touch 0 sends, `next_touch_at`
  advances by `delay_days`; advance clock → touch 1 → touch 2 → `completed`. Assert exact
  `CadenceTouch` rows + `Outcome("sent")` per touch.
- **Stop conditions (one test each)** — reply Outcome before touch 2 ⇒ `stopped(replied)`,
  touch 2 never composed; invalid email at send ⇒ `stopped(undeliverable)`; `pause` ⇒ tick
  skips, `resume` ⇒ resumes; duration cap exceeded ⇒ `stopped(max_touches)`.
- **Idempotency** — same due enrollment processed twice (simulated double-claim) ⇒ one touch,
  one send.
- **review_each_touch** — touch lands `awaiting_approval`, no send; `approve` sends + advances;
  `reject(stop=true)` stops.
- **Tenant isolation** — two tenants with parallel enrollments; a global tick processes both,
  each touch's reads/writes stay within tenant (no cross-tenant contact/draft leakage).
- **Backward-compat** — campaign with `cadence_id=NULL` still single-sends; existing
  `test_campaign_engine.py` stays green.
- **Offline claim path** — SQLite `UPDATE … RETURNING` claim selects due, skips paused/completed.

---

## Migration

New Alembic revision (next is `0007`): create `cadences`, `cadence_steps`,
`cadence_enrollments`, `cadence_touches` with their indexes/uniques; add `campaigns.cadence_id`
(FK, nullable) + `campaigns.review_each_touch` (bool, default false).

## Files

- Create: `nexus/models/cadence.py`, `nexus/campaigns/cadence_service.py` (or extend
  `nexus/campaigns/service.py`), cadence schemas + router (`nexus/api/...`), Alembic `0007_*`.
- Modify: `nexus/models/campaign.py` (+2 columns), `nexus/workers/tasks.py`
  (`handle_advance_cadences` + dispatch), `nexus/core/config.py` (4 settings),
  `nexus/campaigns/service.py` (`approve_and_send` enroll branch).
- Test: `tests/test_cadence_engine.py` (new), reconcile `tests/test_campaign_engine.py` if needed.
