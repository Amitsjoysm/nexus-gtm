# Conversational Orchestrator — Frontend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (or subagent-driven-development) to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. **UI tasks (5–10) MUST invoke the `impeccable` skill before writing each surface** (CLAUDE.md mandate for NEXUS UI); `impeccable` owns the visual/pixel decisions, this plan owns the contracts, states, file layout, and wiring.

**Goal:** Wire the already-shipped conversational-orchestrator backend (chat sessions, discovery results, custom fields, cross-workspace switch) into the React + TS + Vite app as production-grade UI: a `/orchestrator` chat page, a docked mini-chat + discovery results panel in the run console, a CSV proprietary-data import modal, and a workspace switcher in the app shell.

**Architecture:** Extend the existing typed `ApiClient` and `types.ts` (the single UI↔server contract), then build screens that compose existing `src/components/ui/*` primitives and reuse the proven SSE pattern from `RunDetailPage` (`streamRunEvents` → `streamChatEvents`). Auth gains a `switchTenant` action that swaps the stored JWT and forces a full data refetch (route remount). Chat thread + composer are extracted into reusable components so both the full ChatPage and the run-console mini-chat share them. Every view handles loading (skeletons) / empty / error explicitly; a11y is non-negotiable (labelled controls, keyboard, visible focus, `aria-*`, reduced-motion).

**Tech Stack:** React 18 + TypeScript (strict) + Vite, react-router-dom v6 (lazy + `RequireRole`), CSS Modules + design tokens (`src/styles/tokens.css`), framer-motion (transform/opacity only, `prefers-reduced-motion` fallbacks). No new dependencies.

---

## Confirmed backend contracts (source of truth — do not guess)

All paths are under `/api`. RBAC: launching/mutating is **manager+** (`run_orchestration`); custom-fields are **admin+** (`manage_relevance`); reads allow any member; tenant list/switch authenticate the user only.

**Chat** (`nexus/api/routers/chat.py`, `nexus/orchestration/chat_schemas.py`)
- `POST /orchestration/chat/sessions` — body `{account_id?, parent_session_id?, message?}` → `ChatTurnResponse` (201). Requires `run_orchestration`.
- `GET /orchestration/chat/sessions?account_id=&status_filter=` → `ChatSessionOut[]`. Any member.
- `GET /orchestration/chat/sessions/{id}` → `ChatTurnResponse`. Any member.
- `POST /orchestration/chat/sessions/{id}/messages` — body `{content}` → `ChatTurnResponse` (only the **appended** messages). Requires `run_orchestration`.
- `POST /orchestration/chat/sessions/{id}/save-icp` → `{ok, icp}`. Requires `manage_relevance`.
- `GET /orchestration/chat/sessions/{id}/stream` — SSE; frame `event:` = message `kind`, `data` = `{id, role, kind, content, data}`; resumable via `Last-Event-ID` (on `seq`). Closes after ~2s idle or ~60s ceiling.
- `ChatSessionOut` = `{id, title, status, target|null, account_id|null, icp_state, missing_slots[], context_summary, created_at}`.
- `ChatMessageOut` = `{id, seq, role, kind, content, data, created_at}`.
- Message `kind` values: `user` / `assistant` (plain text), `clarifying_question` (`data = {slot, suggestions: string[]}`), `confirmation`, `run_launched` (`data = {run_id, goal}`). Treat unknown kinds as plain text.

**Discovery results** (`nexus/api/routers/orchestration.py`)
- `GET /orchestration/runs/{id}/results?source=&min_fit=&q=&cf_<key>=&limit=&offset=` → `ResultsResponse`.
- `ResultsResponse` = `{run_id, target|null, total, counts, columns: [{key, label, kind}], candidates: dict[]}`.
- Candidate dict shape (already flat/frontend-ready): `{entity: "account"|"contact", id, name, domain?, email?, title?, industry?, fit_score: number, fit_reasons: string[], source: "own"|"discovery", is_new: bool, custom_fields: Record<string,unknown>}`.

**Custom fields** (`nexus/api/routers/custom_fields.py`)
- `GET /custom-fields?entity=` → `CustomFieldOut[]` = `{id, entity, key, label, kind}`.
- `POST /custom-fields` — body `{entity, label, key?, kind?}` → `CustomFieldOut` (201).
- `DELETE /custom-fields/{id}` → 204.
- `POST /custom-fields/import` — multipart form: `entity`, `match_column`, `mapping` (JSON string `{csvColumn: fieldKey}`), `file` → `ImportResult` = `{matched, updated, created_fields: string[], skipped: number}`.

**Tenant switch** (`nexus/api/routers/auth.py`)
- `GET /auth/tenants` → `TenantOut[]` = `{tenant_id, name, slug, role}`.
- `POST /auth/switch` — body `{tenant_id}` → `TokenResponse` `{access_token, token_type, tenant_id, role}`.

---

## File structure

**Wiring (Tasks 1–2)**
- Modify `frontend/src/lib/types.ts` — add chat, discovery, custom-field, tenant types.
- Modify `frontend/src/lib/api.ts` — add chat CRUD + `streamChatEvents`, results, custom-fields + import, `listTenants`/`switchTenant`. Add a private `requestForm` for multipart.

**Auth (Task 3)**
- Modify `frontend/src/app/AuthContext.tsx` — add `switchTenant(tenantId)` that calls `/auth/switch`, replaces the session token, persists, and bumps a `tenantEpoch` so screens remount/refetch.

**Shared chat components (Task 4)**
- Create `frontend/src/components/chat/ChatThread.tsx` (+ `.module.css`) — renders an ordered message list (bubbles, clarifying-question chips, run-launched handoff card).
- Create `frontend/src/components/chat/ChatComposer.tsx` (+ `.module.css`) — textarea + send, Enter-to-send / Shift+Enter newline, disabled/busy states.
- Create `frontend/src/components/chat/useChatSession.ts` — hook owning a session's messages + SSE follow (mirrors `useRunActivity`), exposes `{session, messages, send, streaming, reconnecting}`.
- Create `frontend/src/components/chat/index.ts` — barrel.

**Workspace switcher (Task 5)**
- Create `frontend/src/components/layout/WorkspaceSwitcher.tsx` (+ `.module.css`).
- Modify `frontend/src/components/layout/Topbar.tsx` — mount the switcher in `.left`, after the title.

**ChatPage (Tasks 6–7)**
- Create `frontend/src/pages/ChatPage.tsx` (+ `.module.css`) — session list rail + conversation + composer.
- Modify `frontend/src/pages/AccountDetailPage.tsx` — "Ask the orchestrator about {account}" entry (manager+), pre-creates a session with `account_id` and navigates to `/orchestrator/{id}`.

**Discovery results panel (Task 8)**
- Create `frontend/src/components/discovery/ResultsPanel.tsx` (+ `.module.css`) — filterable table (name, domain/email, fit ScoreMeter, reasons, source badge, dynamic custom-field columns), Research row action, "Add to list" multi-select.
- Create `frontend/src/components/discovery/index.ts`.

**Run console integration (Task 9)**
- Modify `frontend/src/pages/RunDetailPage.tsx` — when `run.chat_session_id` present, dock the mini-chat; when `run.goal === "discover"` (or `blackboard.discovery` present), render `ResultsPanel`.
- Modify `frontend/src/lib/types.ts` — add `chat_session_id?: string | null` to `Run` (backend `RunOut` carries it).

**CSV import modal (Task 10)**
- Create `frontend/src/components/discovery/ImportCsvModal.tsx` (+ `.module.css`) — drop/select CSV → preview (stdlib-friendly parser) → map columns → match key → import → result summary.
- Create `frontend/src/lib/csv.ts` — tiny dependency-free CSV parser (quoted fields, commas, CRLF) + unit-testable.

**Routes + nav (Task 11)**
- Modify `frontend/src/App.tsx` — lazy `ChatPage`; routes `/orchestrator` and `/orchestrator/:sessionId` under `RequireRole minRole="manager"`.
- Modify `frontend/src/app/nav.tsx` — nav item "Orchestrator" (`SparklesIcon` or existing icon), `minRole: "manager"`.
- Modify `frontend/src/components/ui/icons.tsx` — add any missing icon (only if needed).

**Verification (Task 12)** — `tsc -b --noEmit`, `vite build`, in-browser e2e via preview harness.

---

## Task 1: Client types

**Files:**
- Modify: `frontend/src/lib/types.ts` (append a new section before the closing of the file)

- [ ] **Step 1: Add the types**

Append to `frontend/src/lib/types.ts`:

```ts
// ---- conversational orchestrator: chat ----
export type ChatMessageKind =
  | "user"
  | "assistant"
  | "clarifying_question"
  | "confirmation"
  | "run_launched";

export interface ChatMessage {
  id: string;
  seq: number;
  role: "user" | "assistant";
  kind: ChatMessageKind | string;
  content: string;
  data: Record<string, unknown>;
  created_at: string;
}

export interface ChatSession {
  id: string;
  title: string;
  status: string;
  target: string | null;
  account_id: string | null;
  icp_state: Record<string, unknown>;
  missing_slots: string[];
  context_summary: string;
  created_at: string;
}

export interface ChatTurnResponse {
  session: ChatSession;
  messages: ChatMessage[];
}

export interface CreateSessionRequest {
  account_id?: string | null;
  parent_session_id?: string | null;
  message?: string | null;
}

export interface SaveIcpResponse {
  ok: boolean;
  icp: Record<string, unknown>;
}

/** One frame from a chat session's SSE stream (mirrors RunStreamEvent). */
export interface ChatStreamEvent {
  seq: number;
  kind: string;
  data: { id?: string; role?: string; kind?: string; content?: string; data?: Record<string, unknown> };
}

// ---- conversational orchestrator: discovery results ----
export interface DiscoveryCandidate {
  entity: "account" | "contact";
  id: string;
  name: string;
  domain?: string | null;
  email?: string | null;
  title?: string | null;
  industry?: string | null;
  fit_score: number;
  fit_reasons: string[];
  source: "own" | "discovery";
  is_new: boolean;
  custom_fields: Record<string, unknown>;
  [key: string]: unknown;
}

export interface ResultColumn {
  key: string;
  label: string;
  kind: string;
}

export interface DiscoveryResult {
  run_id: string;
  target: string | null;
  total: number;
  counts: Record<string, number>;
  columns: ResultColumn[];
  candidates: DiscoveryCandidate[];
}

export interface ResultsQuery {
  source?: string;
  min_fit?: number;
  q?: string;
  limit?: number;
  offset?: number;
  /** Arbitrary cf_<key> filters. */
  [cf: string]: string | number | undefined;
}

// ---- proprietary data: custom fields ----
export type CustomFieldEntity = "account" | "contact";

export interface CustomFieldDef {
  id: string;
  entity: CustomFieldEntity | string;
  key: string;
  label: string;
  kind: string;
}

export interface CreateCustomFieldRequest {
  entity: CustomFieldEntity;
  label: string;
  key?: string | null;
  kind?: string;
}

export interface CsvImportResult {
  matched: number;
  updated: number;
  created_fields: string[];
  skipped: number;
}

// ---- cross-workspace switch ----
export interface TenantSummary {
  tenant_id: string;
  name: string;
  slug: string;
  role: Role;
}

export interface SwitchTenantRequest {
  tenant_id: string;
}
```

- [ ] **Step 2: Extend `Run` with the chat-session link**

In the existing `Run` interface, add the optional field (backend `RunOut` includes it on discovery runs):

```ts
export interface Run {
  id: string;
  goal: string;
  status: RunStatus;
  account_id: string | null;
  error: string | null;
  created_at: string;
  steps: RunStep[];
  blackboard: RunBlackboard;
  chat_session_id?: string | null;
}
```

> Note: if `RunOut` does not currently serialize `chat_session_id`, add it to `nexus/orchestration/schemas.py::RunOut.from_model` (read `run.chat_session_id`) as part of this step and re-run the backend suite (`python -m pytest -q`) to confirm green. Verify by reading the model field `OrchestrationRun.chat_session_id` first.

- [ ] **Step 3: Typecheck**

Run: `cd frontend && npm run typecheck`
Expected: PASS (no usages yet; pure additive types).

- [ ] **Step 4: Commit**

```bash
git add frontend/src/lib/types.ts
git commit -m "feat(fe): add chat/discovery/custom-field/tenant client types"
```

---

## Task 2: Client API methods

**Files:**
- Modify: `frontend/src/lib/api.ts`

- [ ] **Step 1: Import the new types**

Add to the `import type { ... } from "./types"` block: `ChatMessage`, `ChatSession`, `ChatTurnResponse`, `CreateSessionRequest`, `SaveIcpResponse`, `ChatStreamEvent`, `DiscoveryResult`, `ResultsQuery`, `CustomFieldDef`, `CreateCustomFieldRequest`, `CsvImportResult`, `TenantSummary`.

- [ ] **Step 2: Add a multipart helper** (after the private `request<T>` method)

```ts
  /** Multipart POST (FormData). Mirrors `request` for auth + error handling, no JSON body. */
  private async requestForm<T>(path: string, form: FormData, signal?: AbortSignal): Promise<T> {
    const headers: Record<string, string> = {};
    if (this.token) headers["Authorization"] = `Bearer ${this.token}`;
    let res: Response;
    try {
      res = await fetch(this.buildUrl(path), { method: "POST", headers, body: form, signal });
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") throw err;
      throw new ApiError(0, "Network error — couldn't reach the server.");
    }
    if (res.status === 401) this.onUnauthorized?.();
    const text = await res.text();
    const data = text ? safeJsonParse(text) : null;
    if (!res.ok) {
      const detail =
        (data && typeof data === "object" && "detail" in data
          ? String((data as { detail: unknown }).detail)
          : null) || res.statusText || "Request failed";
      throw new ApiError(res.status, detail);
    }
    return data as T;
  }
```

- [ ] **Step 3: Add the orchestrator-chat methods** (in a new `// ---- orchestrator chat ----` section)

```ts
  createChatSession(body: CreateSessionRequest, signal?: AbortSignal) {
    return this.request<ChatTurnResponse>("/orchestration/chat/sessions", {
      method: "POST", body, signal,
    });
  }
  listChatSessions(params: { account_id?: string; status_filter?: string } = {}, signal?: AbortSignal) {
    return this.request<ChatSession[]>("/orchestration/chat/sessions", { query: params, signal });
  }
  getChatSession(id: string, signal?: AbortSignal) {
    return this.request<ChatTurnResponse>(`/orchestration/chat/sessions/${id}`, { signal });
  }
  postChatMessage(id: string, content: string, signal?: AbortSignal) {
    return this.request<ChatTurnResponse>(`/orchestration/chat/sessions/${id}/messages`, {
      method: "POST", body: { content }, signal,
    });
  }
  saveChatIcp(id: string, signal?: AbortSignal) {
    return this.request<SaveIcpResponse>(`/orchestration/chat/sessions/${id}/save-icp`, {
      method: "POST", signal,
    });
  }
```

- [ ] **Step 4: Add `streamChatEvents`** (mirror `streamRunEvents`; reuse `parseSseFrame` shape — frame `event:` carries the message `kind`)

```ts
  /**
   * Stream a chat session's new messages over SSE. Same hand-rolled fetch+reader as
   * `streamRunEvents` (EventSource can't send Authorization). Resumable via `lastEventId`
   * (highest `seq`). Resolves when the server closes (idle or ceiling); the caller reconnects.
   */
  async streamChatEvents(
    sessionId: string,
    opts: { onEvent: (event: ChatStreamEvent) => void; lastEventId?: number },
    signal?: AbortSignal,
  ): Promise<void> {
    const headers: Record<string, string> = { Accept: "text/event-stream" };
    if (this.token) headers["Authorization"] = `Bearer ${this.token}`;
    if (opts.lastEventId) headers["Last-Event-ID"] = String(opts.lastEventId);
    const res = await fetch(this.buildUrl(`/orchestration/chat/sessions/${sessionId}/stream`), {
      headers, signal,
    });
    if (res.status === 401) this.onUnauthorized?.();
    if (!res.ok || !res.body) {
      throw new ApiError(res.status, res.statusText || "Couldn't open the chat stream");
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    try {
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const frames = buffer.split("\n\n");
        buffer = frames.pop() ?? "";
        for (const frame of frames) {
          const ev = parseSseFrame(frame);
          if (ev) opts.onEvent({ seq: ev.seq, kind: ev.type, data: ev.data as ChatStreamEvent["data"] });
        }
      }
    } finally {
      reader.cancel().catch(() => {});
    }
  }
```

- [ ] **Step 5: Add results, custom-fields, and tenant methods**

```ts
  // ---- discovery results ----
  getRunResults(runId: string, query: ResultsQuery = {}, signal?: AbortSignal) {
    return this.request<DiscoveryResult>(`/orchestration/runs/${runId}/results`, {
      query: query as Record<string, string | number | undefined>, signal,
    });
  }

  // ---- proprietary data: custom fields ----
  listCustomFields(entity?: string, signal?: AbortSignal) {
    return this.request<CustomFieldDef[]>("/custom-fields", { query: { entity }, signal });
  }
  createCustomField(body: CreateCustomFieldRequest, signal?: AbortSignal) {
    return this.request<CustomFieldDef>("/custom-fields", { method: "POST", body, signal });
  }
  deleteCustomField(id: string, signal?: AbortSignal) {
    return this.request<null>(`/custom-fields/${id}`, { method: "DELETE", signal });
  }
  importCustomFieldsCsv(
    args: { entity: string; matchColumn: string; mapping: Record<string, string>; file: File },
    signal?: AbortSignal,
  ) {
    const form = new FormData();
    form.set("entity", args.entity);
    form.set("match_column", args.matchColumn);
    form.set("mapping", JSON.stringify(args.mapping));
    form.set("file", args.file);
    return this.requestForm<CsvImportResult>("/custom-fields/import", form, signal);
  }

  // ---- cross-workspace switch ----
  listTenants(signal?: AbortSignal) {
    return this.request<TenantSummary[]>("/auth/tenants", { signal });
  }
  switchTenant(tenantId: string, signal?: AbortSignal) {
    return this.request<TokenResponse>("/auth/switch", {
      method: "POST", body: { tenant_id: tenantId }, signal,
    });
  }
```

(`TokenResponse` is already imported in `api.ts`.)

- [ ] **Step 6: Typecheck**

Run: `cd frontend && npm run typecheck`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/lib/api.ts
git commit -m "feat(fe): add chat/results/custom-fields/tenant API client methods"
```

---

## Task 3: Auth — switchTenant + tenant epoch

**Files:**
- Modify: `frontend/src/app/AuthContext.tsx`

- [ ] **Step 1: Extend the context API**

Add to the `AuthApi` interface:

```ts
  /** Re-issue a JWT for another tenant the user belongs to, then swap + refetch. */
  switchTenant: (tenantId: string) => Promise<void>;
  /** Increments on every tenant switch so screens can key off it to remount/refetch. */
  tenantEpoch: number;
```

- [ ] **Step 2: Implement it**

Add `const [tenantEpoch, setTenantEpoch] = useState(0);` near the session state, then:

```ts
  const switchTenant = useCallback(
    async (tenantId: string) => {
      const res = await api.switchTenant(tenantId);
      setSession({ token: res.access_token, role: res.role, tenantId: res.tenant_id });
      setTenantEpoch((n) => n + 1);
    },
    [api],
  );
```

Add `switchTenant` and `tenantEpoch` to the `useMemo` value object and its dependency array.

- [ ] **Step 3: Typecheck**

Run: `cd frontend && npm run typecheck`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/app/AuthContext.tsx
git commit -m "feat(fe): switchTenant action + tenantEpoch in AuthContext"
```

---

## Task 4: Shared chat components (thread + composer + hook)

> **Invoke `impeccable` before this task** (chat bubbles, chips, handoff card, streaming status are product UI). Honor the absolute bans (no glassmorphism default, no gradient text, no side-stripe borders). Use tokens only.

**Files:**
- Create: `frontend/src/components/chat/useChatSession.ts`
- Create: `frontend/src/components/chat/ChatThread.tsx` + `ChatThread.module.css`
- Create: `frontend/src/components/chat/ChatComposer.tsx` + `ChatComposer.module.css`
- Create: `frontend/src/components/chat/index.ts`

- [ ] **Step 1: `useChatSession` hook** — contract:

```ts
export interface UseChatSession {
  session: ChatSession | null;
  messages: ChatMessage[];        // ordered by seq, deduped
  loading: boolean;               // initial load
  error: ApiError | null;
  sending: boolean;               // a post is in flight
  streaming: boolean;             // SSE connected
  send: (content: string) => Promise<void>;
  reload: () => void;
}
export function useChatSession(sessionId: string): UseChatSession;
```

Implementation notes (mirror `useRunActivity` + `useApi`):
- Initial load via `getChatSession(sessionId)` → seed `session` + `messages`.
- `send(content)`: optimistic-append a local user message (`seq` = max+0.5 placeholder, `kind:"user"`), call `postChatMessage`; replace placeholder + append returned assistant messages, deduping by `id`/`seq`.
- SSE follow: after each settled turn (and on mount) open `streamChatEvents` from the last seen `seq`; on each event upsert a message by `id` (fallback `seq`). On close, if `session.status` is non-terminal and component is mounted, reconnect (debounced) — reuse the `epoch` pattern. Always abort on unmount.
- Dedupe helper: keep a `Map<string, ChatMessage>` keyed by `id`, sorted by `seq` for render.

- [ ] **Step 2: `ChatThread`** — props `{ messages: ChatMessage[]; onChip?: (text: string) => void; onOpenRun?: (runId: string) => void }`. Renders:
  - `user` → right-aligned bubble; `assistant`/`confirmation`/unknown → left bubble (content text).
  - `clarifying_question` → left bubble with the `content`, then a row of **suggestion chips** from `data.suggestions` (buttons; click → `onChip(text)`; keyboard-focusable, `aria-label`).
  - `run_launched` → a **handoff card**: "Discovery run started" + goal + a primary action "Open run console" → `onOpenRun(data.run_id)` (or `<Link to={/runs/${run_id}}>`).
  - Auto-scroll to bottom on new messages (respect `prefers-reduced-motion`: jump, don't smooth-scroll, when reduced).
  - Empty state: a friendly prompt ("Describe the companies or people you're looking for…").

- [ ] **Step 3: `ChatComposer`** — props `{ onSend: (text: string) => void; disabled?: boolean; busy?: boolean; placeholder?: string }`. A `Textarea` (auto-grow, max ~6 rows) + send `IconButton`/`Button`. Enter sends, Shift+Enter newline. Trims; ignores empty. Disabled while `busy`. Labelled (`aria-label="Message"`); visible focus ring.

- [ ] **Step 4: Barrel** `index.ts` re-exports `ChatThread`, `ChatComposer`, `useChatSession` + their prop types.

- [ ] **Step 5: Typecheck** — `cd frontend && npm run typecheck` → PASS.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/components/chat
git commit -m "feat(fe): reusable chat thread, composer, and session hook"
```

---

## Task 5: Workspace switcher (app shell)

> **Invoke `impeccable`** (app-shell control). Keep it quiet — a labelled trigger showing the current workspace + a dropdown; not a loud element.

**Files:**
- Create: `frontend/src/components/layout/WorkspaceSwitcher.tsx` + `.module.css`
- Modify: `frontend/src/components/layout/Topbar.tsx`

- [ ] **Step 1: `WorkspaceSwitcher`** — self-contained:
  - On mount, `useApi(() => api.listTenants())`. If `≤ 1` tenant → render the single workspace name as static text (no dropdown). Loading → small skeleton; error → silent fallback to current tenant name (don't block the shell).
  - Trigger button shows the current workspace name (match by `session.tenantId`), `aria-haspopup="menu"`, `aria-expanded`. Dropdown is a keyboard-navigable menu (`role="menu"`, arrow keys, Esc to close, focus trap-lite, click-outside close). Each item shows name + role badge; current item marked `aria-current`.
  - Selecting another → `await switchTenant(tenant_id)`; show a toast ("Switched to {name}"); close menu. The `tenantEpoch` bump (Task 6 wiring) triggers refetch. On error → toast error, stay put.
  - Dropdown must escape overflow (use the existing `Modal`/portal approach or `position: fixed`) per impeccable interaction rules — do NOT render an absolutely-positioned menu inside an `overflow:hidden` topbar.

- [ ] **Step 2: Mount in `Topbar`** — render `<WorkspaceSwitcher />` in the `.left` cluster after `<h1>`. Keep existing theme/logout/avatar controls.

- [ ] **Step 3: Force refetch on switch** — in `AppShell` (the routed layout), key the `<Outlet />`'s wrapper on `tenantEpoch` (e.g. `<div key={tenantEpoch}>`) so all child screens remount and refetch tenant-scoped data after a switch. Read `AppShell.tsx` first to place this correctly.

- [ ] **Step 4: Typecheck** — PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/layout/WorkspaceSwitcher.tsx frontend/src/components/layout/WorkspaceSwitcher.module.css frontend/src/components/layout/Topbar.tsx frontend/src/components/layout/AppShell.tsx
git commit -m "feat(fe): workspace switcher in app shell"
```

---

## Task 6: ChatPage — session rail + conversation

> **Invoke `impeccable`** (this is the flagship new screen). Three-pane-ish layout: left session rail, center conversation, bottom composer. Responsive: rail collapses to a top sheet/menu on narrow widths. Loading skeletons / empty / error on every pane.

**Files:**
- Create: `frontend/src/pages/ChatPage.tsx` + `.module.css`

- [ ] **Step 1: Routing model** — page reads `useParams<{ sessionId?: string }>()`. `/orchestrator` = no active session (show "new conversation" empty center). `/orchestrator/:sessionId` = active. Navigation via `useNavigate`.

- [ ] **Step 2: Session rail** — `useApi(() => api.listChatSessions())` keyed on `tenantEpoch`. Group into **Recent** (no `account_id`) and **By account/client** (has `account_id`, grouped/labelled by account). Each row: title + relative time + status. Active row highlighted (`aria-current`). "New conversation" button → creates a session (`createChatSession({})`) and navigates to it, OR routes to `/orchestrator` blank state that creates on first send (choose: create-on-first-send to avoid empty sessions; the blank center has its own composer that calls `createChatSession({ message })` then navigates to the new id).

- [ ] **Step 3: Conversation pane** — when `sessionId` present, use `useChatSession(sessionId)`; render `<ChatThread>` + `<ChatComposer>`. Wire `onChip` → fill composer (lift composer value or call `send` directly per UX — prefer filling so the user can edit). Wire `onOpenRun` → `navigate('/runs/' + runId)`. Show streaming/reconnecting status near the composer (reuse the dot+label pattern from `ActivityFeed`).

- [ ] **Step 4: Save-ICP affordance** — when `session.status` indicates a completed/launched ICP and the user is admin+ (`session.role` from auth ≥ admin), show a subtle "Save as ICP" action calling `api.saveChatIcp(id)` → toast on success. Hidden for managers/reps.

- [ ] **Step 5: States** — initial list loading → skeleton rows; empty → EmptyState ("Start your first discovery conversation"); error → ErrorState with retry. Conversation load/error handled likewise.

- [ ] **Step 6: Typecheck** — PASS.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/pages/ChatPage.tsx frontend/src/pages/ChatPage.module.css
git commit -m "feat(fe): orchestrator ChatPage (session rail + conversation)"
```

---

## Task 7: Account → orchestrator entry

**Files:**
- Modify: `frontend/src/pages/AccountDetailPage.tsx`

- [ ] **Step 1:** Read `AccountDetailPage.tsx` to find the header actions area. Add a manager+ action "Ask the orchestrator about {account.name}" (gate via `session.role` rank ≥ manager, matching existing patterns on the page). On click: `const { session: s } = await api.createChatSession({ account_id: account.id });` then `navigate('/orchestrator/' + s.id)`. Busy state + error toast.

- [ ] **Step 2: Typecheck** — PASS.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/pages/AccountDetailPage.tsx
git commit -m "feat(fe): launch orchestrator from account detail"
```

---

## Task 8: Discovery results panel

> **Invoke `impeccable`** (data table is a core product surface). Reuse the `ScoreMeter` visual from `RunDetailPage` (extract it to a shared `components/ui` primitive `ScoreMeter` if clean, or re-create locally — prefer extracting). Use the existing `DataTable` primitive where it fits; dynamic columns require a flexible render.

**Files:**
- Create: `frontend/src/components/discovery/ResultsPanel.tsx` + `.module.css`
- Create: `frontend/src/components/discovery/index.ts`
- (Optional) Create: `frontend/src/components/ui/ScoreMeter.tsx` + export from `ui/index.ts`; refactor `RunDetailPage` to use it.

- [ ] **Step 1: Component contract** — `<ResultsPanel runId={string} onImport?={() => void} />`.
  - Fetch: `useApi(() => api.getRunResults(runId, query), [runId, queryKey])`. `query` from filter state.
  - Columns (fixed): name; domain (account) / email+title (contact); **Fit** (ScoreMeter + number); fit reasons (truncated, tooltip/expand); source badge (`own` = neutral, `discovery`/`is_new` = info "New"). Then **dynamic columns** from `result.columns` rendering `candidate.custom_fields[col.key]`.
  - Filters bar: fit threshold (range/number `min_fit`), source select (all/own/new→`discovery`), free-text `q` (debounced ~300ms), and a per-custom-field text filter that emits `cf_<key>` query params. Filters drive server-side fetch (the backend already filters/paginates).
  - Pagination: `limit`/`offset`, show `total` and `counts` (own/new). Prev/Next.

- [ ] **Step 2: Row actions** — **Research** button per `account` row → `api.createRun({ goal: "research_account", account_id: candidate.id })` then `navigate('/runs/' + run.id)` (manager+). Multi-select checkboxes → "Add to list" using existing `api.buildList(name, filter)` (reuse Lists; build a filter from selected ids if supported, else map to the Lists flow). Disable Research for `contact` rows.

- [ ] **Step 3: States** — loading → skeleton rows (match column count); empty → EmptyState ("No matches yet — refine the ICP in chat"); error → ErrorState + retry. Reduced-motion safe.

- [ ] **Step 4: Barrel** `index.ts` exports `ResultsPanel`.

- [ ] **Step 5: Typecheck** — PASS.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/components/discovery frontend/src/components/ui/ScoreMeter.tsx frontend/src/components/ui/index.ts frontend/src/pages/RunDetailPage.tsx
git commit -m "feat(fe): discovery results panel with dynamic custom-field columns"
```

---

## Task 9: Run console — mini-chat + results integration

> **Invoke `impeccable`** for the docked panel layout.

**Files:**
- Modify: `frontend/src/pages/RunDetailPage.tsx`

- [ ] **Step 1: Results panel** — in `RunConsole`, when `run.goal === "discover"` or `run.blackboard.discovery` is present, render `<ResultsPanel runId={run.id} />` as a full-width section above or replacing the generic Intelligence panel (discovery runs have no draft/approval). Keep the existing steps/feed layout for non-discovery runs.

- [ ] **Step 2: Docked mini-chat** — when `run.chat_session_id` is set, render a docked panel (right rail on wide, collapsible drawer on narrow) using `useChatSession(run.chat_session_id)` + `<ChatThread>` + `<ChatComposer>`. Hidden entirely when no session. The mini-chat shares the same components as ChatPage (no duplication).

- [ ] **Step 3: States** — mini-chat handles its own loading/empty/error; results panel as Task 8.

- [ ] **Step 4: Typecheck** — PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/RunDetailPage.tsx
git commit -m "feat(fe): dock mini-chat + results in the run console"
```

---

## Task 10: CSV proprietary-data import modal

> **Invoke `impeccable`** for the modal/wizard. Use the existing `Modal` primitive.

**Files:**
- Create: `frontend/src/lib/csv.ts`
- Create: `frontend/src/components/discovery/ImportCsvModal.tsx` + `.module.css`
- Modify: `frontend/src/components/discovery/index.ts` (export modal)

- [ ] **Step 1: CSV parser** `frontend/src/lib/csv.ts` — dependency-free:

```ts
/** Parse CSV text into header + rows. Handles quoted fields, embedded commas/quotes, CRLF. */
export function parseCsv(text: string): { headers: string[]; rows: string[][] } { /* … */ }
```

Implement a small state-machine parser (quote toggling, `""` escape, `\r\n`/`\n` line breaks). Keep it bounded; this is for preview (first ~50 rows) — the actual import sends the raw `File` to the server.

- [ ] **Step 2: Modal flow** — `<ImportCsvModal open entity={"account"|"contact"} existingFields={CustomFieldDef[]} onClose onImported={(CsvImportResult) => void} />`:
  1. **Drop/select** a `.csv` (drag-drop zone + file input fallback; validate extension/size).
  2. **Preview** — parse client-side; show headers + first rows in a table.
  3. **Map** — for each CSV column: ignore / map to an existing field (select) / create a new field (label + key + kind → calls `api.createCustomField` on import, or pass through `mapping` so the server auto-creates). Pick the **match key** column (`domain` for accounts, `email` for contacts) via a select defaulting to a column named accordingly.
  4. **Import** — `api.importCustomFieldsCsv({ entity, matchColumn, mapping, file })`; show busy.
  5. **Result** — summary: matched / updated / created_fields / skipped. "Done" closes + calls `onImported` so the results table refetches new columns.

- [ ] **Step 3: Entry point** — surface an "Import data" button in `ResultsPanel`'s header (admin+ only via `session.role`) that opens the modal with the right `entity` (account if target=companies, contact if contacts) and `existingFields` from `api.listCustomFields(entity)`. On `onImported`, refetch results.

- [ ] **Step 4: States** — disabled/busy during import; invalid-file and server-error inline messages; reduced-motion modal transition fallback.

- [ ] **Step 5: Typecheck** — PASS.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/lib/csv.ts frontend/src/components/discovery/ImportCsvModal.tsx frontend/src/components/discovery/ImportCsvModal.module.css frontend/src/components/discovery/index.ts frontend/src/components/discovery/ResultsPanel.tsx
git commit -m "feat(fe): CSV proprietary-data import modal"
```

---

## Task 11: Routes + nav

**Files:**
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/app/nav.tsx`
- Modify: `frontend/src/components/ui/icons.tsx` (only if a suitable icon is missing)

- [ ] **Step 1: Lazy page + routes** — in `App.tsx`:

```ts
const ChatPage = lazyPage(() => import("@/pages/ChatPage"), "ChatPage");
```

Add inside the authed `AppShell` route group:

```tsx
<Route path="/orchestrator" element={<RequireRole minRole="manager"><ChatPage /></RequireRole>} />
<Route path="/orchestrator/:sessionId" element={<RequireRole minRole="manager"><ChatPage /></RequireRole>} />
```

- [ ] **Step 2: Nav item** — in `nav.tsx`, add (place near AI Runs):

```tsx
{ to: "/orchestrator", label: "Orchestrator", icon: <SparklesIcon />, minRole: "manager" },
```

Use an existing icon if `SparklesIcon` is absent (e.g. `TargetIcon` or `WorkflowIcon`); add a new icon to `icons.tsx` only if needed.

- [ ] **Step 3: Typecheck** — PASS.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/App.tsx frontend/src/app/nav.tsx frontend/src/components/ui/icons.tsx
git commit -m "feat(fe): orchestrator route + nav item"
```

---

## Task 12: Full verification

- [ ] **Step 1: Typecheck** — `cd frontend && npm run typecheck` → PASS (0 errors).
- [ ] **Step 2: Build** — `cd frontend && npm run build` → emits to `nexus/web/dist`, 0 errors/warnings of substance.
- [ ] **Step 3: Backend suite still green** — `python -m pytest -q` (Task 1 may have touched `RunOut`) → all pass.
- [ ] **Step 4: In-browser e2e** (preview harness / running service at `http://127.0.0.1:8000`): sign in (manager+ demo account) → open **Orchestrator** → state an ICP → answer a clarifying question via a chip → launch → handoff card → open run console → results render → filter by fit/source/text → **Import data** CSV adds a column that appears in the table → switch workspace shows isolated data and the rail/results refetch. Capture screenshots; fix any contrast/focus/keyboard/empty-state gaps per `impeccable` `audit`.
- [ ] **Step 5: Finish** — invoke `superpowers:finishing-a-development-branch` (verify tests → choose merge/PR/keep/discard → cleanup).

---

## Self-review checklist (run before execution)

1. **Spec §8 coverage:** 8.1 ChatPage → Tasks 6–7 ✓; 8.2 mini-chat → Task 9 ✓; 8.3 results panel → Task 8 ✓; 8.4 CSV modal → Task 10 ✓; 8.5 switcher → Task 5 ✓; 8.6 client wiring → Tasks 1–2 ✓. §5 switch endpoints → Tasks 2–3, 5 ✓.
2. **Contract fidelity:** every method/type matches the confirmed backend shapes above (chat query param is `status_filter`; import is multipart with `mapping` JSON; switch body is `{tenant_id}`; results candidate is a flat dict).
3. **Reuse over rebuild:** ChatThread/Composer/useChatSession shared by ChatPage + mini-chat; SSE mirrors `streamRunEvents`; ScoreMeter extracted; Lists reused for "Add to list"; existing `Modal`/`DataTable`/`DataState`/`useApi` reused.
4. **A11y + states:** every screen lists loading/empty/error + keyboard/focus/reduced-motion. `impeccable` invoked before each UI task.
5. **No new deps; tokens only; absolute bans respected.**
