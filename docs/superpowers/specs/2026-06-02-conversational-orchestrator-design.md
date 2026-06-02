# Conversational Orchestrator — Design Spec

**Date:** 2026-06-02
**Status:** Approved for planning
**Scope:** Slice A (conversational orchestrator) + thin Slice B (ICP discovery) + CSV proprietary
data + cross-workspace switching. The rich filterable results workspace (Slice C) is deferred,
but this slice ships a basic filterable results table and the data seam C will build on.

---

## 1. Problem & intent

Today a run always targets a *known* `account_id`; the planner deterministically runs
research → score → compose → send for that one account, and results land in the run console.
There is no conversational control, no "find new companies/contacts from an ICP," no results
table, and no place for first-party data.

This slice adds a **chat-first orchestrator**: the user states an ICP in natural language, the
orchestrator asks clarifying questions only when a *required* piece is missing, then launches a
**discovery** run that surfaces matching companies or contacts (own data first, web to fill
gaps). The conversation is context-aware and token-frugal, persists per workspace, and can be
scoped to a specific account/client and resumed later.

### Locked product decisions (from brainstorming)

1. **First slice = chat spine + thin discovery**, plus CSV proprietary-data ingest.
2. **Discovery source = own data ranked by ICP first, then web discovery to fill gaps** (net-new
   companies become `Account` rows).
3. **Clarify logic = hybrid**: a deterministic required-slot schema gates the run; the LLM only
   *phrases* questions and *extracts* slots.
4. **ICP source = pre-fill from the workspace's saved `RelevanceProfile`, stay per-conversation**;
   never writes back unless the user explicitly says "save this as our ICP."
5. **Run surface = hand off to the run console, which embeds a mini-chat** bound to the run's
   session so the user keeps interacting in-context.
6. **Proprietary data = custom-fields model (JSON on Account/Contact + a `CustomFieldDef`
   registry) AND a working CSV import**, shown as columns in discovery results.
7. **Multiple companies with different ICPs = one workspace (tenant) per company** (Option B),
   RLS-isolated, each with its own single `RelevanceProfile`. Switching company = switching
   workspace. Requires a small tenant-switch enabler (§5).

### Non-goals (this slice)

- The rich results workspace (bulk edit, saved views, advanced field mapping UI) — Slice C.
- Multiple named ICP profiles within one workspace — explicitly rejected in favor of Option B.
- Token-level streaming of assistant text — we stream at the message/event granularity.
- LLM-authored (non-deterministic) planning — the planner stays deterministic.

---

## 2. Architecture overview

```
ChatPage / mini-chat ──HTTP/SSE──▶ chat router
                                     │
                                     ▼
                          ChatService ── IntakeController (the "brain")
                                     │        │  Extractor (LLM)
                                     │        │  Phraser   (LLM)
                                     │        │  Summarizer(LLM)
                                     │        └  ContextEnvelope (token budget)
                                     │
                          (when ICP complete) ──▶ OrchestrationEngine.create_run("discover", …)
                                                        │
                                                        ▼
                                                 DiscoveryTool ──▶ DiscoveryAgent
                                                   own-data rank (RelevanceEngine fit)
                                                   + web gap-fill (ctx.browser.search → new Accounts)
                                                        │
                                                        ▼
                                                 run.blackboard["discovery"] = {candidates…}
                                                        │
                                  RunEvent log ── SSE ──▶ run console results panel
```

The chat layer **sits beside** the existing run engine and reuses it unchanged. Discovery is a
new read-only goal/tool/agent. No changes to the run/step/event/approval tables or the engine
control flow.

---

## 3. Data model

All tables tenant-scoped (`TenantScoped` + RLS). All offline-safe.

### 3.1 New tables

**`ChatSession`** (`chat_sessions`)
| column | type | notes |
|---|---|---|
| `id` | str PK | |
| `tenant_id` | str, indexed | workspace = company |
| `created_by` | FK users, nullable | |
| `account_id` | FK accounts, nullable, indexed | the account/"client" the convo centers on; null for pure ICP discovery |
| `parent_session_id` | FK chat_sessions, nullable | branch/continue a prior conversation |
| `title` | str(160) | auto from first user message |
| `status` | str(16) | `active` \| `archived` |
| `target` | str(16), nullable | `companies` \| `contacts` |
| `icp_state` | JSON | working ICP slots; seeded from `RelevanceProfile.icp` |
| `missing_slots` | JSON | cached list (audit/debug) |
| `context_summary` | text | rolling compact summary (§4.3) |
| timestamps | | `TimestampMixin` |

**`ChatMessage`** (`chat_messages`, append-only)
| column | type | notes |
|---|---|---|
| `id` | str PK | |
| `tenant_id` | str, indexed | |
| `session_id` | FK chat_sessions, indexed | |
| `seq` | BigInteger | monotonic within session; `UniqueConstraint(session_id, seq)`; powers SSE replay |
| `role` | str(12) | `user` \| `assistant` \| `system` |
| `kind` | str(24) | `text` \| `clarifying_question` \| `run_launched` \| `notice` |
| `content` | text | rendered text |
| `data` | JSON | e.g. `{slot, suggestions:[…]}` or `{run_id, goal}` |
| timestamps | | |

**`CustomFieldDef`** (`custom_field_defs`)
| column | type | notes |
|---|---|---|
| `id` | str PK | |
| `tenant_id` | str, indexed | |
| `entity` | str(12) | `account` \| `contact` |
| `key` | str(60) | machine key; `UniqueConstraint(tenant_id, entity, key)` |
| `label` | str(120) | display |
| `kind` | str(12) | `text` \| `number` \| `date` \| `bool` \| `url` |
| timestamps | | |

### 3.2 Changed tables

- **`OrchestrationRun`**: add `chat_session_id` (FK chat_sessions, nullable, indexed) — links a run
  back to its conversation so the console renders the mini-chat thread. One session → many runs.
- **`Account`**: add `custom_fields` JSON, default `{}`.
- **`Contact`**: add `custom_fields` JSON, default `{}`.

> JSON `custom_fields` + a `CustomFieldDef` registry (decision a): registry gives the results
> table column metadata (label/kind) and gives CSV import a mapping target, without a normalized
> value table. Tenant-isolated by construction.

### 3.3 Migration / bootstrap

`init_db()` creates new tables for dev/test. For Postgres, an Alembic migration adds the three
tables, the `chat_session_id` FK, and the two `custom_fields` columns (nullable/defaulted, safe
online add). Existing single-`RelevanceProfile`-per-tenant unique constraint is **unchanged**.

---

## 4. The orchestrator brain (`nexus/orchestration/intake.py`)

Three small, independently testable units over a token-budgeted context envelope. None re-reads
the transcript.

### 4.1 ICP slot schema (deterministic core)

Declarative slot list; `required` depends on `target`:

| slot | required when | example |
|---|---|---|
| `target` | always | `companies` / `contacts` |
| `icp_description` *or* `industries` | always | "B2B fintech" / `["fintech"]` |
| `geo` | always | `["US","CA"]` |
| `company_size` | target=companies | `{min:200,max:5000}` |
| `titles` / `seniority` | target=contacts | `["VP Sales","CRO"]` |
| `required_tech` | optional | `["Salesforce"]` |
| `intent_signals` | optional | `["hiring","funding"]` |
| `exclusions` | optional | `["competitors"]` |

`missing_required(icp_state, target)` is pure Python → fully unit-testable.

### 4.2 The three LLM units (stub-deterministic offline)

- **`Extractor`** — input: latest user message + current `icp_state`; output: a **slot-delta**
  (only fields it learned). Small in, small out. Merged by the controller.
- **`Phraser`** — input: the single highest-priority missing slot + saved-ICP defaults; output:
  one natural question + `suggestions[]`. One question per turn.
- **`Summarizer`** — input: prior `context_summary` + latest exchange; output: new summary,
  hard-capped (~150 tokens). Incremental fold.

All three go through the existing `LLMProvider`; the deterministic stub used in tests/CI makes
them reproducible. Each is a focused prompt with a typed parse + a safe fallback (e.g. extractor
parse failure → empty delta, never a crash).

### 4.3 Context envelope (token-frugal — the emphasized requirement)

Per-turn payload to the model =
1. **Structured state** (authoritative, tiny): `icp_state` JSON + `target` + `account_id` +
   `missing_slots`.
2. **Rolling summary**: `context_summary` (~150 tok cap).
3. **Recency window**: last `K=4` raw messages verbatim.
4. **Budget guard**: hard cap (config `orch_chat_token_budget`, default ~1200). On overflow,
   trim the recency window first, then truncate the summary.

The full transcript is persisted for display/audit but **never replayed to the model**, so
per-turn cost stays ~flat as the conversation grows. Resuming a session or branching a child
session loads only `icp_state` + `context_summary`.

### 4.4 Control loop (deterministic; per user turn)

```
1. envelope = ContextEnvelope.build(session, last_user_msg)
2. delta = Extractor(envelope);  icp_state = merge(icp_state, delta)
3. missing = missing_required(icp_state, target)
4. if missing:
       q = Phraser(top(missing), defaults_from_saved_icp)
       append ChatMessage(assistant, kind=clarifying_question, data={slot, suggestions})
       stop
5. else:
       append assistant confirmation ("Finding {target} matching … — go?")
       on explicit "go" (or auto if high-confidence single-shot):
           run = engine.create_run("discover", goal_input={target, icp, max_candidates,
                                    chat_session_id}, account_id=session.account_id, created_by)
           await engine.execute_run(run)            # discovery is read-only; runs inline
           append ChatMessage(assistant, kind=run_launched, data={run_id, goal})
6. context_summary = Summarizer(context_summary, this_turn)
```

Steps 1, 3, 4-gate, 5-launch are pure Python over structured state — the **what** is unit-tested;
only phrasing/extraction/summary touch the LLM (stub-deterministic).

---

## 5. Cross-workspace switching (Option B enabler)

A `User` is global and may hold memberships in several tenants; login already resolves by
`tenant_slug`. The gap: the JWT pins one tenant and there's no in-app switch.

- **`GET /api/auth/tenants`** — tenants the caller is a member of: `[{tenant_id, name, slug, role}]`.
  Derived from `Membership` rows for the authenticated user.
- **`POST /api/auth/switch`** `{tenant_id}` — server re-checks the caller has a `Membership` in
  that tenant (never trusts the client), re-issues a JWT for it, returns `TokenResponse`.
- **Frontend**: a workspace switcher in the app shell — current company + dropdown; selecting one
  calls `/auth/switch`, swaps the stored token, and refetches. Everything below (chat, discovery,
  ICP, accounts) is RLS-scoped to the active tenant.

These endpoints authenticate the *user* (from the current valid token) but are not tenant-data
operations, so they bypass `TenantSession` data scoping and operate on `Membership`/`Tenant`
directly, exactly like login.

---

## 6. Discovery (thin Slice B)

### 6.1 `DiscoveryAgent` (`nexus/agents/discovery.py`)

Runs with `account=None` (the runtime already builds context + loads the `RelevanceProfile` and
exposes `ctx.browser` without an account). Reads `ctx.inputs`: `{target, icp, max_candidates}`.

Algorithm:
1. **Own-data ranking.** Query the tenant's `Account`s (and `Contact`s when target=contacts).
   Apply hard ICP filters (industry, size band, geo, required_tech) deterministically, then score
   each survivor with the `RelevanceEngine` fit (reusing `account_fit` → `score`, `reasons`).
   Rank desc; take top `max_candidates`.
2. **Web gap-fill.** If matches `< max_candidates` and `ctx.browser` supports `search`, issue
   ICP-derived queries (industry + geo + intent terms). For each hit, dedupe by domain; create a
   net-new `Account(source="discovery")` with minimal fields; mark `is_new=True`. (Contact-level
   web discovery is best-effort this slice; net-new companies are the primary fill.)
3. Return a ranked candidate list:
   ```json
   {"target":"companies","counts":{"own":N,"new":M},
    "candidates":[{"entity":"account","id":"…","name":"…","domain":"…",
                   "fit_score":78,"fit_reasons":["…"],"source":"own|discovery",
                   "is_new":false,"custom_fields":{…}}]}
   ```

Offline-deterministic: stub LLM for the brief/summarize, stubbed browser returns fixed hits, so
counts and ordering are reproducible in tests.

### 6.2 `DiscoveryTool` + goal recipe

- **`DiscoveryTool`** (`discovery`, `requires_approval=False`): reads `tc.run.goal_input`
  (`target`, `icp`, `max_candidates`), calls `runtime.run("discovery", ts, account_id=None,
  **inputs)`, writes the candidate list to `tc.blackboard["discovery"]`.
- **Planner recipe `discover`**: a single read-only step `[PlanStep(idx=0, tool="discovery")]`.
  No approval gate (read-only). `available_goals()` gains `discover`.

### 6.3 Drill-in (reuse, not rebuild)

From the results table a row action **"Research"** launches the existing `research_account` /
`research_only` goal on that candidate `account_id` — no new code, just a wired handoff. The
mini-chat can also issue "research the top 3" (maps to launching those runs). Deeper per-account
research stays the existing path.

---

## 7. API surface

All under existing auth; **manager+** for launching work (matches current orchestration routes);
read endpoints allow any member. All tenant-scoped via `TenantSession`.

### 7.1 Chat
- `POST /api/orchestration/chat/sessions` — body `{account_id?, parent_session_id?, message?}`.
  Creates the session (seeds `icp_state` from saved profile), runs the control loop if `message`
  present, returns `{session, messages}`.
- `GET /api/orchestration/chat/sessions?account_id=&status=` — list (recent; filterable by account).
- `GET /api/orchestration/chat/sessions/{id}` — session + ordered messages.
- `POST /api/orchestration/chat/sessions/{id}/messages` — body `{content}`. Appends the user
  message, runs the control loop, returns appended assistant messages (+ any `run_launched`).
- `GET /api/orchestration/chat/sessions/{id}/stream` — SSE of new `ChatMessage`s and, when a run
  is linked, its `RunEvent`s; resumable via `Last-Event-ID` on `seq` (reuses the existing SSE
  frame/replay pattern).
- `POST /api/orchestration/chat/sessions/{id}/save-icp` — explicit opt-in: write `icp_state` →
  `RelevanceProfile.icp` for the workspace.

### 7.2 Discovery results
- Surfaced on `RunOut.blackboard["discovery"]` (small lists).
- `GET /api/orchestration/runs/{id}/results?source=&min_fit=&q=&cf_<key>=` — server-side
  filtered/paginated access for larger lists; returns candidates + the active `CustomFieldDef`
  columns so the table can render dynamic columns.

### 7.3 Proprietary data
- `GET/POST/DELETE /api/custom-fields` — manage `CustomFieldDef`s.
- `POST /api/custom-fields/import` — multipart CSV. Body also carries a column→field mapping and
  the match key (`domain` for accounts, `email` for contacts). Upserts `custom_fields` on matched
  rows; can create missing `CustomFieldDef`s on the fly. Returns `{matched, updated, created_fields,
  skipped:[{row, reason}]}`. Bounded size; streamed parse via stdlib `csv`.

### 7.4 Tenant switch — see §5.

### 7.5 Schemas (`nexus/orchestration/chat_schemas.py`, `nexus/api/schemas.py`)
`ChatSessionOut`, `ChatMessageOut`, `ChatSessionCreate`, `ChatMessageCreate`, `DiscoveryResultOut`,
`CustomFieldDefOut/In`, `CsvImportResult`, `TenantOut`, `SwitchTenantRequest`. Flat, projection-only.

---

## 8. Frontend

Stack unchanged: React 18 + TS strict + Vite, react-router lazy + `RequireRole`, CSS Modules +
tokens, framer-motion. All views handle **loading / empty / error**; a11y non-negotiable
(labels, keyboard, visible focus, `aria-*`, reduced-motion). Invoke `impeccable` (+ `ui-ux-pro-max`
for visual/UX, `framer-motion` for motion) before building each surface.

### 8.1 `ChatPage` (`/orchestrator`)
- Left: session list (grouped **Recent** + **By account/client**; new-session button).
- Center: conversation — user/assistant bubbles; `clarifying_question` renders the question with
  **suggestion chips** (click to fill); `run_launched` renders a handoff card linking to the
  console. Composer with send; SSE-driven streaming append; "Reconnecting"/"Streaming" status like
  the run feed.
- Entry from `AccountDetailPage`: "Ask the orchestrator about {account}" pre-creates a session
  with `account_id`.

### 8.2 Run console mini-chat (`RunDetailPage`)
- A docked mini-chat panel bound to `run.chat_session_id` (reuses the chat thread + composer
  components). Lets the user keep interacting while the run streams. Hidden when a run has no
  session.

### 8.3 Discovery results panel (in the run console)
- A filterable table of companies/contacts. Columns: name, domain/email, **fit** (ScoreMeter),
  fit reasons, source badge (`own`/`new`), + dynamic `CustomFieldDef` columns.
- Filters: fit threshold, source, free-text, and per-custom-field. Server-side via
  `/runs/{id}/results` when large; client-side for small lists.
- Row action **Research** (launch existing goal); multi-select → "Add to list" (reuse Lists).
- Full loading (skeleton rows) / empty / error states.

### 8.4 CSV proprietary-data import (modal)
- Drop CSV → parse preview (stdlib-friendly small parser) → map columns to existing fields or
  create new `CustomFieldDef`s → choose match key → import → result summary; new columns then
  appear in the results table.

### 8.5 Workspace switcher (app shell) — §5.

### 8.6 Client wiring
- `frontend/src/lib/types.ts`: `ChatSession`, `ChatMessage`, `DiscoveryCandidate`,
  `DiscoveryResult`, `CustomFieldDef`, `CsvImportResult`, `TenantSummary`.
- `frontend/src/lib/api.ts`: chat CRUD + `streamChatEvents` (mirrors `streamRunEvents`), results,
  custom-fields + import, `listTenants` / `switchTenant`.
- Routes + nav icon for the orchestrator; `RequireRole minRole="manager"` on launch surfaces.

---

## 9. Testing (offline, deterministic)

**Backend (pytest, stub LLM + stubbed browser):**
- Intake: `missing_required` truth table; extractor merge; control loop (missing → asks exactly
  one; complete → launches `discover`); summarizer cap; envelope budget trim order; child-session
  inheritance of summary + icp.
- Discovery: own-data ranking + hard filters; web gap-fill creates net-new accounts deduped by
  domain; blackboard shape; read-only (never parks at approval).
- Tenant switch: list tenants; switch re-issues token; rejects a tenant the user isn't a member of.
- Custom fields: CRUD; CSV import matching/creating/skip reasons; tenant isolation.
- Chat API: create/list/get/post-message/SSE replay; session↔run link; RBAC + tenant scoping.
- Regression: keep the full suite green (currently 62).

**Frontend:** `tsc` + `vite build` clean; in-browser end-to-end via the preview harness — state
ICP → answer a clarifying question → launch → results render → filter → CSV import adds a column →
switch workspace shows isolated data.

---

## 10. Risks & mitigations

- **Scope:** large slice. Mitigation: phase the implementation plan (data model → intake → tenant
  switch → discovery → API → frontend), each phase independently green and reviewable.
- **Non-determinism creep** in the brain: contained by the slot schema + stub-deterministic LLM
  units; the gate/launch logic is pure Python.
- **Web discovery noise / cost:** bounded `max_candidates`, dedupe by domain, `source` flag so
  reviewers can distinguish net-new from owned; offline tests never hit the network.
- **Token growth:** the context envelope caps per-turn payload regardless of history length.
- **Cross-tenant leakage:** every new table is `TenantScoped`; switch endpoint re-verifies
  membership server-side; tests assert isolation.

---

## 11. Build phases (for the plan)

1. **Data model + migration** (tables, columns, `init_db`, Alembic).
2. **IntakeController + context envelope** (slot schema, extractor/phraser/summarizer, control
   loop) — unit-tested in isolation, no HTTP.
3. **Tenant switch** (endpoints + shell switcher).
4. **Discovery** (agent + tool + `discover` recipe).
5. **Chat + results + custom-fields API** (routers, schemas, SSE).
6. **Frontend** (ChatPage, mini-chat, results panel, CSV modal, switcher, client wiring).
7. **Full verification** (suite green, build clean, in-browser e2e).
