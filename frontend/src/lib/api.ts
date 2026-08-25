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
  AdminPlan,
  AdminRateCard,
  AdminSubscription,
  BillingCredits,
  BillingUsage,
  Entitlements,
  ProrationPreview,
  PlanEntitlement,
  PlatformHealth,
  ImpersonationSession,
  MfaResetResult,
  UserReactivateResult,
  UserSuspendResult,
  UserActivity,
  FeatureFlag,
  RevenueReport,
  Invoice,
  ProviderKey,
  ProviderKeyTestResult,
  AdminSubscriptionDetail,
  CustomerRow,
  CustomerUsage,
  PaymentCredential,
  PlatformOverview,
  SubscriptionPatch,
  ProviderModels,
  SupportedProvider,
  SellablePlan,
  HostedSession,
  PlatformAdmin,
  PlatformIdentity,
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
  TelephonyStatus,
  DialResult,
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
  TitleRecommendation,
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
  NetworkAccount,
  NetworkSearchHit,
  NetworkIntroPath,
  NetworkIngestResult,
  NetworkOAuthStart,
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
  /** Extra request headers, e.g. an Idempotency-Key so a double-submit can't run twice. */
  headers?: Record<string, string>;
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
    const headers: Record<string, string> = { ...(opts.headers ?? {}) };
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
      throw new ApiError(res.status, errorDetail(data, res.statusText));
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
      throw new ApiError(res.status, errorDetail(data, res.statusText));
    }
    return data as T;
  }

  /**
   * Download a CSV the server generated, honouring the current filters.
   *
   * Goes through `fetch` rather than pointing the browser at the URL: the export endpoints are
   * bearer-authenticated, and a plain link or `window.open` sends no Authorization header, so it
   * would 401. The object URL is revoked on a later task, not immediately — see below.
   */
  private async download(
    path: string,
    filename: string,
    query?: RequestOptions["query"],
  ): Promise<void> {
    const headers: Record<string, string> = {};
    if (this.token) headers["Authorization"] = `Bearer ${this.token}`;
    let res: Response;
    try {
      res = await fetch(this.buildUrl(path, query), { headers });
    } catch {
      throw new ApiError(0, "Network error — couldn't reach the server.");
    }
    if (res.status === 401) this.onUnauthorized?.();
    if (!res.ok) {
      const text = await res.text();
      const data = text ? safeJsonParse(text) : null;
      throw new ApiError(res.status, errorDetail(data, res.statusText || "Export failed"));
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    // Revoke on a LATER task, never on this one.
    //
    // `a.click()` only *schedules* the download; the browser reads the object URL afterwards, on
    // its own turn of the event loop. Revoking synchronously on the next line destroys the blob
    // before it has been read, and the file lands empty — which is exactly what "exports are
    // always blank" was. Measured in the browser: with the blob verified at 1179 bytes and 11 data
    // rows, `fetch(url)` on the line after the revoke already threw TypeError, i.e. the URL was
    // dead while the download still needed it.
    //
    // The delay is generous on purpose. The cost of holding a few KB for a minute is nothing; the
    // cost of being marginally too quick is a silently empty export, and the failure gives the
    // user no clue that anything went wrong.
    setTimeout(() => URL.revokeObjectURL(url), 60_000);
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
  recommendTitles(accountId: string, limit = 8, signal?: AbortSignal) {
    return this.request<TitleRecommendation[]>("/relevance/title-recommendations", {
      method: "POST",
      body: { account_id: accountId, limit },
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
    body: {
      disposition: string;
      notes?: string;
      duration_s?: number | null;
      next_step?: string | null;
      provider_call_id?: string | null;
    },
    signal?: AbortSignal,
  ) {
    return this.request<CallActivity>(`/calling/tasks/${taskId}/disposition`, {
      method: "POST",
      body,
      signal,
    });
  }
  telephonyStatus(signal?: AbortSignal) {
    return this.request<TelephonyStatus>("/calling/telephony", { signal });
  }
  /**
   * Start the call. Under the default stub this only returns a `tel:` URL; with telephony
   * configured it places a real, billable call — hence the idempotency key, so a double-click
   * replays the first response instead of ringing the prospect twice.
   */
  dialCallTask(taskId: string, agentNumber: string | null, idempotencyKey: string) {
    return this.request<DialResult>(`/calling/tasks/${taskId}/dial`, {
      method: "POST",
      body: { agent_number: agentNumber },
      headers: { "Idempotency-Key": idempotencyKey },
    });
  }
  skipCallTask(taskId: string, signal?: AbortSignal) {
    return this.request<CallTask>(`/calling/tasks/${taskId}/skip`, { method: "POST", signal });
  }
  contactCallActivities(contactId: string, signal?: AbortSignal) {
    return this.request<CallActivity[]>(`/calling/contacts/${contactId}/activities`, { signal });
  }
  /** Enrich from web. Returns the updated account plus the list of fields actually filled
   * (from the X-Enriched-Fields header) so the UI can report honestly. */
  async enrichAccount(
    accountId: string,
    signal?: AbortSignal,
  ): Promise<{ account: Account; filled: string[] }> {
    const headers: Record<string, string> = {};
    if (this.token) headers["Authorization"] = `Bearer ${this.token}`;
    const res = await fetch(this.buildUrl(`/accounts/${accountId}/enrich`), {
      method: "POST",
      headers,
      signal,
    });
    if (res.status === 401) this.onUnauthorized?.();
    const text = await res.text();
    const data = text ? safeJsonParse(text) : null;
    if (!res.ok) {
      throw new ApiError(res.status, errorDetail(data, res.statusText || "Enrichment failed"));
    }
    const filledHeader = res.headers.get("X-Enriched-Fields") || "";
    const filled = filledHeader ? filledHeader.split(",").filter(Boolean) : [];
    return { account: data as Account, filled };
  }
  archiveAccount(accountId: string, signal?: AbortSignal) {
    return this.request<Account>(`/accounts/${accountId}/archive`, { method: "POST", signal });
  }
  /**
   * Soft delete. Signals, alerts, inbox tasks and cadence steps all reference the account, so the
   * row stays and only stops being listed — which is also what makes `restoreAccount` possible.
   */
  deleteAccount(accountId: string, signal?: AbortSignal) {
    return this.request<{ id: string; restorable: boolean }>(`/accounts/${accountId}`, {
      method: "DELETE",
      signal,
    });
  }
  restoreAccount(accountId: string, signal?: AbortSignal) {
    return this.request<Account>(`/accounts/${accountId}/unarchive`, { method: "POST", signal });
  }
  exportAccounts(includeArchived = false) {
    return this.download("/accounts/export/csv", "accounts.csv", {
      include_archived: includeArchived || undefined,
    });
  }
  /**
   * Find this contact's phone number. Rep-triggered only — there is no background sweep, because
   * each lookup is a paid actor run and enriching a whole workspace on a schedule is a bill nobody
   * asked for. `cached` true means another workspace already bought this number.
   */
  enrichContactPhone(contactId: string, signal?: AbortSignal) {
    return this.request<{
      contact_id: string;
      phone: string;
      raw: string;
      status: string;
      cached: boolean;
    }>(`/contacts/${contactId}/enrich-phone`, { method: "POST", signal });
  }
  deleteContact(contactId: string, signal?: AbortSignal) {
    return this.request<{ id: string; restorable: boolean }>(`/contacts/${contactId}`, {
      method: "DELETE",
      signal,
    });
  }
  restoreContact(contactId: string, signal?: AbortSignal) {
    return this.request<Contact>(`/contacts/${contactId}/restore`, { method: "POST", signal });
  }
  /** Exports exactly what the list is showing — an export that disagrees is worse than none. */
  exportContacts(query?: { q?: string; account_id?: string; include_deleted?: boolean }) {
    return this.download("/contacts/export", "contacts.csv", query);
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
  suggestBuyerTitles(
    icp: {
      industries?: string[];
      employee_min?: number | null;
      employee_max?: number | null;
      required_tech?: string[];
      buyer_titles?: string[];
      limit?: number;
    },
    signal?: AbortSignal,
  ) {
    return this.request<TitleRecommendation[]>("/relevance/suggest-titles", {
      method: "POST",
      body: { limit: 10, ...icp },
      signal,
    });
  }
  analyzeWebsite(url: string, signal?: AbortSignal) {
    return this.request<RelevanceProfileInput>("/relevance/analyze-website", {
      method: "POST",
      body: { url },
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
    params: {
      account_id?: string;
      kind?: string;
      max_age_days?: number;
      limit?: number;
      offset?: number;
    } = {},
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
  /** How many net-new strict-ICP accounts discovery should add per day for this workspace. */
  setIcpDailyCount(count: number, signal?: AbortSignal) {
    return this.request<AutomationSettings>("/workspace/automation", {
      method: "PATCH",
      body: { icp_daily_count: count },
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

  // ---- billing (tenant surface) ----
  billingUsage(signal?: AbortSignal) {
    return this.request<BillingUsage>("/billing/usage", { signal });
  }
  billingCredits(signal?: AbortSignal) {
    return this.request<BillingCredits>("/billing/credits", { signal });
  }
  /** Module gates for the current workspace. Drives navigation; see `EntitlementsContext`. */
  billingEntitlements(signal?: AbortSignal) {
    return this.request<Entitlements>("/billing/entitlements", { signal });
  }
  billingInvoices(signal?: AbortSignal) {
    return this.request<Invoice[]>("/billing/invoices", { signal });
  }
  billingPlans(signal?: AbortSignal) {
    return this.request<SellablePlan[]>("/billing/plans", { signal });
  }
  /** Opens hosted Checkout. Returns a provider URL to redirect to; writes no subscription. */
  // `body` takes a plain object — `request` serializes it. Passing a pre-stringified string here
  // double-encodes it, so the wire carries a JSON *string* rather than an object and FastAPI
  // answers 422. Every other method on this client passes an object; these two did not, which is
  // why both money actions failed the moment they were first clicked.
  billingCheckout(planId: string) {
    return this.request<HostedSession>("/billing/checkout", {
      method: "POST",
      body: { plan_id: planId },
    });
  }
  /** Opens the hosted Customer Portal, where a card is changed or a plan is cancelled. */
  billingPortal() {
    return this.request<HostedSession>("/billing/portal", { method: "POST", body: {} });
  }

  // ---- platform provider keys (superadmin) ----
  // NOTE: `body` takes a plain object — `request` serializes it. Pre-stringifying double-encodes
  // and yields a 422 with no useful detail; see the comment on billingCheckout.
  providerKeys(provider = "", signal?: AbortSignal) {
    return this.request<ProviderKey[]>("/admin/provider-keys", { query: { provider }, signal });
  }
  supportedProviders(signal?: AbortSignal) {
    return this.request<SupportedProvider[]>("/admin/provider-keys/providers", { signal });
  }
  addProviderKey(body: { provider: string; label: string; key: string }) {
    return this.request<ProviderKey>("/admin/provider-keys", { method: "POST", body });
  }
  deleteProviderKey(id: string) {
    return this.request<void>(`/admin/provider-keys/${id}`, { method: "DELETE" });
  }
  preferProviderKey(id: string) {
    return this.request<ProviderKey>(`/admin/provider-keys/${id}/prefer`, { method: "POST" });
  }
  setProviderKeyEnabled(id: string, enabled: boolean) {
    return this.request<ProviderKey>(
      `/admin/provider-keys/${id}/enabled/${enabled}`, { method: "POST" },
    );
  }
  /** `verify` makes a real, billable call. Never call it on a sweep. */
  testProviderKey(id: string, depth: "probe" | "verify") {
    return this.request<ProviderKeyTestResult>(
      `/admin/provider-keys/${id}/test`, { method: "POST", query: { depth } },
    );
  }
  /** Asks the PROVIDER what it currently offers — their catalogue changes without notice. */
  providerModels(provider: string, signal?: AbortSignal) {
    return this.request<ProviderModels>(
      `/admin/provider-keys/${provider}/models`, { signal },
    );
  }
  /** An empty string clears the override and the environment value applies again. */
  setProviderModel(provider: string, model: string) {
    return this.request<{ provider: string; model: string }>(
      `/admin/provider-keys/${provider}/model`, { method: "PUT", body: { model } },
    );
  }

  // ---- billing (platform-admin control plane) ----
  adminBillingPlans(signal?: AbortSignal) {
    return this.request<AdminPlan[]>("/admin/billing/plans", { signal });
  }
  adminBillingRates(signal?: AbortSignal) {
    return this.request<AdminRateCard[]>("/admin/billing/rates", { signal });
  }
  adminBillingSubscriptions(signal?: AbortSignal) {
    return this.request<AdminSubscription[]>("/admin/billing/subscriptions", { signal });
  }
  /** Answers "am I a platform admin?" — returns false rather than 403. */
  /** Live dependency probes + route inventory. Slow by nature: it calls Stripe and Apify. */
  platformHealth(signal?: AbortSignal) {
    return this.request<PlatformHealth>("/admin/health/endpoints", { signal });
  }
  // ---- platform-admin user administration ----
  //
  // The endpoints existed and had NO caller: the whole admin_users router was reachable only by
  // someone who knew the URL. Every one of these is audited server-side with the reason supplied.
  suspendUser(email: string, reason: string) {
    return this.request<UserSuspendResult>(
      `/admin/users/${encodeURIComponent(email)}/suspend`,
      { method: "POST", body: { reason } },
    );
  }
  reactivateUser(email: string) {
    return this.request<UserReactivateResult>(
      `/admin/users/${encodeURIComponent(email)}/reactivate`,
      { method: "POST", body: {} },
    );
  }
  /** Account recovery: clears every factor and recovery code. Deletes rather than deactivates. */
  resetUserMfa(email: string) {
    return this.request<MfaResetResult>(
      `/admin/users/${encodeURIComponent(email)}/mfa`,
      { method: "DELETE" },
    );
  }
  /** Mint a time-boxed READ-ONLY session as this user. The reason is mandatory server-side. */
  impersonateUser(email: string, reason: string, ttlMin = 30) {
    return this.request<ImpersonationSession>(
      `/admin/users/${encodeURIComponent(email)}/impersonate`,
      { method: "POST", body: { reason, ttl_min: ttlMin } },
    );
  }
  /** One user's activity. Attribution is partial — see `attribution_note` in the payload. */
  userActivity(email: string, limit = 50, signal?: AbortSignal) {
    return this.request<UserActivity>(
      `/admin/users/${encodeURIComponent(email)}/activity`,
      { query: { limit }, signal },
    );
  }
  platformWhoAmI(signal?: AbortSignal) {
    return this.request<PlatformIdentity>("/admin/billing/whoami", { signal });
  }
  listPlatformAdmins(signal?: AbortSignal) {
    return this.request<PlatformAdmin[]>("/admin/billing/admins", { signal });
  }
  /** `permissions` overrides the role preset; omit it to store the preset's expanded set. */
  grantPlatformAdmin(body: {
    email: string;
    platform_role: string;
    permissions?: string[];
    note?: string;
  }) {
    return this.request<{ email: string; created: boolean; permissions: string[] }>(
      "/admin/billing/admins",
      { method: "POST", body },
    );
  }
  revokePlatformAdmin(email: string) {
    return this.request<{ email: string; active: boolean }>(
      `/admin/billing/admins/${encodeURIComponent(email)}`,
      { method: "DELETE" },
    );
  }
  updatePlan(planId: string, body: Record<string, unknown>) {
    return this.request<AdminPlan>(`/admin/billing/plans/${planId}`, {
      method: "PATCH",
      body,
    });
  }
  /** Every capability, with this plan's entitlement where one is configured. */
  planEntitlements(planId: string, signal?: AbortSignal) {
    return this.request<PlanEntitlement[]>(
      `/admin/billing/plans/${planId}/entitlements`, { signal },
    );
  }
  upsertEntitlement(planId: string, capabilityId: string, body: Record<string, unknown>) {
    return this.request<{ plan_id: string; capability_id: string; mode: string; quota: number | null }>(
      `/admin/billing/plans/${planId}/entitlements/${capabilityId}`,
      { method: "PUT", body },
    );
  }
  upsertRateCard(capabilityId: string, body: Record<string, unknown>) {
    return this.request<AdminRateCard>(`/admin/billing/rates/${capabilityId}`, {
      method: "PUT",
      body,
    });
  }
  adminBillingRevenue(since?: string, signal?: AbortSignal) {
    return this.request<RevenueReport>("/admin/billing/revenue", {
      query: { since },
      signal,
    });
  }
  /**
   * How many people are on the platform and what they consume. Neither number existed anywhere:
   * the Subscriptions tab shows plan and status, /billing/usage is tenant-scoped, and the
   * user-activity endpoint answers for one person.
   */
  adminPlatformOverview(signal?: AbortSignal) {
    return this.request<PlatformOverview>("/admin/billing/overview", { signal });
  }
  // ---- the customer directory (superadmin) ----
  adminCustomers(q = "", signal?: AbortSignal) {
    return this.request<CustomerRow[]>("/admin/billing/customers", { query: { q }, signal });
  }
  adminCustomerUsage(tenantId: string, signal?: AbortSignal) {
    return this.request<CustomerUsage>(
      `/admin/billing/customers/${tenantId}/usage`, { signal },
    );
  }
  adminTenantSubscription(tenantId: string, signal?: AbortSignal) {
    return this.request<{ tenant_id: string; subscription: AdminSubscriptionDetail | null }>(
      `/admin/billing/tenants/${tenantId}/subscription`, { signal },
    );
  }
  /** Only the fields you send are changed. Changing the PLAN has its own endpoint. */
  patchTenantSubscription(tenantId: string, body: SubscriptionPatch) {
    return this.request<{ plan_id: string; status: string; changed: string[] }>(
      `/admin/billing/tenants/${tenantId}/subscription`, { method: "PATCH", body },
    );
  }
  cancelTenantSubscription(tenantId: string, body: { at_period_end: boolean; reason: string }) {
    return this.request<{ status: string; cancel_at_period_end: boolean }>(
      `/admin/billing/tenants/${tenantId}/subscription/cancel`, { method: "POST", body },
    );
  }

  // ---- payment credentials (superadmin) ----
  paymentCredentials(signal?: AbortSignal) {
    return this.request<PaymentCredential[]>("/admin/payment-credentials", { signal });
  }
  addPaymentCredential(body: {
    label: string; secret_key: string; publishable_key: string; webhook_secret: string;
  }) {
    return this.request<PaymentCredential>("/admin/payment-credentials", { method: "POST", body });
  }
  /** Makes a real call and reports which account answered. */
  verifyPaymentCredential(id: string) {
    return this.request<{ ok: boolean; status: string; detail?: string; account_name?: string;
                          account_id?: string; livemode?: boolean }>(
      `/admin/payment-credentials/${id}/verify`, { method: "POST" },
    );
  }
  activatePaymentCredential(id: string) {
    return this.request<PaymentCredential>(
      `/admin/payment-credentials/${id}/activate`, { method: "POST" },
    );
  }
  deactivatePaymentCredential(id: string) {
    return this.request<PaymentCredential>(
      `/admin/payment-credentials/${id}/deactivate`, { method: "POST" },
    );
  }
  deletePaymentCredential(id: string) {
    return this.request<void>(`/admin/payment-credentials/${id}`, { method: "DELETE" });
  }
  adminBillingFlags(signal?: AbortSignal) {
    return this.request<FeatureFlag[]>("/admin/billing/flags", { signal });
  }
  upsertFeatureFlag(flagId: string, body: { enabled: boolean; description?: string }) {
    return this.request<FeatureFlag>(`/admin/billing/flags/${encodeURIComponent(flagId)}`, {
      method: "PUT",
      body,
    });
  }
  setFeatureFlagOverride(flagId: string, scope: "tenant" | "env", key: string, enabled: boolean) {
    return this.request<{ id: string; overrides: Record<string, boolean> }>(
      `/admin/billing/flags/${encodeURIComponent(flagId)}/overrides/${scope}/${encodeURIComponent(key)}`,
      { method: "PUT", body: { enabled } },
    );
  }
  clearFeatureFlagOverride(flagId: string, scope: "tenant" | "env", key: string) {
    return this.request<{ id: string; overrides: Record<string, boolean> }>(
      `/admin/billing/flags/${encodeURIComponent(flagId)}/overrides/${scope}/${encodeURIComponent(key)}`,
      { method: "DELETE" },
    );
  }
  /** What moving this workspace to `planId` would credit and charge. Writes nothing. */
  prorationPreview(tenantId: string, planId: string, signal?: AbortSignal) {
    return this.request<ProrationPreview>(
      `/admin/billing/tenants/${tenantId}/proration-preview?plan_id=${encodeURIComponent(planId)}`,
      { signal },
    );
  }
  pauseTenantSubscription(tenantId: string, reason: string) {
    return this.request<{ plan_id: string; status: string; paused_at: string | null }>(
      `/admin/billing/tenants/${tenantId}/pause`,
      { method: "POST", body: { reason } },
    );
  }
  resumeTenantSubscription(tenantId: string, reason: string) {
    return this.request<{ plan_id: string; status: string; days_returned: number }>(
      `/admin/billing/tenants/${tenantId}/resume`,
      { method: "POST", body: { reason } },
    );
  }
  setTenantSubscription(tenantId: string, planId: string) {
    return this.request<{ plan_id: string; status: string }>(
      `/admin/billing/tenants/${tenantId}/subscription`,
      { method: "POST", body: { plan_id: planId } },
    );
  }
  grantTenantCredits(tenantId: string, body: {
    amount: number; reason: string; idempotency_key: string;
  }) {
    return this.request<{ applied: boolean; balance: number }>(
      `/admin/billing/tenants/${tenantId}/credits`,
      { method: "POST", body },
    );
  }
  createCustomPlan(tenantId: string, body: Record<string, unknown>) {
    return this.request<Record<string, unknown>>(
      `/admin/billing/tenants/${tenantId}/custom-plan`,
      { method: "POST", body },
    );
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

  // ---- network (relationship graph) ----
  listNetworkAccounts(signal?: AbortSignal) {
    return this.request<NetworkAccount[]>("/network/accounts", { signal });
  }
  connectNetworkAccount(
    body: { provider: string; external_account_id: string; display_email?: string },
    signal?: AbortSignal,
  ) {
    return this.request<NetworkAccount>("/network/accounts", { method: "POST", body, signal });
  }
  patchNetworkAccount(
    id: string,
    body: { pooling_enabled?: boolean; status?: string },
    signal?: AbortSignal,
  ) {
    return this.request<NetworkAccount>(`/network/accounts/${id}`, {
      method: "PATCH",
      body,
      signal,
    });
  }
  syncNetworkAccount(id: string, signal?: AbortSignal) {
    return this.request<{ enqueued: boolean; account_id: string }>(
      `/network/accounts/${id}/sync`,
      { method: "POST", signal },
    );
  }
  networkOAuthStart(provider: "google" | "microsoft", signal?: AbortSignal) {
    return this.request<NetworkOAuthStart>(`/network/oauth/${provider}/start`, { signal });
  }
  importLinkedInCsv(accountId: string, file: File, signal?: AbortSignal) {
    const form = new FormData();
    form.set("file", file);
    return this.requestForm<NetworkIngestResult>(
      `/network/accounts/${accountId}/import-linkedin`, form, signal);
  }
  searchNetwork(query: string, limit = 20, signal?: AbortSignal) {
    return this.request<NetworkSearchHit[]>("/network/search", {
      method: "POST",
      body: { query, limit },
      signal,
    });
  }
  networkIntroPaths(personId: string, signal?: AbortSignal) {
    return this.request<NetworkIntroPath[]>(`/network/people/${personId}/intro-paths`, { signal });
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

/**
 * A human-readable message out of whatever the backend returned.
 *
 * FastAPI answers a validation failure (422) with `detail` as an ARRAY of
 * `{loc, msg, type}` objects, and the old code did `String(detail)` on it — which yields
 * `"[object Object]"`. Every 422 in the app therefore surfaced as that string, which tells the
 * user nothing and tells a developer reading a bug report even less: it is indistinguishable from
 * a rendering fault, so a real backend rejection reads as "the button does nothing".
 *
 * Handles the three shapes the API actually produces: a plain string (our own HTTPException
 * details), the 422 array, and anything else — which falls back to the HTTP status text rather
 * than stringifying an object.
 */
export function errorDetail(data: unknown, statusText = ""): string {
  const fallback = statusText || "Request failed";
  if (!data || typeof data !== "object") return fallback;
  const detail = (data as { detail?: unknown }).detail;
  if (typeof detail === "string" && detail.trim()) return detail;
  if (Array.isArray(detail)) {
    const parts = detail
      .map((e) => {
        if (!e || typeof e !== "object") return String(e);
        const { loc, msg } = e as { loc?: unknown; msg?: unknown };
        // `loc` is ["body", "plan_id"] — the last segment is the field the user cares about.
        const field = Array.isArray(loc) ? String(loc[loc.length - 1] ?? "") : "";
        const message = typeof msg === "string" ? msg : "is invalid";
        return field ? `${field}: ${message}` : message;
      })
      .filter(Boolean);
    if (parts.length) return parts.join("; ");
  }
  return fallback;
}
