/**
 * TypeScript mirrors of the backend Pydantic schemas (nexus/api/schemas.py).
 * Keep in sync with the API. These are the contract between UI and server.
 */

export type Role = "owner" | "admin" | "manager" | "rep";

export interface TokenResponse {
  access_token: string;
  token_type: string;
  tenant_id: string;
  role: Role;
}

export interface SignupRequest {
  company_name: string;
  company_slug: string;
  email: string;
  full_name: string;
  password: string;
}

export interface LoginRequest {
  email: string;
  password: string;
  tenant_slug?: string | null;
}

export interface Account {
  id: string;
  name: string;
  domain: string | null;
  industry: string | null;
  employee_count: number | null;
  country: string | null;
  tech_stack: string[];
}

export type AccountInput = Omit<Account, "id">;

export interface Contact {
  id: string;
  account_id: string;
  full_name: string;
  title: string | null;
  seniority: string | null;
  email: string | null;
  phone: string | null;
  linkedin_url: string | null;
  email_confidence: number;
  phone_confidence: number;
  enrichment_source: string | null;
}

export interface TriageSummary {
  signal_kind: string | null;
  signal_strength: number | null;
  signal_age_hours: number | null;
  deliverability: EmailStatus | string | null;
  email_confidence: number | null;
  research_ready: boolean;
}

export interface InboxTask {
  id: string;
  title: string;
  reason: string;
  priority: number;
  status: string;
  account_id: string | null;
  suggested_action: Record<string, unknown>;
  triage?: TriageSummary | null;
}

export interface SignalEvent {
  id: string;
  account_id: string | null;
  contact_id: string | null;
  kind: string;
  source: string;
  title: string;
  body: string | null;
  url: string | null;
  strength: number;
  occurred_at: string;
}

export type AlertSeverity = "info" | "warning" | "critical";
export type AlertStatus = "open" | "acked";

export interface Alert {
  id: string;
  title: string;
  body: string;
  severity: AlertSeverity;
  channel: string;
  status: AlertStatus;
  account_id: string | null;
  signal_id: string | null;
  source: string;
  meta: Record<string, unknown>;
}

export interface Member {
  membership_id: string;
  user_id: string;
  email: string;
  full_name: string;
  role: Role;
  workspace_id: string | null;
}

export interface Workspace {
  id: string;
  name: string;
}

export interface AgentRunResponse {
  agent: string;
  status: string;
  output: Record<string, unknown>;
  error: string | null;
  latency_ms: number;
  tokens: number;
  run_id: string | null;
}

export interface AnalyticsOverview {
  [key: string]: number;
}

// ---- relevance / ICP ----
export interface IcpDefinition {
  industries?: string[];
  countries?: string[];
  employee_min?: number | null;
  employee_max?: number | null;
  required_tech?: string[];
  weights?: Record<string, number>;
}

export interface ValueProp {
  name: string;
  description?: string;
  pains_solved?: string[];
}

export interface RelevanceProfile {
  id: string;
  icp: IcpDefinition;
  value_props: ValueProp[];
  product_context: string;
}

export type RelevanceProfileInput = Omit<RelevanceProfile, "id">;

// ---- lists / segments ----
export interface ListFilter {
  industries?: string[];
  countries?: string[];
  min_employees?: number | null;
  max_employees?: number | null;
  min_composite?: number | null;
}

export interface ListBuildResult {
  id: string;
  name: string;
  accounts: number;
}

// ---- plays ----
export interface PlayTrigger {
  signal_kinds?: string[];
  min_strength?: number;
  min_composite?: number | null;
}

export interface PlayAction {
  type: string;
  message?: string;
  body?: string;
  severity?: AlertSeverity;
  channel?: string;
  [key: string]: unknown;
}

export interface Play {
  id: string;
  name: string;
  enabled: boolean;
  trigger: PlayTrigger;
  actions: PlayAction[];
}

export type PlayInput = Omit<Play, "id">;

// ---- integrations ----
export interface CRMAccountInput {
  external_id: string;
  name: string;
  domain?: string | null;
  industry?: string | null;
  employee_count?: number | null;
  country?: string | null;
}

export interface CRMSyncRequest {
  source: "salesforce" | "hubspot";
  accounts: CRMAccountInput[];
}

export interface CRMSyncResponse {
  source: string;
  synced: number;
  account_ids: string[];
}

export interface CRMPushResponse {
  ok: boolean;
  source: string;
  external_id?: string | null;
  contacts: number;
}

export interface SEPPushRequest {
  sequence: string;
  contact_id?: string | null;
  email?: string | null;
  payload?: Record<string, unknown>;
}

export interface SEPPushResponse {
  ok: boolean;
  platform: string;
  detail: Record<string, unknown>;
}

// ---- orchestration ----
export type RunStatus =
  | "planning"
  | "running"
  | "awaiting_approval"
  | "completed"
  | "failed"
  | "cancelled";

export type StepStatus =
  | "pending"
  | "running"
  | "awaiting_approval"
  | "completed"
  | "failed"
  | "skipped"
  | "rejected";

export type ApprovalStatus = "pending" | "approved" | "rejected";

/** Goals the deterministic planner can author today. */
export type RunGoal = "research_account" | "research_only";

export interface RunStep {
  idx: number;
  tool: string;
  status: StepStatus;
  attempts: number;
  requires_approval: boolean;
  depends_on: number[];
  approval_id: string | null;
  error: string | null;
}

/** Deliverability verdict from the email-verification provider. */
export type EmailStatus = "valid" | "invalid" | "unknown";

/** Citations + provenance attached to a grounded draft. */
export interface DraftGrounding {
  facts?: string[];
  sources?: { title?: string; url?: string }[];
}

/**
 * The composed outreach draft staged for the approval gate. Carries the
 * grounded-send signals (was it grounded in retrieved facts, and is the
 * recipient deliverable) so the reviewer sees credibility before deciding.
 */
export interface OutreachDraft {
  contact_id?: string | null;
  subject?: string;
  body?: string;
  message?: string;
  grounded?: boolean;
  grounding?: DraftGrounding;
  email_status?: EmailStatus | null;
  email_confidence?: number | null;
}

/** Shared inter-agent context written as the run progresses. */
export interface RunBlackboard {
  account_id?: string;
  research?: { brief?: string; facts?: unknown[]; sources?: unknown[] };
  composite?: number | null;
  draft?: OutreachDraft;
  [key: string]: unknown;
}

/**
 * An approval's payload is a snapshot of the staged draft (the engine copies
 * the blackboard draft into the approval), so it carries the same grounded-send
 * signals the reviewer needs.
 */
export type ApprovalPayload = OutreachDraft & Record<string, unknown>;

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

export interface RunCreateRequest {
  goal: string;
  input?: Record<string, unknown>;
  account_id?: string | null;
  idempotency_key?: string | null;
}

export interface Approval {
  id: string;
  run_id: string;
  step_id: string;
  kind: string;
  status: ApprovalStatus;
  payload: Record<string, unknown>;
  decided_at: string | null;
}

export interface ApprovalDecisionRequest {
  decision: "approve" | "reject";
  edits?: Record<string, unknown>;
}

/** One frame from the run's Server-Sent Events stream. */
export interface RunStreamEvent {
  seq: number;
  type: string;
  data: Record<string, unknown>;
}

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
