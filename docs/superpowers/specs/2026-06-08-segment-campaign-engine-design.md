# Segment Campaign Engine — Design Spec

**Date:** 2026-06-08
**Sub-project:** A (of a 5 sub-project sequence covering 6 capability improvements)
**Status:** Approved design — ready for implementation planning

---

## Context

NEXUS GTM today can discover accounts, research/score them, and compose grounded
outreach drafts — but only one account at a time, driven inline from an HTTP request,
parked at a per-send approval gate. There is no way to point the system at a *segment*
and say "research everyone, draft outreach for everyone, let me approve once, then send."

This sub-project (#1 of the 6 improvements) builds the **Segment Campaign Engine**: the
autonomous spine that runs discover→research→outreach across a saved segment with a
single campaign-level approval. It is built first because the other sub-projects extend
it: B (Contact Sourcing) consumes its skip report, C (Channel & Cadence) extends its
send phase, D (Continuous Automation) schedules it, E (Live Dashboard) consumes its
events. #5 (CRM auto-sync) is a later cycle; this v1 is **email-only, no CRM**.

### v1 scope (locked)

- **Segment = a saved List.** Reuse the existing ListBuilder; discovery results can be
  saved as a List first, then targeted.
- **Campaign-level approval.** Human approves once (segment + ICP + a preview sample of
  generated drafts + the skip report). After approval the engine processes every account
  automatically, still enforcing the two hard send gates per account.
- **Per account:** research → score → compose a grounded email. On approval → send via
  SEP. No CRM push.
- **Un-actionable accounts** (no deliverable contact, or failing a hard gate) are skipped
  and surfaced in a campaign report (the hand-off to sub-project B).

### Approach (locked: "Approach 1")

Campaign aggregate over child orchestration runs, two-phase:

- **Draft phase:** per target, run a NEW `research_compose` recipe (research → score →
  compose, NO send, NO per-run gate) on the background worker → one draft per account.
- **Campaign approval gate:** human reviews a sample, approves once.
- **Send phase:** per approved target, execute the send reusing `SendMessageTool`'s hard
  gates verbatim.

Reuses the existing orchestration engine, tools, agents, event log, and worker queue.
Per-account runs stay small and independently inspectable. The send gates live in exactly
one place (`SendMessageTool`) and are not duplicated.

### Constraints (carried from project guide)

- Multi-tenant: every table tenant-scoped; every endpoint tenant-scoped + RBAC-gated;
  never bypass `TenantSession`.
- Offline test path (SQLite + stub LLM + in-memory queue) must stay green with zero
  network.
- New config uses the `NEXUS_` prefix.
- Reduce external dependencies — reuse what exists.

---

## 1. Data model

Two new tables, both tenant-scoped (carry `tenant_id`, RLS-guarded).

### `Campaign` — the aggregate over a segment

| Field | Type | Notes |
|-------|------|-------|
| `id` | str (pk) | |
| `tenant_id` | str | tenant scope |
| `name` | str | human label |
| `list_id` | str (fk → ProspectList) | the saved List defining the segment (accounts via `ListItem`) |
| `icp` | JSON | value-prop / messaging context the composer grounds against |
| `sequence` | str | SEP sequence enrolled into on send |
| `status` | str enum | `draft_pending → drafting → awaiting_approval → approved → sending → completed`; plus `cancelled`, `failed` |
| `report` | JSON, nullable | rolled-up counts (drafted/skipped/sent/failed + skip reasons), filled as targets resolve |
| `created_by_user_id` | str | |
| `created_at` / `updated_at` | datetime | |

### `CampaignTarget` — one row per account in the segment

| Field | Type | Notes |
|-------|------|-------|
| `id` | str (pk) | |
| `tenant_id` | str | tenant scope |
| `campaign_id` | str (fk → Campaign) | |
| `account_id` | str (fk → Account) | |
| `run_id` | str, nullable | the orchestration run producing this target's draft |
| `status` | str enum | `pending → drafting → drafted → skipped → approved → sent → failed` |
| `skip_reason` | str, nullable | `no_deliverable_contact` / `ungrounded_draft` / `undeliverable_address` / `research_failed` |
| `draft` | JSON, nullable | snapshot copied off the run blackboard (subject/body/grounding/email_status) so approval UI + report survive the run |
| `created_at` / `updated_at` | datetime | |

**Rationale:** the campaign never holds draft content — it aggregates targets; each target
points at a real orchestration run, reusing existing RunStep/RunEvent durability. Snapshot
the draft onto the target so the UI and report don't depend on the run staying alive.

---

## 2. New planner recipe: `research_compose`

A fourth recipe added to `_RECIPES` in `nexus/orchestration/planner.py`. Identical to
`research_account` **minus the send step**:

```
research(0) → scoring(1) → compose_message(2)
```

No `send_message`, so no per-run approval gate — the draft phase runs fully autonomously
on the worker. The send happens later, in a separate controlled phase, reusing
`SendMessageTool` verbatim (both hard gates intact). Gates stay in exactly one place.

Recipe validation (contiguous idx, no forward refs/cycles) is reused as-is; this recipe is
a strict prefix of `research_account`.

---

## 3. Two-phase execution

### Draft phase (background, autonomous)

1. `POST /campaigns` creates the Campaign + one `CampaignTarget` per account in the List
   (status `pending`), sets campaign `drafting` (transient `draft_pending` on row insert
   before targets enqueue).
2. For each target, enqueue a worker task that creates + executes a `research_compose` run
   for that account. Runs are independent — one account's failure never blocks others.
3. On run completion the worker copies the draft snapshot onto the target (`drafted`), or
   marks `skipped` with a reason if there's no deliverable contact / research couldn't
   ground.
4. When all targets resolve, campaign → `awaiting_approval`; report counts finalized.

### Approval gate (campaign-level, once)

- Human reviews segment + ICP + a **preview sample** (first N drafted targets,
  default N=3) + the skip report.
- One decision approves the whole campaign. No per-account clicking.

### Send phase (background, gated per send)

5. On approve, campaign → `sending`; for each `drafted` target, enqueue a send task that
   runs `SendMessageTool` against that target's draft.
6. The two hard gates fire per account at the boundary: ungrounded draft → refuse (target
   `skipped`/`failed`, reason recorded); `STATUS_INVALID` address → refuse. Survivors →
   `sent`.
7. All targets resolved → campaign `completed`, final report.

Reuses the existing worker queue (memory|redis), orchestration engine, event log. Nothing
new in the execution core.

---

## 4. Per-account isolation & skip-and-report

The reliability spine. Every target is processed in its own run/task with try/except
isolation: a thrown error marks that one target `failed` with the exception summary and
moves on. Un-actionable accounts are **never** silently dropped — they land in the
campaign report:

| Condition | `skip_reason` |
|-----------|---------------|
| no contact with an email | `no_deliverable_contact` |
| research produced no facts | `ungrounded_draft` (send gate would refuse anyway) |
| email verified `STATUS_INVALID` | `undeliverable_address` |
| run errored | `research_failed` |

The report is the explicit hand-off surface to sub-project B (Contact Sourcing): "here are
the N accounts we couldn't reach, and why."

---

## 5. API + RBAC

New router `nexus/api/routers/campaigns.py`. Gated on a new `manage_campaigns` permission
mirroring `manage_plays` (added to the RBAC permission set + role grants).

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/campaigns` | create from `list_id` + icp + sequence; kicks off draft phase → `201` |
| GET | `/campaigns` | list with status + report summary |
| GET | `/campaigns/{id}` | detail: targets, statuses, drafts, report |
| GET | `/campaigns/{id}/preview` | the approval sample (N drafted targets) |
| POST | `/campaigns/{id}/approve` | approve → start send phase |
| POST | `/campaigns/{id}/cancel` | cancel (cascades: pending/drafting targets cancelled) |
| GET | `/campaigns/{id}/events` | SSE stream of campaign progress |

All tenant-scoped via `TenantSession`. Pydantic v2 request/response schemas in
`nexus/api/schemas.py` (`CampaignIn`, `CampaignOut`, `CampaignTargetOut`,
`CampaignPreviewOut`, `CampaignReportOut`).

---

## 6. Event surfacing

The engine already publishes `orchestration.*` per child run. Add campaign-level events on
the same in-process `EventBus`:

- `campaign.drafting`
- `campaign.target_drafted`
- `campaign.target_skipped`
- `campaign.awaiting_approval`
- `campaign.sending`
- `campaign.target_sent`
- `campaign.completed`

Same envelope (run_id / causation_id) so it composes with the existing event log; the SSE
endpoint replays them (Last-Event-ID pattern, mirroring the runs stream). This is also the
seam that feeds sub-project E (Live Dashboard).

---

## 7. Testing strategy (offline-safe, zero network)

- Stub LLM + SQLite + in-memory queue throughout — same as the rest of the suite.
- **Unit:** recipe validation for `research_compose`; target state-machine transitions;
  skip-reason classification.
- **Integration:** create a campaign over a 3-account List → drive draft phase to
  `awaiting_approval` → assert per-target drafts + one deliberately un-actionable account
  lands in the report with the correct reason.
- **Approval → send phase:** assert hard gates fire (one ungrounded draft refused, one
  invalid address refused, survivors `sent`).
- **Idempotency:** re-approving / double-enqueue doesn't double-send (reuse the run
  idempotency-key pattern).
- **Multi-tenant:** a campaign in tenant A is invisible to tenant B.

---

## Frontend (built after backend lands)

A Campaigns screen: create-from-List, a drafting progress view (SSE), an approval screen
showing the sample + skip report, and a sent/report view. Built with the `impeccable`
skill per CLAUDE.md, consuming design tokens, with explicit loading/empty/error states.
Out of scope for the backend implementation plan; tracked as a follow-on.

---

## How this advances all 6 improvements

| # | Improvement | This sub-project |
|---|-------------|------------------|
| 1 | Composite autonomous goal (discover→research→outreach across a segment) | **Built here** |
| 4 | Net-new contact discovery | Seeded by the skip-report hand-off → sub-project B |
| 6 | Multi-channel outbound + cadence | Send phase is the extension point → sub-project C |
| 2 | Recurring scheduler | Wraps campaign creation → sub-project D |
| 3 | Live dashboard over SSE | Consumes campaign events → sub-project E |
| 5 | Auto-sync discovered accounts to CRM | Deferred to its own later cycle (v1 is email-only) |

---

## Out of scope (v1)

- CRM push of any kind (#5).
- Net-new contact sourcing (#4) — un-actionable accounts are reported, not enriched.
- Non-email channels / cadence (#6).
- Scheduling / recurrence (#2).
- Live dashboard UI (#3).
- Editing individual drafts at the approval gate (campaign approval is all-or-nothing in
  v1; per-draft editing can come later).
