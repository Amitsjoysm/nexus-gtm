/**
 * Typed API client for the NEXUS backend.
 *
 * Design:
 * - A single `ApiClient` owns the base URL, the auth token, and JSON (de)serialization.
 * - Every method returns a typed promise; failures throw `ApiError` carrying the HTTP
 *   status and the backend `detail` message so the UI can render real error states.
 * - `onUnauthorized` lets the auth layer react to 401s (e.g. log the user out).
 */
import type {
  Account,
  AccountInput,
  AgentRunResponse,
  Alert,
  AlertStatus,
  AnalyticsOverview,
  Approval,
  ApprovalDecisionRequest,
  ApprovalStatus,
  ChatSession,
  ChatStreamEvent,
  ChatTurnResponse,
  Contact,
  CreateCustomFieldRequest,
  CreateSessionRequest,
  LookalikeResponse,
  CRMPushResponse,
  CRMSyncRequest,
  CRMSyncResponse,
  CsvImportResult,
  CustomFieldDef,
  DiscoveryResult,
  InboxTask,
  ListBuildResult,
  ListFilter,
  LoginRequest,
  Member,
  Play,
  PlayInput,
  RelevanceProfile,
  RelevanceProfileInput,
  ResultsQuery,
  Role,
  Run,
  RunCreateRequest,
  RunStreamEvent,
  SaveIcpResponse,
  SEPPushRequest,
  SEPPushResponse,
  SignalEvent,
  SignupRequest,
  TenantSummary,
  TokenResponse,
  Workspace,
} from "./types";

export class ApiError extends Error {
  status: number;
  detail: string;
  constructor(status: number, detail: string) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

interface RequestOptions {
  method?: string;
  body?: unknown;
  query?: Record<string, string | number | boolean | undefined | null>;
  signal?: AbortSignal;
}

export class ApiClient {
  private baseUrl: string;
  private token: string | null = null;
  private onUnauthorized?: () => void;

  constructor(baseUrl = "/api", onUnauthorized?: () => void) {
    this.baseUrl = baseUrl;
    this.onUnauthorized = onUnauthorized;
  }

  setToken(token: string | null): void {
    this.token = token;
  }

  private buildUrl(path: string, query?: RequestOptions["query"]): string {
    const url = new URL(this.baseUrl + path, window.location.origin);
    if (query) {
      for (const [k, v] of Object.entries(query)) {
        if (v !== undefined && v !== null && v !== "") url.searchParams.set(k, String(v));
      }
    }
    return url.pathname + url.search;
  }

  private async request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
    const headers: Record<string, string> = {};
    if (opts.body !== undefined) headers["Content-Type"] = "application/json";
    if (this.token) headers["Authorization"] = `Bearer ${this.token}`;

    let res: Response;
    try {
      res = await fetch(this.buildUrl(path, opts.query), {
        method: opts.method ?? "GET",
        headers,
        body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
        signal: opts.signal,
      });
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

  // ---- auth ----
  signup(body: SignupRequest, signal?: AbortSignal) {
    return this.request<TokenResponse>("/auth/signup", { method: "POST", body, signal });
  }
  login(body: LoginRequest, signal?: AbortSignal) {
    return this.request<TokenResponse>("/auth/login", { method: "POST", body, signal });
  }

  // ---- accounts ----
  listAccounts(signal?: AbortSignal) {
    return this.request<Account[]>("/accounts", { signal });
  }
  getAccount(id: string, signal?: AbortSignal) {
    return this.request<Account>(`/accounts/${id}`, { signal });
  }
  createAccount(body: AccountInput, signal?: AbortSignal) {
    return this.request<Account>("/accounts", { method: "POST", body, signal });
  }
  listContacts(accountId: string, signal?: AbortSignal) {
    return this.request<Contact[]>(`/accounts/${accountId}/contacts`, { signal });
  }
  findLookalikes(accountId: string, limit = 10, signal?: AbortSignal) {
    return this.request<LookalikeResponse>(
      `/accounts/${accountId}/lookalikes?limit=${limit}`,
      { method: "POST", signal },
    );
  }

  // ---- agents ----
  listAgents(signal?: AbortSignal) {
    return this.request<string[]>("/agents", { signal });
  }
  runAgent(
    name: string,
    accountId: string | null,
    inputs: Record<string, unknown> = {},
    signal?: AbortSignal,
  ) {
    return this.request<AgentRunResponse>(`/agents/${name}/run`, {
      method: "POST",
      body: { account_id: accountId, inputs },
      signal,
    });
  }
  runPipeline(accountId: string, signal?: AbortSignal) {
    return this.request<unknown>(`/agents/pipeline/${accountId}`, { method: "POST", signal });
  }

  // ---- relevance / ICP ----
  getRelevanceProfile(signal?: AbortSignal) {
    return this.request<RelevanceProfile>("/relevance/profile", { signal });
  }
  updateRelevanceProfile(body: RelevanceProfileInput, signal?: AbortSignal) {
    return this.request<RelevanceProfile>("/relevance/profile", {
      method: "PUT",
      body,
      signal,
    });
  }

  // ---- lists / segments ----
  previewList(filter: ListFilter, signal?: AbortSignal) {
    return this.request<Account[]>("/lists/preview", {
      method: "POST",
      body: { name: "preview", filter },
      signal,
    });
  }
  buildList(name: string, filter: ListFilter, signal?: AbortSignal) {
    return this.request<ListBuildResult>("/lists", {
      method: "POST",
      body: { name, filter },
      signal,
    });
  }

  // ---- plays ----
  listPlays(signal?: AbortSignal) {
    return this.request<Play[]>("/plays", { signal });
  }
  createPlay(body: PlayInput, signal?: AbortSignal) {
    return this.request<Play>("/plays", { method: "POST", body, signal });
  }

  // ---- integrations ----
  crmSync(body: CRMSyncRequest, signal?: AbortSignal) {
    return this.request<CRMSyncResponse>("/integrations/crm/sync", {
      method: "POST",
      body,
      signal,
    });
  }
  crmPush(accountId: string, signal?: AbortSignal) {
    return this.request<CRMPushResponse>(
      `/integrations/crm/push/${accountId}`,
      { method: "POST", signal },
    );
  }
  sepPush(body: SEPPushRequest, signal?: AbortSignal) {
    return this.request<SEPPushResponse>("/integrations/sep/push", {
      method: "POST",
      body,
      signal,
    });
  }

  // ---- inbox ----
  listInbox(signal?: AbortSignal) {
    return this.request<InboxTask[]>("/inbox", { signal });
  }
  completeTask(id: string, signal?: AbortSignal) {
    return this.request<InboxTask>(`/inbox/${id}/complete`, { method: "POST", signal });
  }

  // ---- signals ----
  listSignals(
    params: { account_id?: string; kind?: string; limit?: number; offset?: number } = {},
    signal?: AbortSignal,
  ) {
    return this.request<SignalEvent[]>("/signals", { query: params, signal });
  }

  // ---- alerts ----
  listAlerts(status?: AlertStatus, signal?: AbortSignal) {
    return this.request<Alert[]>("/alerts", { query: { status }, signal });
  }
  ackAlert(id: string, signal?: AbortSignal) {
    return this.request<Alert>(`/alerts/${id}/ack`, { method: "POST", signal });
  }

  // ---- analytics ----
  analyticsOverview(signal?: AbortSignal) {
    return this.request<AnalyticsOverview>("/analytics/overview", { signal });
  }

  // ---- workspace / members ----
  listMembers(signal?: AbortSignal) {
    return this.request<Member[]>("/workspace/members", { signal });
  }
  inviteMember(
    body: { email: string; full_name: string; password: string; role: Role },
    signal?: AbortSignal,
  ) {
    return this.request<Member>("/workspace/members", { method: "POST", body, signal });
  }
  changeMemberRole(membershipId: string, role: Role, signal?: AbortSignal) {
    return this.request<Member>(`/workspace/members/${membershipId}/role`, {
      method: "PUT",
      body: { role },
      signal,
    });
  }
  removeMember(membershipId: string, signal?: AbortSignal) {
    return this.request<null>(`/workspace/members/${membershipId}`, {
      method: "DELETE",
      signal,
    });
  }
  listWorkspaces(signal?: AbortSignal) {
    return this.request<Workspace[]>("/workspace/workspaces", { signal });
  }

  // ---- orchestration ----
  createRun(body: RunCreateRequest, signal?: AbortSignal) {
    return this.request<Run>("/orchestration/runs", { method: "POST", body, signal });
  }
  listRuns(signal?: AbortSignal) {
    return this.request<Run[]>("/orchestration/runs", { signal });
  }
  getRun(id: string, signal?: AbortSignal) {
    return this.request<Run>(`/orchestration/runs/${id}`, { signal });
  }
  cancelRun(id: string, signal?: AbortSignal) {
    return this.request<Run>(`/orchestration/runs/${id}/cancel`, { method: "POST", signal });
  }
  listApprovals(status?: ApprovalStatus, signal?: AbortSignal) {
    return this.request<Approval[]>("/orchestration/approvals", {
      query: { status_filter: status },
      signal,
    });
  }
  decideApproval(id: string, body: ApprovalDecisionRequest, signal?: AbortSignal) {
    return this.request<Run>(`/orchestration/approvals/${id}/decision`, {
      method: "POST",
      body,
      signal,
    });
  }

  // ---- orchestrator chat ----
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

  /**
   * Stream a run's append-only event log over SSE.
   *
   * `EventSource` can't attach an Authorization header, so we read the stream by hand via
   * `fetch` + a `ReadableStream` reader and parse SSE frames. Resumable: pass `lastEventId`
   * (the highest `seq` seen) and the server replays only newer events. The promise resolves
   * when the server closes the stream — at a terminal run, or when it parks at an approval
   * (reconnect after deciding to pick up the rest).
   */
  async streamRunEvents(
    runId: string,
    opts: { onEvent: (event: RunStreamEvent) => void; lastEventId?: number },
    signal?: AbortSignal,
  ): Promise<void> {
    const headers: Record<string, string> = { Accept: "text/event-stream" };
    if (this.token) headers["Authorization"] = `Bearer ${this.token}`;
    if (opts.lastEventId) headers["Last-Event-ID"] = String(opts.lastEventId);

    const res = await fetch(this.buildUrl(`/orchestration/runs/${runId}/events`), {
      headers,
      signal,
    });
    if (res.status === 401) this.onUnauthorized?.();
    if (!res.ok || !res.body) {
      throw new ApiError(res.status, res.statusText || "Couldn't open the event stream");
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    try {
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        // SSE frames are separated by a blank line. Keep the trailing partial in the buffer.
        const frames = buffer.split("\n\n");
        buffer = frames.pop() ?? "";
        for (const frame of frames) {
          const event = parseSseFrame(frame);
          if (event) opts.onEvent(event);
        }
      }
    } finally {
      reader.cancel().catch(() => {});
    }
  }
}

/** Parse a single SSE frame ("id:/event:/data:" lines) into a typed event, or null. */
function parseSseFrame(frame: string): RunStreamEvent | null {
  let seq = 0;
  let type = "message";
  const dataLines: string[] = [];
  for (const raw of frame.split("\n")) {
    const line = raw.replace(/\r$/, "");
    if (!line || line.startsWith(":")) continue; // blank or comment/heartbeat
    const idx = line.indexOf(":");
    const field = idx === -1 ? line : line.slice(0, idx);
    const val = idx === -1 ? "" : line.slice(idx + 1).replace(/^ /, "");
    if (field === "id") seq = Number(val) || 0;
    else if (field === "event") type = val;
    else if (field === "data") dataLines.push(val);
  }
  if (dataLines.length === 0) return null;
  let data: Record<string, unknown> = {};
  try {
    const parsed = JSON.parse(dataLines.join("\n"));
    if (parsed && typeof parsed === "object") data = parsed as Record<string, unknown>;
  } catch {
    data = { raw: dataLines.join("\n") };
  }
  return { seq, type, data };
}

function safeJsonParse(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}
