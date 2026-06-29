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
  ActivityItem,
  AgentRunResponse,
  Alert,
  AlertStatus,
  AnalyticsOverview,
  Approval,
  ApprovalDecisionRequest,
  ApprovalStatus,
  AutomationSettings,
  EmailAccount,
  EmailAccountInput,
  EmailSettings,
  EmailSettingsInput,
  EmailTestResult,
  Mailbox,
  WorkspaceContact,
  ReverifyResult,
  CallTask,
  CallScript,
  CallActivity,
  CallBrief,
  Cadence,
  CadenceEnrollment,
  CadenceInput,
  CadenceReport,
  Campaign,
  CampaignDetail,
  CampaignInput,
  CampaignPreview,
  CampaignProgress,
  ChatSession,
  ChatStreamEvent,
  ChatTurnResponse,
  Contact,
  CreateCustomFieldRequest,
  CreateSessionRequest,
  LaunchFromSelectionInput,
  LookalikeResponse,
  ContactLookalikeResponse,
  CRMPushResponse,
  CRMSyncRequest,
  CRMSyncResponse,
  CRMSyncStatus,
  CsvImportResult,
  CustomFieldDef,
  DiscoveryResult,
  EnrollmentDetail,
  InboxTask,
  LearnedWeights,
  ListBuildResult,
  ListFilter,
  LoginRequest,
  Member,
  NewWorkspaceRequest,
  Outcome,
  OutcomeInput,
  OutcomeSummary,
  Play,
  PlayInput,
  ProspectList,
  RelevanceProfile,
  RelevanceProfileInput,
  ResultsQuery,
  Role,
  Run,
  RunCreateRequest,
  RunStreamEvent,
  SaveIcpResponse,
  SEPPushRequest,
  MessageResponse,
  RegisterStartResponse,
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
  /** Step 1 of OTP registration: validate + email a code (no account created yet). */
  registerStart(body: SignupRequest, signal?: AbortSignal) {
    return this.request<RegisterStartResponse>("/auth/register/start", {
      method: "POST",
      body,
      signal,
    });
  }
  /** Re-send the verification code for an in-flight registration (cooldown-limited). */
  registerResend(body: { email: string }, signal?: AbortSignal) {
    return this.request<RegisterStartResponse>("/auth/register/resend", {
      method: "POST",
      body,
      signal,
    });
  }
  /** Step 2 of OTP registration: verify the code and provision the account. */
  registerVerify(body: { email: string; code: string }, signal?: AbortSignal) {
    return this.request<TokenResponse>("/auth/register/verify", { method: "POST", body, signal });
  }
  login(body: LoginRequest, signal?: AbortSignal) {
    return this.request<TokenResponse>("/auth/login", { method: "POST", body, signal });
  }
  /** Request a password-reset link (generic response — never reveals if the email exists). */
  forgotPassword(body: { email: string }, signal?: AbortSignal) {
    return this.request<MessageResponse>("/auth/forgot-password", { method: "POST", body, signal });
  }
  /** Complete a password reset with the emailed token. */
  resetPassword(
    body: { email: string; token: string; new_password: string },
    signal?: AbortSignal,
  ) {
    return this.request<MessageResponse>("/auth/reset-password", { method: "POST", body, signal });
  }

  // ---- accounts ----
  listAccounts(signal?: AbortSignal) {
    return this.request<Account[]>("/accounts", { signal });
  }
  listWorkspaceContacts(q?: string, signal?: AbortSignal) {
    return this.request<WorkspaceContact[]>("/contacts", {
      query: q ? { q } : undefined,
      signal,
    });
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
  enrichContact(contactId: string, signal?: AbortSignal) {
    return this.request<Contact>(`/accounts/contacts/${contactId}/enrich`, {
      method: "POST",
      signal,
    });
  }
  reverifyContacts(onlyUnverified = true, signal?: AbortSignal) {
    return this.request<ReverifyResult>("/contacts/reverify", {
      method: "POST",
      query: { only_unverified: String(onlyUnverified) },
      signal,
    });
  }
  // ---- Cold calling --------------------------------------------------------------------
  callQueue(status = "open", mine = false, signal?: AbortSignal) {
    return this.request<CallTask[]>("/calling/queue", {
      query: { status_: status, mine: String(mine) },
      signal,
    });
  }
  createCallTask(
    body: { account_id: string; contact_id?: string | null; reason?: string; priority?: number },
    signal?: AbortSignal,
  ) {
    return this.request<CallTask>("/calling/tasks", { method: "POST", body, signal });
  }
  generateCallScript(taskId: string, signal?: AbortSignal) {
    return this.request<CallScript>(`/calling/tasks/${taskId}/script`, { method: "POST", signal });
  }
  callBrief(taskId: string, signal?: AbortSignal) {
    return this.request<CallBrief>(`/calling/tasks/${taskId}/brief`, { signal });
  }
  logCallDisposition(
    taskId: string,
    body: { disposition: string; notes?: string; duration_s?: number | null; next_step?: string | null },
    signal?: AbortSignal,
  ) {
    return this.request<CallActivity>(`/calling/tasks/${taskId}/disposition`, {
      method: "POST",
      body,
      signal,
    });
  }
  skipCallTask(taskId: string, signal?: AbortSignal) {
    return this.request<CallTask>(`/calling/tasks/${taskId}/skip`, { method: "POST", signal });
  }
  contactCallActivities(contactId: string, signal?: AbortSignal) {
    return this.request<CallActivity[]>(`/calling/contacts/${contactId}/activities`, { signal });
  }
  enrichAccount(accountId: string, signal?: AbortSignal) {
    return this.request<Account>(`/accounts/${accountId}/enrich`, { method: "POST", signal });
  }
  archiveAccount(accountId: string, signal?: AbortSignal) {
    return this.request<Account>(`/accounts/${accountId}/archive`, { method: "POST", signal });
  }
  findLookalikes(accountId: string, limit = 10, signal?: AbortSignal) {
    return this.request<LookalikeResponse>(
      `/accounts/${accountId}/lookalikes?limit=${limit}`,
      { method: "POST", signal },
    );
  }
  /** Find people in the workspace who resemble this contact (role/seniority/dept + company). */
  findContactLookalikes(contactId: string, limit = 10, signal?: AbortSignal) {
    return this.request<ContactLookalikeResponse>(
      `/accounts/contacts/${contactId}/lookalikes?limit=${limit}`,
      { method: "POST", signal },
    );
  }
  /** Source the buying committee for an account; returns the contacts newly added. */
  sourceContacts(accountId: string, limit = 5, signal?: AbortSignal) {
    return this.request<Contact[]>(`/accounts/${accountId}/source-contacts?limit=${limit}`, {
      method: "POST",
      signal,
    });
  }
  /** Add a lookalike to tracked accounts and score it against the ICP. */
  addFromLookalike(
    body: { name: string; domain?: string | null; industry?: string | null },
    signal?: AbortSignal,
  ) {
    return this.request<Account>("/accounts/from-lookalike", { method: "POST", body, signal });
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
  listSavedLists(signal?: AbortSignal) {
    return this.request<ProspectList[]>("/lists", { signal });
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
  crmSyncStatus(signal?: AbortSignal) {
    return this.request<CRMSyncStatus>("/integrations/crm/sync-status", { signal });
  }

  // ---- inbox ----
  listInbox(status?: string, signal?: AbortSignal) {
    return this.request<InboxTask[]>("/inbox", { query: status ? { status } : undefined, signal });
  }
  completeTask(id: string, signal?: AbortSignal) {
    return this.request<InboxTask>(`/inbox/${id}/complete`, { method: "POST", signal });
  }
  reopenTask(id: string, signal?: AbortSignal) {
    return this.request<InboxTask>(`/inbox/${id}/reopen`, { method: "POST", signal });
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
  analyticsActivity(limit = 20, signal?: AbortSignal) {
    return this.request<ActivityItem[]>("/analytics/activity", { query: { limit }, signal });
  }

  // ---- segment campaigns ----
  listCampaigns(signal?: AbortSignal) {
    return this.request<Campaign[]>("/campaigns", { signal });
  }
  getCampaign(id: string, signal?: AbortSignal) {
    return this.request<CampaignDetail>(`/campaigns/${id}`, { signal });
  }
  createCampaign(body: CampaignInput, signal?: AbortSignal) {
    return this.request<Campaign>("/campaigns", { method: "POST", body, signal });
  }
  launchFromSelection(body: LaunchFromSelectionInput, signal?: AbortSignal) {
    return this.request<Campaign>("/campaigns/launch-from-selection", {
      method: "POST",
      body,
      signal,
    });
  }
  previewCampaign(id: string, signal?: AbortSignal) {
    return this.request<CampaignPreview>(`/campaigns/${id}/preview`, { signal });
  }
  approveCampaign(id: string, signal?: AbortSignal) {
    return this.request<Campaign>(`/campaigns/${id}/approve`, { method: "POST", signal });
  }
  cancelCampaign(id: string, signal?: AbortSignal) {
    return this.request<Campaign>(`/campaigns/${id}/cancel`, { method: "POST", signal });
  }

  // ---- cadences ----
  listCadences(signal?: AbortSignal) {
    return this.request<Cadence[]>("/cadences", { signal });
  }
  getCadence(id: string, signal?: AbortSignal) {
    return this.request<Cadence>(`/cadences/${id}`, { signal });
  }
  createCadence(body: CadenceInput, signal?: AbortSignal) {
    return this.request<Cadence>("/cadences", { method: "POST", body, signal });
  }
  deactivateCadence(id: string, signal?: AbortSignal) {
    return this.request<null>(`/cadences/${id}`, { method: "DELETE", signal });
  }
  listEnrollments(campaignId: string, signal?: AbortSignal) {
    return this.request<CadenceEnrollment[]>(
      `/campaigns/${campaignId}/enrollments`,
      { signal },
    );
  }
  getEnrollment(id: string, signal?: AbortSignal) {
    return this.request<EnrollmentDetail>(`/enrollments/${id}`, { signal });
  }
  cadenceReport(campaignId: string, signal?: AbortSignal) {
    return this.request<CadenceReport>(`/campaigns/${campaignId}/cadence-report`, { signal });
  }
  pauseEnrollment(id: string, signal?: AbortSignal) {
    return this.request<CadenceEnrollment>(`/enrollments/${id}/pause`, { method: "POST", signal });
  }
  resumeEnrollment(id: string, signal?: AbortSignal) {
    return this.request<CadenceEnrollment>(`/enrollments/${id}/resume`, { method: "POST", signal });
  }
  stopEnrollment(id: string, signal?: AbortSignal) {
    return this.request<CadenceEnrollment>(`/enrollments/${id}/stop`, { method: "POST", signal });
  }
  approveTouch(enrollmentId: string, stepIndex: number, editedBody?: string, signal?: AbortSignal) {
    return this.request<CadenceEnrollment>(
      `/enrollments/${enrollmentId}/touches/${stepIndex}/approve`,
      { method: "POST", body: { edited_body: editedBody ?? null }, signal },
    );
  }
  rejectTouch(enrollmentId: string, stepIndex: number, stop = false, signal?: AbortSignal) {
    return this.request<CadenceEnrollment>(
      `/enrollments/${enrollmentId}/touches/${stepIndex}/reject`,
      { method: "POST", body: { stop }, signal },
    );
  }

  // ---- outcome-feedback loop ----
  recordOutcome(body: OutcomeInput, signal?: AbortSignal) {
    return this.request<Outcome>("/outcomes", { method: "POST", body, signal });
  }
  listOutcomes(limit = 50, signal?: AbortSignal) {
    return this.request<Outcome[]>("/outcomes", { query: { limit }, signal });
  }
  getLearnedWeights(signal?: AbortSignal) {
    return this.request<LearnedWeights>("/outcomes/weights", { signal });
  }
  getOutcomeSummary(signal?: AbortSignal) {
    return this.request<OutcomeSummary>("/outcomes/summary", { signal });
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
  getAutomation(signal?: AbortSignal) {
    return this.request<AutomationSettings>("/workspace/automation", { signal });
  }
  setAutomation(enabled: boolean, signal?: AbortSignal) {
    return this.request<AutomationSettings>("/workspace/automation", {
      method: "PATCH",
      body: { automation_enabled: enabled },
      signal,
    });
  }
  getEmailSettings(signal?: AbortSignal) {
    return this.request<EmailSettings>("/workspace/email", { signal });
  }
  setEmailSettings(body: EmailSettingsInput, signal?: AbortSignal) {
    return this.request<EmailSettings>("/workspace/email", { method: "PUT", body, signal });
  }
  testEmailSettings(to?: string, signal?: AbortSignal) {
    return this.request<EmailTestResult>("/workspace/email/test", {
      method: "POST",
      body: { to: to ?? null },
      signal,
    });
  }

  // ---- sending mailboxes (multi-account SMTP) ----
  listEmailAccounts(signal?: AbortSignal) {
    return this.request<EmailAccount[]>("/workspace/email/accounts", { signal });
  }
  addEmailAccount(body: EmailAccountInput, signal?: AbortSignal) {
    return this.request<EmailAccount>("/workspace/email/accounts", { method: "POST", body, signal });
  }
  updateEmailAccount(id: string, body: EmailAccountInput, signal?: AbortSignal) {
    return this.request<EmailAccount>(`/workspace/email/accounts/${id}`, {
      method: "PUT",
      body,
      signal,
    });
  }
  deleteEmailAccount(id: string, signal?: AbortSignal) {
    return this.request<void>(`/workspace/email/accounts/${id}`, { method: "DELETE", signal });
  }
  setDefaultEmailAccount(id: string, signal?: AbortSignal) {
    return this.request<EmailAccount>(`/workspace/email/accounts/${id}/default`, {
      method: "POST",
      signal,
    });
  }
  testEmailAccount(id: string, to?: string, signal?: AbortSignal) {
    return this.request<EmailTestResult>(`/workspace/email/accounts/${id}/test`, {
      method: "POST",
      body: { to: to ?? null },
      signal,
    });
  }
  /** Send-ready mailboxes for the approval gate (approvers, not only admins). */
  listMailboxes(signal?: AbortSignal) {
    return this.request<Mailbox[]>("/workspace/email/mailboxes", { signal });
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
  redraftApproval(id: string, instructions: string, signal?: AbortSignal) {
    return this.request<Approval>(`/orchestration/approvals/${id}/redraft`, {
      method: "POST",
      body: { instructions },
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
  createWorkspace(body: NewWorkspaceRequest, signal?: AbortSignal) {
    return this.request<TokenResponse>("/auth/workspaces", { method: "POST", body, signal });
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

  /**
   * Stream a campaign's draft/send progress over SSE. Same hand-rolled fetch+reader as
   * `streamRunEvents` (EventSource can't send Authorization). The server emits `progress`
   * frames (status + per-status target counts) and closes at a terminal status or the
   * approval gate — at which point the promise resolves and the caller refetches once.
   */
  async streamCampaignEvents(
    campaignId: string,
    opts: { onProgress: (p: CampaignProgress) => void; lastEventId?: number },
    signal?: AbortSignal,
  ): Promise<void> {
    const headers: Record<string, string> = { Accept: "text/event-stream" };
    if (this.token) headers["Authorization"] = `Bearer ${this.token}`;
    if (opts.lastEventId) headers["Last-Event-ID"] = String(opts.lastEventId);
    const res = await fetch(this.buildUrl(`/campaigns/${campaignId}/events`), { headers, signal });
    if (res.status === 401) this.onUnauthorized?.();
    if (!res.ok || !res.body) {
      throw new ApiError(res.status, res.statusText || "Couldn't open the campaign stream");
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
          if (ev && ev.type === "progress") opts.onProgress(ev.data as unknown as CampaignProgress);
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
