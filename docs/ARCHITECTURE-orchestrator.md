# NEXUS GTM — Multi-Agent Orchestrator

> Turns the single-shot agent runtime into a coordinated, durable, resumable workflow. A
> planner authors a DAG of tool calls; an engine drives it over a shared run blackboard;
> outbound actions park at a human approval gate; progress streams as an append-only event
> log. The whole thing runs offline (SQLite + stub LLM + in-memory queue) for tests/CI.

This document specifies the **orchestration spine** — the first, load-bearing slice of the
larger "give a URL, get reviewed hyper-personalized sequences" vision. It is deliberately
minimal but shaped so each later capability (target discovery, multi-touch sequences,
proprietary-data ingestion, a learning loop) slots in without reworking the core.

## 1. Why this exists

The existing `AgentRuntime` runs **one** agent against relevance-grounded context and records
an audit row. That is the right primitive, but a real GTM motion is multi-step and
side-effecting: research an account, score it, draft warm outreach, and **send** it — where
the send must never fire unreviewed. That needs four things the runtime doesn't have:

1. a **durable, multi-step run model** (survives a process restart, resumes from the DB),
2. a **planner** that decomposes a goal into an ordered, validated DAG of tool calls,
3. an **approval gate** that parks outbound steps until a human approves/edits/rejects,
4. a **streaming progress** channel the UI can follow live and replay after a reconnect.

A shared **blackboard** on the run is how subagents stay aware of each other: research writes
facts, the composer grounds its draft in those facts, the sender ships the approved draft.

## 2. Component structure

```
nexus/orchestration/
  tools.py      Tool registry — typed capabilities wrapping existing agents + the SEP send
  planner.py    Planner — goal -> validated DAG of step descriptors (deterministic today)
  engine.py     OrchestrationEngine — drives the DAG, parks at gates, emits the event log
  schemas.py    Pydantic wire contracts (RunOut / RunStepOut / ApprovalOut / decisions)

nexus/models/orchestration.py   OrchestrationRun · RunStep · RunEvent · Approval
nexus/api/routers/orchestration.py   REST + SSE endpoints
nexus/workers/tasks.py               run_orchestration durable handler + enqueue
nexus/core/events.py                 Event envelope upgraded with run_id/step_id/causation_id
```

### Layering & seams

- **Tools wrap agents, they don't replace them.** A `Tool` exposes a stable name + the
  blackboard contract; `_AgentTool` delegates to `AgentRuntime.run(...)`. Swapping an agent's
  internals, or replacing a deterministic tool with an LLM-backed one, is invisible to the
  engine. `SendMessageTool` is the one non-agent tool — it reads the approved draft and pushes
  through the existing `SEPConnector`.
- **The planner is the only place goals are decomposed.** It returns plain step dicts — exactly
  the shape an LLM planner would emit later — so model-authored plans are a drop-in behind
  `Planner.plan()`. Today's recipes are deterministic for reproducibility and offline tests.
- **The engine never imports an agent directly.** It only knows tools + the durable model. This
  keeps the blast radius of an agent change to the tool layer.

## 3. Data model & schema

All tables are tenant-scoped (`tenant_id`), id = uuid hex `String(32)`, with `created_at` /
`updated_at`. Status strings are centralized as module constants with `*_TERMINAL` frozensets.

| Table | Key columns | Purpose |
|---|---|---|
| `orchestration_runs` | `goal`, `goal_input` JSON, `status`, `plan` JSON, **`blackboard` JSON**, `account_id` FK, `idempotency_key`, `error` | A planned, resumable unit of work. The blackboard is the shared inter-agent context. |
| `run_steps` | `run_id` FK, `idx`, `tool`, `inputs` JSON, `depends_on` JSON `list[int]`, `status`, `attempts`, `requires_approval`, `approval_id`, `output` JSON, `error` | One node in the DAG. `idx` is unique per run (the engine's idempotency anchor). |
| `run_events` | `run_id` FK, `seq` BigInt, `type`, `data` JSON | Append-only log. `seq` is monotonic per run — the SSE replay cursor. |
| `approvals` | `run_id` FK, `step_id` FK, `kind`, `payload` JSON, `status`, `decided_by`, `decided_at`, `edits` JSON | The human-in-the-loop gate; `payload` is the exact content that will go out. |

**Indexes / constraints chosen for the access patterns:**
- `uq_run_idempotency (tenant_id, idempotency_key)` — dedupes double-submits at the DB level.
- `ix_run_tenant_status (tenant_id, status)` — list runs / find resumable runs per tenant.
- `uq_step_run_idx (run_id, idx)` — a resumed run never double-executes a node.
- `ix_step_run_status (run_id, status)` — find the next runnable step.
- `uq_event_run_seq (run_id, seq)` + `ix_event_run_seq` — gap-free monotonic replay.

**Deliberate decision — `RunStep.approval_id` is a plain `String(32)`, not an FK.** A real FK
both ways (`run_steps.approval_id -> approvals.id` and `approvals.step_id -> run_steps.id`)
forms a circular dependency that forces ALTER-based creation and breaks SQLite `create_all`.
The plain reference keeps offline tests trivial; the relationship is still navigable.

## 4. Data flow — the flagship goal

`research_account` plan (authored by the planner, each step consuming the prior's blackboard):

```
research ──> scoring ──> compose_message ──> send_message [requires_approval]
  │            │              │                    │
  writes       writes         writes               reads blackboard["draft"]
  research{}   composite      draft{grounded?}     -> SEPConnector.push_contact
```

1. **create_run** plans the goal, persists the run + step rows, seeds the blackboard with
   `account_id`, emits `run.created`.
2. **execute_run** repeatedly picks the next runnable step (all deps `completed`) and runs its
   tool with bounded retries (`orch_max_attempts`). Each tool writes its slice of the
   blackboard; the engine flags the JSON column dirty so the mutation persists.
3. At `send_message` (`requires_approval`), the engine **parks**: it creates a pending
   `Approval` carrying the draft payload, sets the step `awaiting_approval`, sets the run
   `awaiting_approval`, emits `approval.requested`, and **stops**.
4. **decide(approve)** optionally merges reviewer `edits` into the draft, then runs the gated
   tool and continues the DAG. **decide(reject)** marks the step `rejected` and cascade-skips
   any downstream dependents. Either way the run is then finalized.

Every transition appends a `RunEvent` and publishes an `Event` on the in-proc bus carrying the
`run_id` envelope.

## 5. API design

All endpoints are tenant-scoped and RBAC-gated. Two new permissions (both manager+):
`run_orchestration` (author/inspect runs) and `approve_outreach` (decide the gate).

| Method | Path | Permission | Notes |
|---|---|---|---|
| POST | `/orchestration/runs` | run_orchestration | Plan + drive inline to first stop; returns `RunOut`. `idempotency_key` makes it double-submit safe. |
| GET | `/orchestration/runs` | run_orchestration | Recent runs for the tenant. |
| GET | `/orchestration/runs/{id}` | run_orchestration | Run + steps + blackboard snapshot. |
| POST | `/orchestration/runs/{id}/cancel` | run_orchestration | Cancels; skips pending steps, rejects a pending gate. |
| GET | `/orchestration/runs/{id}/events` | run_orchestration | **SSE**, resumable via `Last-Event-ID`. |
| GET | `/orchestration/approvals?status_filter=` | approve_outreach | The reviewer queue. |
| POST | `/orchestration/approvals/{id}/decision` | approve_outreach | `{decision: approve\|reject, edits?}`. |

**Streaming.** `GET …/events` replays the durable log from `Last-Event-ID`, then follows. Each
poll opens a short-lived tenant session (never pins a connection), closes on a terminal run,
and emits a heartbeat + closes when the run parks at a gate (the client reconnects after
deciding). A `max_polls` ceiling guarantees no stream leaks. SSE is used (not WebSockets)
because progress is server→client only and it survives proxies with `X-Accel-Buffering: no`.

## 6. Execution semantics & guardrails

- **Out-of-context guard.** Agents report in-band problems as `{"error": …}` while still
  `status == completed`. `_AgentTool` raises `ToolError` on that, so a step that couldn't
  ground itself **fails loudly** instead of feeding garbage downstream.
- **Groundedness flag.** `compose_message` marks the draft `grounded` iff research produced
  facts. An ungrounded draft is allowed to exist but flagged for the reviewer / auto-send policy.
- **Send never invents content.** `send_message` fails if there is no approved draft.
- **Retries.** Per-step bounded by `orch_max_attempts`; a final failure cascade-skips dependents
  and finalizes the run as `failed`.
- **Runaway guard.** The planner rejects plans over `orch_max_steps`; it validates contiguous
  `idx`, no forward refs / cycles, registered tools, and that a side-effecting tool can only have
  its approval gate *escalated*, never dropped.
- **Idempotency.** `create_run` returns the existing run for a repeated `idempotency_key`;
  `run_steps.idx` uniqueness makes a resumed run safe to re-drive.

## 7. Message bus — interface kept, in-proc now

The `Event` envelope gains `run_id` / `step_id` / `causation_id` (additive; existing callers
unaffected). Today these publish on the in-process `EventBus`. Because the **durable** record is
the `run_events` table and the bus is only live fan-out, swapping the bus for Redis
Streams / NATS later is transparent to subscribers — same envelope, same `run_id` correlation.
No new dependency was added for this slice.

## 8. Caching & scale path (designed, not yet built)

Shaped so these are additive when load demands them:
- **Tool-result cache** keyed `tool:{name}:{tenant}:{hash(inputs)}` (Redis) — research/scoring
  are pure-ish and expensive.
- **Live SSE fan-out** via a Redis Stream per run so multiple subscribers and multiple app
  instances share one source; the DB log stays the durable replay.
- **Idempotency / rate / token budget** counters via `SET NX` locks and per-tenant counters.
- **Durable driver.** `enqueue_run_orchestration` + the `run_orchestration` worker handler move
  long runs off the request path; the API only drives inline to the first gate for snappy UX.

## 9. What's intentionally deferred

Next slices, each its own spec → plan → build: `infer_icp` + `discover_targets` agents (the
"give a URL, find targets" front door); `Sequence` / `SequenceStep` / `OutreachEvent` models for
multi-touch call+email cadences with calendar follow-ups; proprietary-data ingestion (import
model + dedup/merge engine + API + UI) so a tenant's own account/person data grounds the agents;
a frontend run console (timeline + live SSE + approve buttons); and a learning loop that feeds
outcomes back into scoring/relevance.

## 10. Tests (offline, in `tests/test_orchestration.py`)

Planner DAG shape + unknown-goal rejection; run parks at the gate with nothing sent; approve
resumes and the SEP push is recorded; approve-with-edits reaches the send payload; reject skips
the send and the run still completes; double-decision is rejected; idempotent create returns the
same run; a read-only goal completes with no gate; the event log is gap-free monotonic starting
at `run.created`. All green alongside the existing suite (62 passed).
