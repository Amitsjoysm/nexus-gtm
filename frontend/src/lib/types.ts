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

export interface InboxTask {
  id: string;
  title: string;
  reason: string;
  priority: number;
  status: string;
  account_id: string | null;
  suggested_action: Record<string, unknown>;
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

/** Shared inter-agent context written as the run progresses. */
export interface RunBlackboard {
  account_id?: string;
  research?: { brief?: string; facts?: unknown[]; sources?: unknown[] };
  composite?: number | null;
  draft?: {
    contact_id?: string | null;
    subject?: string;
    body?: string;
    message?: string;
    grounded?: boolean;
  };
  [key: string]: unknown;
}

export interface Run {
  id: string;
  goal: string;
  status: RunStatus;
  account_id: string | null;
  error: string | null;
  created_at: string;
  steps: RunStep[];
  blackboard: RunBlackboard;
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
