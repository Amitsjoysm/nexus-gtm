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

/** Step 1 of OTP registration — a verification code was emailed; no account exists yet. */
export interface RegisterStartResponse {
  email: string;
  expires_in_s: number;
  resend_in_s: number;
  message: string;
}

/** Generic acknowledgement (forgot/reset password) — never reveals whether an account exists. */
export interface MessageResponse {
  message: string;
}

export interface Account {
  id: string;
  name: string;
  domain: string | null;
  industry: string | null;
  employee_count: number | null;
  country: string | null;
  tech_stack: string[];
  fit_score?: number | null;
  linkedin_url?: string | null;
  description?: string | null;
  /** Extra firmographics / technographics from web enrichment. */
  sub_industry?: string | null;
  revenue?: string | null;
  region?: string | null;
  city?: string | null;
  keywords?: string[];
  source?: string | null;
  /** CRM trust signals: where this record syncs and when it last did. */
  crm_source?: string | null;
  crm_synced_at?: string | null;
}

export type AccountInput = Omit<Account, "id" | "crm_source" | "crm_synced_at">;

export interface Lookalike {
  name: string;
  domain: string;
  url: string | null;
  snippet: string;
  score: number;
  reasons: string[];
  source: string;
  already_tracked: boolean;
}

export interface LookalikeResponse {
  seed_account_id: string;
  seed_domain: string | null;
  lookalikes: Lookalike[];
}

export interface ContactLookalike {
  contact_id: string;
  full_name: string;
  account_id: string;
  account_name: string;
  title: string | null;
  seniority: string | null;
  email: string | null;
  linkedin_url: string | null;
  score: number;
  reasons: string[];
}

export interface ContactLookalikeResponse {
  seed_contact_id: string;
  lookalikes: ContactLookalike[];
}

// ---- outcome-feedback loop ----
export type OutcomeStage = "sent" | "replied" | "meeting" | "won" | "lost";

export interface Outcome {
  id: string;
  stage: OutcomeStage | string;
  account_id: string | null;
  contact_id: string | null;
  industry: string | null;
  employee_count: number | null;
  country: string | null;
  tech_count: number;
  created_at: string;
}

export interface OutcomeInput {
  stage: OutcomeStage;
  account_id?: string | null;
  contact_id?: string | null;
  /** Attribute this outcome to the campaign that drove it. */
  campaign_id?: string | null;
  meta?: Record<string, unknown>;
}

/** Per-tenant relevance weights, learned from outcomes or the static defaults. */
export interface LearnedWeights {
  weights: Record<string, number>;
  learned: boolean;
  sample_size: number;
  defaults: Record<string, number>;
}

export interface OutcomeSummary {
  total: number;
  by_stage: Record<string, number>;
  positive: number;
}

export interface Contact {
  id: string;
  account_id: string;
  full_name: string;
  title: string | null;
  seniority: string | null;
  email: string | null;
  phone: string | null;
  linkedin_url: string | null;
  email_status: string | null;
  email_confidence: number;
  email_checked_at: string | null;
  email_provider: string | null;
  phone_confidence: number;
  enrichment_source: string | null;
}

export interface WorkspaceContact {
  id: string;
  account_id: string;
  account_name: string;
  account_domain: string | null;
  full_name: string;
  title: string | null;
  seniority: string | null;
  email: string | null;
  email_status: string | null;
  email_confidence: number;
  /** ISO timestamp of the last deliverability check (any verdict). null = never checked. */
  email_checked_at: string | null;
  /** Detected email service provider (gsuite|office365|outlook|yahoo|custom|…). */
  email_provider: string | null;
  phone: string | null;
  phone_confidence: number;
  linkedin_url: string | null;
  enrichment_source: string | null;
}

export interface ReverifyResult {
  checked: number;
  updated: number;
  statuses: Record<string, number>;
}

// ---- Cold calling --------------------------------------------------------------------------
export interface CallTask {
  id: string;
  account_id: string;
  account_name: string;
  contact_id: string | null;
  contact_name: string | null;
  title: string | null;
  phone: string | null;
  reason: string;
  priority: number;
  status: string;
  source: string;
  due_at: string | null;
  has_script: boolean;
}

export interface CallScript {
  opener: string;
  hook: string;
  value_prop: string;
  discovery_questions: string[];
  objections: { objection: string; response: string }[];
  cta: string;
  voicemail: string;
}

export interface CallBriefContact {
  name: string;
  title: string | null;
  seniority: string | null;
  email: string | null;
  email_status: string | null;
  phone: string | null;
  linkedin_url: string | null;
  role_angle: string;
  source: string | null;
}

export interface CallBriefAccount {
  name: string;
  domain: string | null;
  industry: string | null;
  employee_count: number | null;
  country: string | null;
  tech_stack: string[];
  fit_score: number | null;
  fit_rationale: string;
  source: string | null;
}

export interface CallBriefInsights {
  headline: string;
  summary: string;
  recent_posts: string[];
  interests: string[];
  source: string;
  fetched_at: string | null;
}

export interface CallBriefSignal {
  title: string;
  body: string;
  kind: string;
  source: string;
  url: string | null;
  strength: number;
  occurred_at: string;
  is_personal: boolean;
}

/** The pre-call research dossier — person + company + signals, every block sourced. */
export interface CallBrief {
  contact: CallBriefContact | null;
  account: CallBriefAccount | null;
  insights: CallBriefInsights | null;
  signals: CallBriefSignal[];
  talking_points: string[];
}

export interface CallActivity {
  id: string;
  call_task_id: string | null;
  account_id: string;
  contact_id: string | null;
  disposition: string;
  notes: string;
  duration_s: number | null;
  next_step: string | null;
  occurred_at: string;
}

export const CALL_DISPOSITIONS = [
  "connected",
  "voicemail",
  "no_answer",
  "callback",
  "meeting_booked",
  "not_interested",
  "bad_number",
  "gatekeeper",
] as const;

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
  /** SLA aging: when the task entered the queue and how long it has waited. */
  created_at?: string | null;
  age_hours?: number | null;
}

export interface TitleRecommendation {
  title: string;
  priority_score: number;
  confidence: number;
  department: string;
  buying_influence: string;
  reason: string;
  alternatives: string[];
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

// ---- Relationship Graph (network) — mirrors nexus/network/schemas.py ----
export type NetworkProvider = "google" | "microsoft" | "linkedin";

/** A member's connected source account. OAuth is never sent to the client. */
export interface NetworkAccount {
  id: string;
  provider: string;
  external_account_id: string;
  display_email: string;
  status: string;
  pooling_enabled: boolean;
  last_synced_at: string | null;
}

/** A resolved person in the deduped graph (search/intro projection). */
export interface NetworkPersonSummary {
  id: string;
  primary_email: string | null;
  full_name: string;
  title: string;
  company: string;
  location: string;
}

/** One NL-search result: a known person ranked by match × best visible connection strength. */
export interface NetworkSearchHit {
  person: NetworkPersonSummary;
  score: number;
  best_strength: number;
  broker_member_ids: string[];
}

/** A warm-intro path: which teammate can broker the intro, how, and how strongly. */
export interface NetworkIntroPath {
  broker_member_id: string;
  broker_user_id: string;
  relation: string;
  strength: number;
  last_touch_at: string | null;
  provider: string;
}

export interface NetworkIngestResult {
  identities: number;
  new_persons: number;
  new_edges: number;
}

export interface NetworkOAuthStart {
  authorize_url: string;
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
  buyer_titles?: string[];
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

/** A saved segment as returned by GET /lists, with its current member count. */
export interface ProspectList {
  id: string;
  name: string;
  accounts: number;
  created_at: string;
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
  /** This step's own output — each AI run's result, inspectable separately. */
  output: Record<string, unknown>;
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
  /** Reviewer edits applied at the gate, plus a `reason` when rejected. */
  edits?: Record<string, unknown>;
  decided_at: string | null;
}

export interface ApprovalDecisionRequest {
  decision: "approve" | "reject";
  edits?: Record<string, unknown>;
  /** On approve: which configured mailbox to send from (account id; default if omitted). */
  from_account?: string | null;
  /** On approve: "send" delivers; "draft" saves to the mailbox's Drafts for manual send. */
  delivery_mode?: "send" | "draft";
  /** On reject: why, for the audit trail. */
  reason?: string | null;
}

export interface ApprovalRedraftRequest {
  instructions: string;
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

export interface NewWorkspaceRequest {
  name: string;
  slug: string;
}

// ---- segment campaigns ----
export type CampaignStatus =
  | "draft_pending"
  | "drafting"
  | "awaiting_approval"
  | "approved"
  | "sending"
  | "completed"
  | "cancelled"
  | "failed";

export type CampaignTargetStatus =
  | "pending"
  | "drafting"
  | "drafted"
  | "skipped"
  | "approved"
  | "sent"
  | "failed";

export interface CampaignTarget {
  id: string;
  account_id: string;
  status: CampaignTargetStatus | string;
  skip_reason: string | null;
  draft: Record<string, unknown>;
  error: string | null;
}

export interface Campaign {
  id: string;
  name: string;
  list_id: string;
  status: CampaignStatus | string;
  sequence: string;
  icp: Record<string, unknown>;
  report: Record<string, number>;
  send_risky: boolean;
  cadence_id: string | null;
  review_each_touch: boolean;
  created_at: string;
}

export interface CampaignDetail extends Campaign {
  targets: CampaignTarget[];
  /** Reply attribution: per-stage outcome counts recorded against this campaign. */
  outcomes: Record<string, number>;
}

export interface CampaignPreview {
  campaign_id: string;
  status: CampaignStatus | string;
  report: Record<string, number>;
  sample: CampaignTarget[];
}

export interface CampaignInput {
  name: string;
  list_id: string;
  icp?: Record<string, unknown>;
  sequence?: string;
  send_risky?: boolean;
  cadence_id?: string | null;
  review_each_touch?: boolean;
}

/** Turn a discovery-results selection into a gated personalized cadence in one call. */
export interface LaunchFromSelectionInput {
  name: string;
  account_ids?: string[];
  contact_ids?: string[];
  icp?: Record<string, unknown>;
  mode: "new_cadence" | "existing_cadence";
  cadence_id?: string | null;
  review_each_touch?: boolean;
}

/** One frame from a campaign's SSE progress stream (status + per-status target counts). */
export interface CampaignProgress {
  status: CampaignStatus | string;
  counts: Record<string, number>;
  report: Record<string, number>;
}

// ---- cadences ----
export type EnrollmentStatus = "active" | "paused" | "completed" | "stopped";
export type TouchStatus = "sent" | "skipped" | "failed" | "awaiting_approval";

export interface CadenceStep {
  step_index: number;
  delay_days: number;
  angle: string;
  channel: string;
}

export interface CadenceStepInput {
  delay_days: number;
  angle: string;
  channel: string;
}

export interface Cadence {
  id: string;
  name: string;
  description: string | null;
  is_active: boolean;
  created_at: string;
  steps: CadenceStep[];
}

export interface CadenceInput {
  name: string;
  description?: string | null;
  steps: CadenceStepInput[];
}

export interface CadenceEnrollment {
  id: string;
  campaign_id: string;
  account_id: string;
  contact_id: string | null;
  cadence_id: string;
  current_step_index: number;
  status: EnrollmentStatus | string;
  stop_reason: string | null;
  next_touch_at: string | null;
  started_at: string | null;
  completed_at: string | null;
}

export interface CadenceTouch {
  id: string;
  enrollment_id: string;
  step_index: number;
  status: TouchStatus | string;
  skip_reason: string | null;
  run_id: string | null;
  sent_at: string | null;
  error: string | null;
}

export interface EnrollmentDetail extends CadenceEnrollment {
  touches: CadenceTouch[];
}

export interface CadenceReport {
  campaign_id: string;
  cadence_id: string | null;
  total_enrollments: number;
  by_status: Record<string, number>;
  touches_sent: number;
  touches_skipped: number;
  stops: Record<string, number>;
}

// ---- automation + CRM sync (settings) ----
export interface AutomationSettings {
  automation_enabled: boolean;
  /** Per-workspace daily target for net-new ICP accounts. null = platform default. */
  icp_daily_count: number | null;
  icp_daily_default: number;
}

export interface EmailSettings {
  provider: string;
  host: string;
  port: number;
  username: string;
  from_email: string;
  from_name: string;
  use_tls: boolean;
  enabled: boolean;
  has_password: boolean;
  verified_at: string | null;
}

export interface EmailSettingsInput {
  provider: string;
  host?: string;
  port?: number;
  username: string;
  password?: string; // write-only; omit to keep the stored one
  from_email?: string;
  from_name?: string;
  use_tls?: boolean;
  enabled: boolean;
}

export interface EmailTestResult {
  ok: boolean;
  detail: string;
}

/** One sending mailbox in the workspace's multi-account SMTP config. */
export interface EmailAccount {
  id: string;
  label: string;
  provider: string;
  host: string;
  port: number;
  username: string;
  from_email: string;
  from_name: string;
  use_tls: boolean;
  enabled: boolean;
  default: boolean;
  has_password: boolean;
  verified_at: string | null;
}

export interface EmailAccountInput {
  label?: string;
  provider: string;
  host?: string;
  port?: number;
  username: string;
  password?: string; // write-only; omit to keep the stored one
  from_email?: string;
  from_name?: string;
  use_tls?: boolean;
  enabled: boolean;
}

/** Send-ready mailbox shown at the approval gate (no secrets, visible to approvers). */
export interface Mailbox {
  id: string;
  label: string;
  from_email: string;
  default: boolean;
}

export interface CRMSyncStatus {
  enabled: boolean;
  provider: string;
  pending: number;
  synced: number;
}

// ---- live dashboard activity feed ----
export type ActivityKind = "signal" | "alert" | "account_scored" | "agent_run";
export type ActivityTone = "neutral" | "info" | "success" | "warning" | "critical";

export interface ActivityItem {
  id: string;
  kind: ActivityKind | string;
  title: string;
  detail: string;
  account_id: string | null;
  account_name: string | null;
  at: string;
  tone: ActivityTone | string;
}

/* ---- Billing ------------------------------------------------------------------------- */

export interface CapabilityUsage {
  capability_id: string;
  name: string;
  category: string;
  unit: string;
  used: number;
  /** null = unlimited on this plan. */
  quota: number | null;
  mode: string;
}

export interface BillingUsage {
  plan: string | null;
  plan_name: string | null;
  period: string;
  capabilities: CapabilityUsage[];
}

export interface CreditEntry {
  id: string;
  /** Positive = granted, negative = spent. */
  delta: number;
  kind: string;
  reason: string;
  created_at: string;
}

export interface BillingCredits {
  balance: number;
  entries: CreditEntry[];
}

export interface InvoiceLine {
  kind: string;
  capability_id: string | null;
  description: string;
  quantity: number;
  amount_cents: number;
}

export interface Invoice {
  id: string;
  number: string;
  period_key: string;
  status: string;
  currency: string;
  total_cents: number;
  finalized_at: string | null;
  lines: InvoiceLine[];
}

export interface AdminRateCard {
  capability_id: string;
  name: string;
  category: string;
  unit: string;
  credits_per_unit: number;
  unit_cost_usd: number;
  /** 0-1. The guardrail refuses anything below 0.5 without an exception. */
  gross_margin: number;
  tiers: Record<string, unknown>[];
  margin_exception: boolean;
  margin_exception_reason: string;
  active: boolean;
}

export interface AdminSubscription {
  tenant_id: string;
  tenant_name: string;
  plan_id: string;
  status: string;
  grandfathered: boolean;
  current_period_end: string | null;
}

export interface AdminPlan {
  id: string;
  name: string;
  description: string;
  plan_class: string;
  status: string;
  base_price_cents: number;
  seat_price_cents: number;
  currency: string;
  interval: string;
  included_credits: number;
  max_seats: number | null;
  trial_days: number;
  sort_order: number;
  entitlement_count: number;
}
