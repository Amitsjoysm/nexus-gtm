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
  /** "Help improve the AI with this workspace's data" (D24). Pre-selected on the form. */
  training_consent?: boolean;
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
  /** Who works this account. `null` is unowned, not "everyone's". */
  owner_user_id?: string | null;
  /** The owner's name. `null` when unowned, or when the owner has since left the workspace. */
  owner_name?: string | null;
}

export type AccountInput = Omit<
  Account,
  "id" | "crm_source" | "crm_synced_at" | "owner_user_id" | "owner_name"
>;

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
  /** The verifier's verdict on `email` (null when unchecked or for a net-new person). */
  email_status?: string | null;
  linkedin_url: string | null;
  score: number;
  reasons: string[];
  /** True when this person is NOT in the workspace yet — `contact_id` is empty, so offer "add"
   *  rather than a link to a record that does not exist. */
  is_new: boolean;
  /** Employer as plain text. A net-new person has no Account row, so `account_name` is blank. */
  company: string;
}

/** A company's website domain resolved from its name, with the result that matched it. */
export interface CompanyDomainResult {
  domain: string | null;
  url: string;
  title: string;
}

/** Keep a sourced person: file them under an existing account, or find-or-create their company. */
export interface SimilarPersonInput {
  full_name: string;
  title?: string | null;
  linkedin_url?: string | null;
  company?: string;
  account_id?: string;
  new_account_name?: string;
  new_account_domain?: string;
}

export interface SimilarPersonAdded {
  contact: Contact;
  account_id: string;
  account_name: string;
  account_domain: string | null;
  /** False when the person was already in the workspace (same LinkedIn profile). */
  created: boolean;
  account_created: boolean;
}

/** Which question "find similar people" is answering. */
export type LookalikeMode = "existing" | "new";

export interface ContactLookalikeResponse {
  seed_contact_id: string;
  /** Echoed back so the empty state can be specific: "no comparable contacts in your workspace"
   *  and "we could not find anyone similar on the web" are different answers. */
  mode: LookalikeMode;
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

/** Re-verifying ONE contact. `rechecked`: the saved address held up and was kept (charged as an
 *  email check). `searched`: it failed or was missing, so the pattern search ran (charged as a
 *  contact enrichment instead). */
export interface ContactReverifyResult {
  action: "rechecked" | "searched";
  previous_email: string | null;
  previous_status: string | null;
  contact: Contact;
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
  /** When this script was written; a script is reused only on that day. */
  generated_at?: string | null;
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
  dated?: "event" | "found";
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
  /** Live calls only; null for click-to-dial. */
  provider_call_id: string | null;
  recording_url: string | null;
  transcript: string | null;
}

/** Whether this workspace can place live calls, or dials from the rep's own device. */
export interface TelephonyStatus {
  provider: string;
  mode: "manual" | "live";
  from_number: string;
  configured: boolean;
  record_calls: boolean;
  /** Why a selected provider is unusable — surfaced to admins, not silently swallowed. */
  detail: string | null;
  /** Whose Twilio places the call. "platform" minutes cost credits; "workspace" ones do not. */
  source?: "workspace" | "platform" | "none";
}

/** A workspace's own Twilio. The SID and token are never returned: `account_hint` is AC...1234. */
export interface TelephonyConnection {
  provider: string;
  source: "workspace" | "platform" | "none";
  has_credentials: boolean;
  account_hint: string;
  from_number: string;
  status: "none" | "unverified" | "connected" | "error";
  verified_at: string | null;
  last_error: string | null;
  updated_at: string | null;
}

/** Blank `account_sid` / `auth_token` keep the stored ones. */
export interface TelephonyConnectionInput {
  account_sid: string | null;
  auth_token: string | null;
  from_number: string;
}

export interface DialResult {
  mode: "manual" | "live";
  dial_url: string | null;
  provider_call_id: string | null;
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
  /** "event": occurred_at is when it happened. "found": only when we collected it, because the
   *  source gave no date. Rendered by `signalWhen` so the two never look alike. */
  dated?: "event" | "found";
}

/** One choice in the signal window picker. `days: null` is all time. */
export interface SignalWindowOption {
  label: string;
  days: number | null;
}

/** GET /signals/window: the platform's window policy, set by a superadmin. */
export interface SignalWindowPolicy {
  user_choice: boolean;
  default_days: number | null;
  options: SignalWindowOption[];
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

/** The latest COMPLETED run of one agent, as saved — so a page shows it instead of an empty card.
 *  `fresh` = written today (UTC): drafts propose specific dates, so a stale one is regenerated
 *  rather than reused. A brief carries no dates and is shown whatever its age. */
export interface LatestAgentRun {
  agent: string;
  input: Record<string, unknown>;
  output: Record<string, unknown>;
  created_at: string;
  fresh: boolean;
}

/** What "Draft from website" last found, or all-null when nothing has been analysed. */
export interface LastWebsiteAnalysis {
  url: string | null;
  draft: RelevanceProfileInput | null;
  analyzed_at: string | null;
}

export interface AnalyticsOverview {
  [key: string]: number;
}

// ---- relevance / ICP ----
export interface IcpDefinition {
  industries?: string[];
  countries?: string[];
  // Sub-country geography and revenue. The engine has scored these since 0051; until now there
  // was no field to set them from, so a tester could filter on Country and nothing finer.
  regions?: string[];
  postal_codes?: string[];
  revenue_min?: number | null;
  revenue_max?: number | null;
  employee_min?: number | null;
  employee_max?: number | null;
  required_tech?: string[];
  buyer_titles?: string[];
  // Title matching beyond an exact string. `buyer_titles` is an exact-ish match; these let a
  // customer say "Director or Head, anything mentioning facilities, but not assistants" and catch
  // "Head of Facilities", "Facilities Head" and "Director, Facilities" in one rule.
  job_levels?: string[];
  title_keywords?: string[];
  exclude_title_keywords?: string[];
  weights?: Record<string, number>;
}

// Mirrors LEVELS in nexus/relevance/job_levels.py. Stable strings: renaming one silently drops
// whatever a customer had selected, because the stored value stops matching.
export const JOB_LEVELS: { value: string; label: string }[] = [
  { value: "c_level", label: "C-Level" },
  { value: "vp", label: "VP" },
  { value: "head", label: "Head" },
  { value: "director", label: "Director" },
  { value: "manager", label: "Manager" },
  { value: "ic", label: "Individual contributor" },
];

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
  /** Set on a save that changed the ICP: the page then asks how many companies to add now. */
  icp_changed?: boolean;
}

export type RelevanceProfileInput = Omit<RelevanceProfile, "id" | "icp_changed">;

/** What adding N ICP companies would cost, against the workspace's credit balance. */
export interface PopulateQuote {
  count: number;
  credits_per_company: number;
  total_credits: number;
  /** Null when the balance could not be read; the server still refuses what it cannot cover. */
  balance: number | null;
  enough: boolean;
}

export type PopulateStatus = "queued" | "running" | "done" | "failed";

/** One "add N companies now" request and how it went. */
export interface PopulateRun {
  id: string;
  status: PopulateStatus;
  requested: number;
  delivered: number;
  /** Companies added per source: `database`, `linkedin`, `web`. */
  sources: Record<string, number>;
  /** Companies found and not added, by reason: `no_website`, `already_held`, ... */
  discarded: Record<string, number>;
  /** Why a source added nothing: `{ linkedin: "not_configured" | "failed" | ... }`. */
  notes: Record<string, string>;
  account_ids: string[];
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string | null;
}

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
  kind: ListKind;
  accounts: number;
  members: number;
  added?: number;
  skipped?: number;
}

/** A list holds companies or people, never both. */
export type ListKind = "account" | "contact";

/** A list as returned by GET /lists. `accounts` is distinct companies; `members` is what it holds,
 *  which differs on a contact list. */
export interface ProspectList {
  id: string;
  name: string;
  kind: ListKind;
  accounts: number;
  members: number;
  owner_user_id: string | null;
  owner_name: string | null;
  /** The person who made it, or a manager. Decided by the server. */
  can_edit: boolean;
  created_at: string;
  updated_at: string | null;
}

/** One member of a list. On an account list the contact fields are empty. */
export interface ListMember {
  account_id: string;
  account_name: string;
  domain: string | null;
  industry: string | null;
  country: string | null;
  employee_count: number | null;
  contact_id: string | null;
  full_name: string | null;
  title: string | null;
  email: string | null;
  email_status: string | null;
  added_at: string | null;
}

export interface ListMembersPage {
  total: number;
  items: ListMember[];
}

export interface ListAddResult {
  added: number;
  already: number;
  skipped: number;
  members: number;
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
// `risky` was missing here, so `asEmailStatus` narrowed it to null and the Approvals page showed
// NO deliverability chip at all for the one verdict a reviewer most needs to see before approving.
export type EmailStatus = "valid" | "catch_all" | "risky" | "invalid" | "unknown";

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
  /**
   * Step progress as counts. The runs LIST sends these with `steps: []` — shipping every step's
   * output blob to render one "3/5" label is not worth it — so anything showing progress must
   * read these rather than `steps.length`.
   */
  step_total?: number;
  step_done?: number;
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

// ---- automation + CRM sync (settings) ----
export interface AutomationSettings {
  automation_enabled: boolean;
  /** Per-workspace daily target for net-new ICP accounts. null = platform default. */
  icp_daily_count: number | null;
  icp_daily_default: number;
  /** What the current discovery interval delivered. Absent before the first pass, or from an older server. */
  today?: DiscoveryToday | null;
}

export interface DiscoveryToday {
  window_started_at: string | null;
  target: number;
  delivered: number;
  /** Passes so far this interval; a short day is topped up a few hours apart. */
  attempts: number;
  last_attempt_at: string | null;
  /** Empty when the number was met. Otherwise what failed and where the candidates went. */
  short_reason: string;
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
  /** STARTTLS on a plain connection. `use_ssl` is implicit TLS from the first byte — not both. */
  use_tls: boolean;
  use_ssl: boolean;
  /** Stored, not resolved: blank means "use the provider preset". See `server_summary`. */
  imap_host: string;
  imap_port: number;
  drafts_folder: string;
  /** What this mailbox will really use, preset and overrides merged, computed on the server. */
  server_summary: string;
  /** Whether a draft can be saved to this mailbox (it needs an IMAP host and credentials). */
  supports_drafts: boolean;
  enabled: boolean;
  default: boolean;
  has_password: boolean;
  verified_at: string | null;
  /** Why the last verification failed, in the SMTP server's own words. */
  last_error: string | null;
  /** The rep's sign-off block, appended to every email sent from this mailbox. */
  signature: string;
  /** Whether the CALLER owns this mailbox — sending requires your own. */
  mine: boolean;
  /** Owned by nobody (added before ownership existed). Claimable, unlike a colleague's. */
  unassigned: boolean;
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
  use_ssl?: boolean;
  imap_host?: string;
  imap_port?: number;
  drafts_folder?: string;
  enabled: boolean;
  signature?: string;
}

/**
 * How this workspace's drafted emails read and sign off.
 *
 * `samples` are emails the workspace considers good. The draft writer copies their structure and
 * voice only — never their facts; see `nexus/agents/email_style.py`.
 */
export interface EmailStyle {
  default_signature: string;
  tone: string;
  length_words: number | null;
  samples: string[];
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

/** A tenant's own CRM connection. The access token is never returned by the API. */
export interface CRMConnection {
  provider: string;
  /** Where the effective config comes from: the tenant's own row, deployment env, or nothing. */
  source: "tenant" | "env" | "none";
  has_credentials: boolean;
  status: "none" | "unverified" | "connected" | "error";
  api_base: string;
  verified_at: string | null;
  last_error: string | null;
  updated_at: string | null;
}

/** `access_token` is write-only: omit it to keep the stored secret. */
export interface CRMConnectionInput {
  provider: string;
  access_token?: string | null;
  api_base?: string;
}

export interface CRMConnectionTest {
  ok: boolean;
  label: string;
  detail: string;
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

/** One side of a mid-cycle plan change. `amount_cents` is signed: a credit is negative. */
export interface ProrationLine {
  kind: string;
  description: string;
  amount_cents: number;
  days_remaining: number;
  days_in_period: number;
}

export interface BillingUsage {
  plan: string | null;
  plan_name: string | null;
  period: string;
  capabilities: CapabilityUsage[];
  /** trialing | active | past_due | suspended | canceled. Null when no subscription exists. */
  status: string | null;
  /** `custom`/`enterprise` mean an admin-managed deal: no self-serve checkout. */
  plan_class: string | null;
  trial_end: string | null;
  period_end: string | null;
  /** Net of every adjustment already committed to this period's invoice. */
  pending_proration_cents: number;
  proration_lines: ProrationLine[];
}

/** A named switch a plan entitlement can hang off. Platform-global, not per-tenant. */
export interface FeatureFlag {
  id: string;
  description: string;
  enabled: boolean;
  /** Keyed `tenant:<id>` / `env:<name>`, resolved narrowest-first by the server. */
  overrides: Record<string, boolean>;
  /** Plans whose entitlements name this flag. Empty means flipping it affects nobody. */
  used_by_plans: string[];
}

export interface RevenueReport {
  revenue: {
    mrr_cents: number;
    arr_cents: number;
    paying_tenants: number;
    trialing_tenants: number;
    past_due_tenants: number;
    by_plan: Record<string, { tenants: number; mrr_cents: number }>;
  };
  collection: {
    invoiced_cents: number;
    paid_cents: number;
    outstanding_cents: number;
    invoices: number;
    paid_invoices: number;
    failed_invoices: number;
    collection_rate: number;
  };
}

/** One capability as a plan sees it. `configured` false = falls through to the catalog default. */
export interface PlanEntitlement {
  capability_id: string;
  name: string;
  category: string;
  unit: string;
  default_mode: string;
  configured: boolean;
  mode: string | null;
  quota: number | null;
  soft_limit_pct: number;
  overage_price_credits: number | null;
  feature_flag: string | null;
}

/** What a plan change would cost, computed without writing anything. */
export interface ProrationPreview {
  tenant_id: string;
  plan_id: string;
  credit_cents: number;
  charge_cents: number;
  net_cents: number;
  days_remaining: number;
  days_in_period: number;
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

/** One capability's share of this period's credit spend (`GET /billing/usage/credits`). */
export interface CreditSpendRow {
  capability_id: string;
  name: string;
  credits: number;
  /** How many times it ran. Deliberately separate from `credits`: 40 enrichments is 120 credits,
   *  and reporting only the count was the gap this whole report closes. */
  actions: number;
}

export interface CreditDay {
  /** `YYYY-MM-DD`. */
  date: string;
  credits: number;
}

export interface CreditUserRow {
  user_id: string;
  /** "Former member" for someone who has left the workspace. */
  name: string;
  email: string;
  credits: number;
}

/**
 * Where this period's credits went.
 *
 * Built from the credit LEDGER rather than the usage stream, so the numbers reconcile against the
 * balance the customer can see. `granted` and `spent` are period figures; `balance` is live and
 * may include credits carried over, so `granted - spent === balance` does NOT hold in general.
 */
export interface CreditUsageReport {
  period: string;
  granted: number;
  spent: number;
  balance: number;
  by_capability: CreditSpendRow[];
  by_day: CreditDay[];
  by_user: CreditUserRow[];
  /**
   * Spend with no user behind it: refresh sweeps, crawls, plays.
   *
   * ATTRIBUTION IS PARTIAL BY CONSTRUCTION, so `by_user` cannot sum to `spent`. Reported as its own
   * line rather than dropped, because a screen whose rows do not add up to the balance is one
   * people stop believing.
   */
  unattributed_credits: number;
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

/**
 * A plan this workspace can switch to, from `GET /billing/plans`.
 *
 * Only `standard`, `active` plans appear: checkout refuses custom and enterprise with a 409, and
 * listing something the next click rejects is worse than not listing it.
 */
export interface SellablePlan {
  id: string;
  name: string;
  description: string;
  base_price_cents: number;
  currency: string;
  interval: string;
  included_credits: number;
  max_seats: number | null;
  trial_days: number;
  sort_order: number;
  current: boolean;
  /** Module names, resolved against THIS plan — not against what the caller currently has. */
  includes: string[];
  excludes: string[];
  /**
   * The tier this plan belongs to, shared by its monthly and annual rows. Decided by the server
   * (`interval_pairs`), so the picker groups on it instead of guessing pairs from ids or names.
   */
  family: string;
  /** The same tier billed on the other interval, or null when this plan is sold on one only. */
  counterpart_id: string | null;
}

/** A redirect to the payment provider. Nothing is written until the webhook comes back. */
export interface HostedSession {
  id: string;
  url: string;
  provider: string;
  plan_id: string | null;
}

/**
 * One deployment setting a superadmin may change without a deploy.
 *
 * Only settings the server's catalog allows appear here. The 150-odd other fields on `Settings` are
 * deploy-time - several are guards, and a guard that can be switched off from the interface it
 * protects is not a guard.
 */
export interface RuntimeSetting {
  key: string;
  label: string;
  group: string;
  kind: "bool" | "int" | "float" | "str";
  /** What changing it does, in one sentence. */
  effect: string;
  /** What it costs or breaks. Always present on medium and high risk. */
  warning: string;
  risk: "low" | "medium" | "high";
  /** Read once into a module-level object; the value is stored and pending until a restart. */
  requires_restart: boolean;
  options: string[];
  minimum: number | null;
  maximum: number | null;
  value: unknown;
  /** True when an operator set it here, rather than it coming from the environment. */
  overridden: boolean;
  /**
   * Whether the stored value is what the application is actually running on.
   *
   * "Saved" and "in force" are different facts. A restart-only setting is stored and pending, and
   * a panel showing only the first is how an operator concludes a feature is on when it is not.
   */
  in_effect: boolean;
  note: string;
  /** What each option is called on screen. The stored value stays the raw option. */
  option_labels: Record<string, string>;
  placeholder: string;
}

/** `POST /admin/runtime/email-verifier/check`: whether the verifier in force answers. */
export interface EmailVerifierCheck {
  status: "ok" | "degraded" | "unconfigured" | "error";
  /** Never contains the Authorization header. */
  detail: string;
  provider: string;
  /** Empty when no Reacher key is configured. */
  url: string;
}

export interface WebhookInfo {
  path: string;
  provider: string;
  signing_secret_configured: boolean;
  /** "control plane" | "environment" | "not set" - which of the two is actually in force. */
  signing_secret_source: string;
  stripe_account: string;
  livemode: boolean;
  events_handled: string[];
  instructions: string[];
}

export interface WebhookTestResult {
  ok: boolean;
  url?: string;
  http_status?: number;
  detail: string;
}

/**
 * A platform provider API key. The key itself is NEVER sent to the client — `key_hint` is its last
 * four characters, which is all the UI needs to tell two rows apart.
 */
export interface ProviderKey {
  id: string;
  provider: string;
  label: string;
  key_hint: string;
  /**
   * untested | probe_ok | verified | failed.
   *
   * `probe_ok` and `verified` are NOT the same and must not render alike: a key can authenticate
   * while every real call fails. Measured on Groq 2026-08-21 — five keys passed `GET /models` and
   * 404'd on every completion, so the stub wrote every outbound email.
   */
  status: string;
  last_depth: string;
  last_error: string;
  last_error_status: number | null;
  enabled: boolean;
  /** The pinned key: tried first, so rotation is the failure path rather than the resting state. */
  preferred: boolean;
  /**
   * The key actually serving traffic for this provider right now.
   *
   * Computed server-side from the same ordering the resolver uses — pinned first, then oldest,
   * enabled only — so the indicator cannot disagree with which credential is really being spent.
   */
  in_use: boolean;
}

export interface ProviderKeyTestResult {
  ok: boolean;
  status: string;
  detail: string;
  http_status: number | null;
}

/** A workspace in the Control-plane directory. */
export interface CustomerRow {
  tenant_id: string;
  workspace: string;
  plan_id: string;
  plan_name: string;
  status: string;
  users: number;
  /**
   * The member address the search matched, when it matched one. Credits belong to a WORKSPACE,
   * not a person, so an operator who typed an email needs to see they found the right human
   * rather than a workspace that merely contains a similar address.
   */
  matched_email: string;
  /** The number this workspace's platform-account calls show; "" is the platform default. */
  platform_caller_id?: string;
  requests_this_period: number;
  credits_balance: number;
}

export interface CustomerCapabilityUse {
  capability_id: string;
  name: string;
  category: string;
  used: number;
}

export interface CustomerUsage {
  tenant_id: string;
  workspace: string;
  period: string;
  plan_id: string;
  plan_name: string;
  status: string;
  capabilities: CustomerCapabilityUse[];
  credits_balance: number;
  requests_this_period: number;
  requests_total: number;
}

/** Full subscription terms. The list view omits everything below `status`. */
export interface AdminSubscriptionDetail {
  id: string;
  plan_id: string;
  plan_name: string;
  plan_class: string;
  status: string;
  interval: string;
  currency: string;
  current_period_start: string | null;
  current_period_end: string | null;
  trial_end: string | null;
  cancel_at_period_end: boolean;
  grandfathered: boolean;
  seats_included: number | null;
  /** Empty for an enterprise deal that never had a provider object. */
  psp_customer_id: string;
  psp_subscription_id: string;
}

/** Only what is sent is changed. `plan_id` is deliberately absent — see the endpoint. */
export interface SubscriptionPatch {
  status?: string;
  trial_end?: string | null;
  current_period_end?: string | null;
  cancel_at_period_end?: boolean;
  seats_included?: number | null;
  grandfathered?: boolean;
  reason?: string;
}

/**
 * A payment-provider account. The secret key is NEVER sent to the client — `key_hint` is its last
 * four characters, and `account_name` is read back from the provider during verification because
 * authenticating against the WRONG business looks exactly like success.
 */
export interface PaymentCredential {
  id: string;
  provider: string;
  label: string;
  key_hint: string;
  publishable_key: string;
  account_id: string;
  account_name: string;
  livemode: boolean;
  /** registered | verified | failed. Only `verified` can be activated. */
  status: string;
  last_error: string;
  active: boolean;
}

/**
 * Platform-wide counts, read across every tenant.
 *
 * `requests_with_a_user` is separate on purpose: attribution is partial by construction. Only
 * usage events carry a user id, and background work — crawls, sweeps, plays — has nobody to
 * attribute it to. Showing only the total would imply the difference came from nowhere.
 */
export interface PlatformOverview {
  users: number;
  active_users: number;
  tenants: number;
  requests_this_period: number;
  requests_total: number;
  requests_with_a_user: number;
  credits_granted: number;
  credits_spent: number;
}

export interface SupportedProvider {
  id: string;
  label: string;
  /**
   * Whether this provider has a model to choose. Comes from the server for the same reason the id
   * list does — a second copy here would drift, and the drift would show as a model picker whose
   * dropdown is permanently empty.
   */
  has_model: boolean;
  /** How the key is typed when it is not one opaque token (Twilio: SID:TOKEN). Empty otherwise. */
  key_format?: string;
}

/**
 * What a provider currently offers, asked of the provider itself rather than read from a list we
 * maintain: `llama-3.3-70b-versatile` was withdrawn under us and every key started 404ing.
 *
 * `detail` carries the reason the list is empty. "We could not ask" and "there are none" need
 * opposite responses from the operator, and a bare `[]` conflates them.
 */
export interface ProviderModels {
  provider: string;
  /** The model in force right now — an override if one is set, else the environment value. */
  current: string;
  /** True when an operator chose it here, so the UI can offer to clear it. */
  overridden: boolean;
  models: string[];
  detail: string;
  /** The endpoint in force. Only an OpenAI-compatible provider has one to choose. */
  base_url: string;
  base_url_overridden: boolean;
  base_url_editable: boolean;
}

/** One capability that a cost change pushed below the margin floor. */
export interface CostRateBreach {
  capability_id: string;
  credits_per_unit: number;
  unit_cost_usd: number;
  gross_margin: number;
  /** What the price must become to clear the floor again — the operator's next action, precomputed. */
  credits_to_clear_floor: number;
}

export interface CostRateResult {
  capability_id: string;
  unit_cost_usd: number;
  gross_margin: number;
  /**
   * Empty when nothing broke. Non-empty is a WORK LIST, not an error — the write succeeded.
   * Covers the whole catalog, not just the capability edited: one provider price change can move
   * several that share the input.
   */
  below_floor: CostRateBreach[];
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

export interface ModuleEntitlement {
  capability_id: string;
  name: string;
  mode: string;
  /** Whether the plan includes it at all. NOT the same as "the server will let you through". */
  included: boolean;
  source: string;
  /**
   * Whether the client should actually gate this. Computed by the SERVER, so the nav and the route
   * guard cannot derive it differently. Folds in both rules: a plan gate locks only under
   * enforcement, a platform switch locks always.
   */
  locked: boolean;
  /**
   * Set only when a platform switch is what disabled this, never by a plan.
   * `disabled | coming_soon | maintenance` — three sentences sharing one entitlement.
   */
  switch_state: FeatureSwitchState | null;
  switch_message: string;
}

export type FeatureSwitchState = "enabled" | "disabled" | "coming_soon" | "maintenance";

/** One switchable module in the superadmin console (`GET /admin/features`). */
export interface FeatureSwitchRow {
  capability_id: string;
  name: string;
  state: FeatureSwitchState;
  message: string;
  updated_by: string;
  /**
   * Capability ids this module gates through `depends_on`. Switching it off stops these too, so
   * an operator sees the reach BEFORE flipping rather than from the support queue.
   */
  gates: string[];
}

export interface FeatureSwitchList {
  features: FeatureSwitchRow[];
  /** The states the server accepts. Drives the picker, so client and server cannot disagree. */
  states: FeatureSwitchState[];
}

export interface Entitlements {
  plan: string | null;
  plan_name: string | null;
  status: string | null;
  enforcement: string;
  /**
   * True only when the server will genuinely refuse a call.
   *
   * Enforcement defaults to `shadow`, which resolves every entitlement and then allows anyway.
   * Gating navigation on `included` alone would therefore hide features that still work — a
   * visible regression produced by a rollout mode whose whole promise is "changes nothing".
   */
  gating_active: boolean;
  modules: ModuleEntitlement[];
  /**
   * Menu paths a superadmin has hidden from every workspace. Optional because a server that
   * predates the field sends nothing, which means nothing is hidden.
   */
  hidden_pages?: string[];
}

/** One hideable menu page in the superadmin console (`GET /admin/features/pages`). */
export interface AdminPageRow {
  key: string;
  path: string;
  label: string;
  /** The module switch this page sits under. Hiding the page leaves the module running. */
  module: string;
  hidden: boolean;
  updated_by: string;
}

export interface AdminPageList {
  pages: AdminPageRow[];
}

/** Platform health console (`GET /admin/health/endpoints`). */
export type HealthStatus = "ok" | "degraded" | "unconfigured" | "error";
export type RouteProbeStatus = "ok" | "error" | "not_probed";

export interface HealthDependency {
  name: string;
  status: HealthStatus;
  detail: string;
  latency_ms: number | null;
}

export interface HealthRoute {
  method: string;
  path: string;
  auth: "public" | "authenticated" | "platform-admin";
  status: RouteProbeStatus;
  http_status: number | null;
  /** Why it was not probed. Present exactly when `status === "not_probed"`. */
  reason: string;
  latency_ms: number | null;
}

export interface PlatformHealth {
  generated_at: string;
  overall: HealthStatus;
  dependencies: HealthDependency[];
  routes: HealthRoute[];
  summary: {
    routes_total: number;
    routes_probed: number;
    routes_failing: number;
    routes_not_probed: number;
    dependencies_total: number;
    dependencies_ok: number;
    dependencies_degraded: number;
    dependencies_failing: number;
  };
}

/** Platform-admin user administration (`/admin/users/...`). */
export interface UserSuspendResult {
  email: string;
  suspended: boolean;
  suspended_at?: string | null;
}

export interface UserReactivateResult {
  email: string;
  suspended: boolean;
}

export interface MfaResetResult {
  email: string;
  cleared: boolean;
}

export interface ImpersonationSession {
  access_token: string;
  token_type: string;
  expires_in_min: number;
  read_only: boolean;
  impersonating: string;
  tenant_id: string;
}

export interface UserActivity {
  email: string;
  suspended: boolean;
  suspended_at: string | null;
  suspended_reason: string;
  memberships: { tenant_id: string; tenant_name: string; slug: string; role: string }[];
  /** Actions attributed to THIS user. The only true user-level trail. */
  metered_actions: {
    capability_id: string; quantity: number; unit: string; source: string;
    occurred_at: string | null; tenant_id: string; attrs: Record<string, unknown>;
  }[];
  /** What platform staff did TO this account. */
  admin_actions: { action: string; actor: string; note: string; at: string | null }[];
  /** Tenant-wide context. `attributed` says whether the row names a person at all. */
  workspace_activity: {
    capability_id: string; user_id: string; attributed: boolean; source: string;
    occurred_at: string | null; tenant_id: string;
  }[];
  attribution_note: string;
}

export interface PlatformIdentity {
  email: string;
  is_platform_admin: boolean;
  platform_role: string;
  /** Expanded permission set. The console hides controls it does not contain; the server still
   *  enforces every one of them. */
  permissions: string[];
  /** Ceiling on this caller's credit grants; null means no ceiling. */
  credit_grant_cap: number | null;
}

export interface PlatformAdmin {
  id: string;
  email: string;
  platform_role: string;
  permissions: string[];
  active: boolean;
  note: string;
  created_at: string;
}


/** What `POST /imports/{accounts,contacts}/{csv,crm}` returns.
 *
 * `skipped` and `errors` are shown, never swallowed: a silently dropped row reads as data loss and
 * the operator has no way to find which one it was. */
export interface RecordImportResult {
  created: number;
  updated: number;
  skipped: number;
  total_rows: number;
  errors: string[];
}

/** `GET /imports/fields` — the mapping picker builds itself from the server rather than from a
 * hard-coded list that drifts out of step with what the importer actually accepts. */
/** One of the workspace's own field definitions, offered as a CSV mapping target. */
export interface ImportCustomField {
  /** Send this as the mapping value — already prefixed, e.g. `custom:territory`. */
  target: string;
  key: string;
  label: string;
  kind: string;
}

export interface ImportFields {
  account_fields: string[];
  contact_fields: string[];
  /** Empty is normal. An unmapped column is still kept, under its own header — mapping it here is
   *  what lands it on the DEFINED field the workspace's filters actually read. */
  account_custom_fields: ImportCustomField[];
  contact_custom_fields: ImportCustomField[];
  max_rows: number;
  default_limit: number;
  max_upload_bytes: number;
  /** The target that means "drop this column". Sent by the server so the sentinel the picker
   *  offers and the one the server honours cannot drift apart. */
  skip_target?: string;
}

// ---- external source databases (superadmin) ----
/**
 * A read-only DSN into somebody else's Postgres, tried AHEAD of the paid enrichment APIs.
 *
 * The status ladder IS the safety story: `registered → connected → introspected → mapped →
 * verified`. Only the server advances it, and re-introspecting or re-mapping clears the proof —
 * a source that stayed `verified` after its table was rebuilt is the wrong-attribution bug.
 *
 * The connection string is in no field here, in any form. A "show DSN" affordance would turn every
 * read of this console into a credential disclosure.
 */
export interface SourceDatabase {
  id: string;
  name: string;
  kind: string;
  /** Host and database only. Never the credentials. */
  dsn_redacted: string;
  status: "registered" | "connected" | "introspected" | "mapped" | "verified" | "failed" | string;
  enabled: boolean;
  discovered_schema: { tables?: SourceTable[] };
  mapping: SourceMapping | Record<string, never>;
  dry_run: SourceDryRun | Record<string, never>;
  last_ok_at: string | null;
  last_error: string;
  /** Verified AND enabled: the only state in which anything reads from it. */
  usable: boolean;
}

export interface SourceTable {
  schema: string;
  table: string;
  columns: { name: string; type: string }[];
}

export interface SourceMapping {
  entity: string;
  schema: string;
  table: string;
  /** `{app_field: source_column}`. Names only — never an expression. */
  columns: Record<string, string>;
}

export interface SourceDryRun {
  entity: string;
  rows: number;
  /** The number that matters. Rows alone say the query ran; this says the mapping found the column
   *  that makes a row joinable to anything. */
  usable_rows: number;
  identity_field: string;
  sample: Record<string, unknown>[];
  statement: string;
  verified: boolean;
}

export interface SourceTestResult {
  server_version: string;
  database: string;
  read_only: boolean;
}

// ---- shared company crawl (superadmin) ----
/**
 * One company's agreement between the shared crawl and the per-tenant crawls.
 *
 * Delivery is gated per company on `crawl_verdict`, and until this screen existed nothing in
 * production wrote that column — so every company sat at `unknown`, the shared crawl gathered and
 * delivered nothing, and the per-tenant crawl still ran in full.
 */
export interface SharedCrawlCompany {
  company_id: string;
  domain: string;
  name: string;
  verdict: "unknown" | "agrees" | "disagrees" | string;
  verdict_at: string | null;
  last_crawled_at: string | null;
  accounts: number;
  accounts_agreeing: number;
  accounts_disagreeing: number;
  /** Signals a TENANT holds that the shared crawl does not. Extra shared signals are usually fine;
   *  this is the failure, because fan-out would present less than the tenant already has. */
  missing_from_shared: string[];
  /** False when nothing has been crawled or nothing is linked. Agreement between two empty sets is
   *  not evidence, and approving on it would make the whole gate decorative. */
  comparable: boolean;
  would_agree: boolean;
}

export interface SharedCrawlSummary {
  companies: Record<string, number>;
  /** Accounts whose company is approved. While this is 0, both crawls run for every account. */
  accounts_served_by_shared_crawl: number;
  fanout_enabled: boolean;
}

/** What a mapping may name. Server-driven so the form cannot drift from `validate_mapping`. */
export interface SourceVocabulary {
  entities: Record<string, string[]>;
  required: Record<string, string[]>;
  identity_note: Record<string, string>;
}

/** Result of sending one drafted email. `ok:false` carries the SMTP server's own reason. */
export interface SendEmailResult {
  ok: boolean;
  detail: string;
  to: string;
  from_email: string;
  email_status: string;
}

/** One signal kind and whether this workspace collects it. Absent server-side means enabled. */
export interface SignalPreference {
  kind: string;
  enabled: boolean;
}


// ---- alert delivery preferences ----
export type AlertMode = "immediate" | "digest" | "off";

/** Which accounts a personal route covers: every account, or only the ones this member owns. */
export type AlertScope = "all" | "mine";

export interface NotificationPreference {
  category: string;
  channel: string;
  mode: AlertMode;
  /** Minutes from LOCAL midnight. Stored this way so the overnight wrap (22:00 -> 07:00) is
   *  arithmetic rather than a special case. `null` disables quiet hours. */
  quiet_from_min: number | null;
  quiet_to_min: number | null;
  utc_offset_min: number;
  quiet_hours_allow_critical: boolean;
  scope: AlertScope;
}

export interface NotificationPreferences {
  /** Only what the user has actually chosen. Empty means every category still follows the
   *  workspace default — an absent row is "no preference", not "the default was chosen". */
  preferences: NotificationPreference[];
  categories: string[];
  channels: string[];
  modes: AlertMode[];
  scopes: AlertScope[];
}

// ---- alert channel connections ----
/**
 * Channels a workspace connects its OWN credential for.
 *
 * Deliberately narrower than `NotificationPreferences.channels`: `in_app` needs no credential and
 * `webhook` stays a deployment-level integration an operator wires. Routing an alert to a channel
 * in this list is meaningless until somebody has connected it, which is what the guided setup and
 * the connections panel exist to fix.
 */
export type AlertChannelKind = "slack" | "teams" | "telegram" | "email";

/** Connection state for one channel. Carries NO secret — the server never returns one. */
export interface AlertChannelConnection {
  kind: AlertChannelKind;
  connected: boolean;
  /** A stored credential that no longer decrypts. Its own state: a tick beside a channel that
   *  silently stopped delivering is worse than no tick at all. */
  needs_reconnect: boolean;
  /** `not_connected` | `pending` | `connected` | `error` — only a real test send advances it. */
  status: string;
  verified_at: string | null;
  last_error: string;
  /** Which fields the connect form must collect. Server-driven so the form cannot drift. */
  fields: string[];
  /** Alert types this shared channel receives for the whole team. Empty: none go there by rule. */
  categories: string[];
}

export interface AlertChannelConnections {
  channels: AlertChannelConnection[];
}

/** What `PUT /alert-connections/{kind}/rules` saved: the whole set, sorted. */
export interface AlertChannelRules {
  kind: AlertChannelKind;
  categories: string[];
}

/** Only the fields the chosen channel declares are sent; the rest stay absent. */
export interface AlertChannelSecret {
  url?: string;
  bot_token?: string;
  chat_id?: string;
  to?: string;
}

/** One mailbox OAuth app as the Control plane reports it. No secret is ever included. */
export interface MailboxAppSetup {
  provider: "google" | "microsoft";
  configured: boolean;
  missing: string[];
  client_id: string;
  tenant: string;
  redirect_uri: string;
  scopes: string[];
  /** Last four characters of the client secret in use, or "" when none is stored. */
  secret_hint?: string;
}

/**
 * PUT /admin/engagement/setup. Omitted fields are left alone. An empty string clears a setting back
 * to the environment value; an empty secret keeps the stored one.
 */
export interface EngagementSetupInput {
  public_base_url?: string;
  google_client_id?: string;
  google_client_secret?: string;
  google_pubsub_topic?: string;
  google_push_service_account?: string;
  microsoft_client_id?: string;
  microsoft_client_secret?: string;
  microsoft_tenant?: string;
  note?: string;
}

/** GET /admin/engagement/setup — what to paste into Google Cloud and Azure (spec §12). */
export interface EngagementSetup {
  public_base_url: string;
  campaigns_enabled: boolean;
  mailbox_apps: MailboxAppSetup[];
  gmail_pubsub_topic: string;
  gmail_push_service_account: string;
  gmail_push_audience: string;
  graph_notification_url: string;
  ledger_stores: Record<string, boolean>;
  pseudonym_secret_configured: boolean;
}

/** An SDR mailbox connected by OAuth. Tokens never leave the server. */
export interface ConnectedMailbox {
  id: string;
  provider: "google" | "microsoft";
  email: string;
  display_name: string;
  owner_user_id: string;
  mine: boolean;
  status: "connected" | "needs_reauth" | "revoked" | "error";
  last_error: string | null;
  timezone: string;
  signature: string;
  reply_confidence: number | null;
  effective_reply_confidence: number;
  reply_confidence_min: number;
  reply_confidence_max: number;
  paused_until: string | null;
  last_synced_at: string | null;
  /** Sent since the owner's local midnight. */
  sent_today: number;
  /** Non-empty above 50 a day (D10). A warning, never a block. */
  volume_warning: string;
  /** This week's sends and bounces, and the warning above 3% (spec §19). Never a block. */
  sent_7d: number;
  bounced_7d: number;
  bounce_rate_7d: number;
  health_warning: string;
  created_at: string;
}

export interface MailboxProviderState {
  provider: "google" | "microsoft";
  configured: boolean;
}

/** One do-not-contact entry (D7). `liftable` is false for unsubscribes and lifted blocks. */
export interface DoNotContactEntry {
  id: string;
  /** For a domain block this is the stored key, `@acme.io`; `kind` says which it is. */
  email: string;
  /** Optional: a server that predates domain blocks sends nothing, and every row is an address. */
  kind?: "email" | "domain";
  reason: "unsubscribed" | "declined" | "bounced" | "manual";
  contact_id: string | null;
  source_message_id: string | null;
  created_by_user_id: string | null;
  created_at: string;
  lifted_at: string | null;
  lifted_by_user_id: string | null;
  lift_note: string;
  liftable: boolean;
}

/** POST /engagement/do-not-contact/bulk */
export interface DoNotContactBulkResult {
  emails_blocked: number;
  domains_blocked: number;
  already_blocked: number;
  /** The first 20 lines that were neither an address nor a domain, as written. */
  unreadable: string[];
  unreadable_count: number;
}

/** GET /engagement/settings/training — the workspace's ledger consent (D24). */
export interface TrainingConsentState {
  status: "on" | "off" | "pending";
  source: "signup" | "prompt" | "settings" | null;
  terms_version: string;
  decided_at: string | null;
  can_decide: boolean;
  prompt: boolean;
}

/** GET /admin/ledger — one ledger store's state (spec §18.2). */
export interface LedgerStoreStatus {
  store: string;
  configured: boolean;
  reachable: boolean;
  owns_schema: boolean;
  applied: string[];
  pending: string[];
  detail: string;
}

export interface LedgerStatus {
  capture_enabled: boolean;
  pseudonym_secret_configured: boolean;
  stores: LedgerStoreStatus[];
  outbox: {
    waiting: number;
    oldest_age_s: number;
    retrying: number;
    max_attempts: number;
  };
  last_built_at: string | null;
  consented_workspaces: number;
  opted_out_workspaces: number;
  undecided_workspaces: number;
}

// ---- engagement: campaigns, sequences and the reply desk (phase 11) ----------------------------

/** GET /engagement/settings/status — the engine switch, answered even while it is off. */
export interface EngagementStatus {
  engine_on: boolean;
  can_manage: boolean;
}

export type StepChannel = "email" | "call";
export type StepTiming = "auto" | "manual";

export interface EngagementStep {
  step_index?: number;
  channel: StepChannel;
  angle: string;
  timing_mode: StepTiming;
  delay_business_days: number;
  send_time_local: string | null;
  allowed_weekdays: number[];
}

/** draft → reviewing (first emails drafted) → active → paused / completed; stop ends it as completed. */
export type EngagementCampaignStatus = "draft" | "reviewing" | "active" | "paused" | "completed";

export interface EngagementCampaign {
  id: string;
  name: string;
  owner_user_id: string;
  mailbox_connection_id: string | null;
  status: EngagementCampaignStatus;
  pause_reason: string | null;
  review_every_touch: boolean;
  first_send_mode: "on_approval" | "scheduled";
  first_send_at: string | null;
  timezone_mode: "contact" | "sdr";
  launched_at: string | null;
  steps: Required<EngagementStep>[];
  /** Enrollment count by status. */
  counts: Record<string, number>;
}

export interface EngagementCampaignInput {
  name: string;
  mailbox_id: string;
  steps?: EngagementStep[];
  template_id?: string | null;
  review_every_touch?: boolean;
  first_send_mode?: "on_approval" | "scheduled";
  first_send_at?: string | null;
  timezone_mode?: "contact" | "sdr";
  source_list_id?: string | null;
}

export interface EngagementCandidate {
  contact_id: string;
  full_name: string;
  title: string;
  seniority: string;
  email: string;
  email_status: string;
  account_id: string;
  account_name: string;
  /** On the do-not-contact list: shown so the SDR knows why, refused if added. */
  blocked: boolean;
}

export interface EnrollResult {
  added: string[];
  /** reason: not_found | already_enrolled | no_email | do_not_contact:<why> */
  skipped: { contact_id: string; reason: string }[];
  /** The person is already in a colleague's (or another) live campaign. Added anyway. */
  warnings: { contact_id: string; campaign_id: string; campaign_name: string; owner: string }[];
}

export interface ReviewItem {
  enrollment_id: string;
  contact_id: string;
  contact_name: string;
  contact_email: string;
  contact_title: string;
  account_name: string;
  message_id: string | null;
  subject: string;
  body: string;
  quality_problems: string[];
  /** undrafted | draft | approved | ... */
  status: string;
}

export type EngagementEnrollmentStatus =
  | "awaiting_review" | "active" | "paused" | "snoozed" | "stopped" | "completed";

export interface EngagementEnrollment {
  id: string;
  contact_id: string;
  account_id: string;
  contact_name: string;
  contact_email: string;
  contact_title: string;
  account_name: string;
  status: EngagementEnrollmentStatus;
  status_reason: string | null;
  current_step_index: number;
  next_action_at: string | null;
  snoozed_until: string | null;
  contact_timezone: string;
}

export interface CapabilityLine {
  units: number;
  credits: number;
  likely_units: number;
  likely_credits: number;
}

/** GET /engagement/campaigns/{id}/estimate (spec §10, D18). */
export interface CampaignEstimate {
  contacts: number;
  email_steps: number;
  worst_credits: number;
  likely_credits: number;
  balance: number;
  gate_applies: boolean;
  covered: boolean;
  shortfall: number;
  per_capability: Record<string, CapabilityLine>;
  /** Non-empty above 50 sends a day from one mailbox (D10). Never a block. */
  volume_warning: string;
}

export interface SequenceTemplate {
  id: string;
  name: string;
  description: string;
  steps: EngagementStep[];
  created_at: string;
}

export interface TimelineEntry {
  message_id: string;
  direction: "out" | "in";
  kind: string;
  status: string;
  subject: string;
  preview: string;
  at: string | null;
  contact_id: string | null;
  contact_name: string;
  campaign_id: string | null;
  campaign_name: string;
  category: string | null;
}

export type ReplyCategory =
  | "interested" | "question" | "referral" | "later" | "out_of_office" | "declined"
  | "unsubscribe" | "unclear";

export type DeskDecision = "reengage" | "block" | "close" | "meeting";

export interface DeskQueueItem {
  id: string;
  message_id: string;
  category: ReplyCategory;
  corrected_category: ReplyCategory | null;
  confidence: number;
  resolved_date: string | null;
  status: "open" | "done";
  decision: DeskDecision | null;
  assigned_user_id: string | null;
  contact_id: string | null;
  account_id: string | null;
  contact_name: string;
  contact_email: string;
  account_name: string;
  subject: string;
  preview: string;
  received_at: string | null;
  responded_at: string | null;
}

export interface DeskConversationMessage {
  id: string;
  direction: "out" | "in";
  subject: string;
  body: string;
  at: string | null;
}

export interface DeskItemDetail extends DeskQueueItem {
  body: string;
  suggested_response: string | null;
  conversation: DeskConversationMessage[];
  paused_colleagues: {
    enrollment_id: string;
    contact_id: string;
    contact_name: string;
    campaign_id: string;
    /** False when the colleague is in someone else's campaign: shown, not steerable. */
    actionable: boolean;
  }[];
}

export interface DeskScheduledItem {
  enrollment_id: string;
  contact_id: string;
  contact_name: string;
  account_name: string;
  campaign_id: string;
  status: string;
  status_reason: string | null;
  due_at: string | null;
}

export interface WorkspaceEngagementSettings {
  reply_confidence_default: number;
  reply_confidence_min: number;
  reply_confidence_max: number;
  reply_reminder_business_hours: number;
  ooo_default_days: number;
  can_edit: boolean;
}

// ---- engagement: reporting (phase 12) ----------------------------------------------------------

export interface CampaignStepResult {
  step_index: number;
  channel: StepChannel;
  sent: number;
  replies: number;
  reply_rate: number;
}

/** GET /engagement/reports/campaigns/{id}: counted in people, not messages. */
export interface CampaignReport {
  contacts: number;
  sent: number;
  bounced: number;
  replied: number;
  positive: number;
  meetings: number;
  reply_rate: number;
  positive_rate: number;
  steps: CampaignStepResult[];
  categories: Record<string, number>;
}

export interface ResponseTimeRow {
  user_id: string;
  name: string;
  answered: number;
  waiting: number;
  /** Business hours, in the mailbox's own zone. */
  median_hours: number | null;
  p90_hours: number | null;
}

export type TodayKind =
  | "reply" | "decide" | "colleagues" | "call" | "review" | "restart" | "returning";

export interface TodayItem {
  kind: TodayKind;
  title: string;
  detail: string;
  link: string;
  at: string | null;
  count: number;
}

// ---- engagement: insights (phase 13) -----------------------------------------------------------

/** What may be shown of a person or company (D26): a pattern at 3+ workspaces, else a speed band. */
export interface ProspectInsight {
  level: "pattern" | "band" | "none";
  text: string;
  best_weekday: number | null;
  best_hour: number | null;
  typical_response_hours: number | null;
  band: string;
  propensity: number | null;
}

export interface ReplyLikelihood {
  /** `unknown` when there is no fit, no recent signal and no allowed pattern to go on. */
  band: "high" | "medium" | "low" | "unknown";
  score: number;
  reasons: string[];
}

export interface BestTimeSuggestion {
  hour: number;
  minute: number;
  weekday: number | null;
  source: "person" | "company" | "campaign" | "default";
  text: string;
  /** "HH:MM", in the contact's own timezone. */
  clock: string;
}

export interface ContactInsight {
  contact_id: string;
  person: ProspectInsight;
  company: ProspectInsight;
  likelihood: ReplyLikelihood;
  best_time: BestTimeSuggestion;
}

// ---- engagement: enhancements (phase 14) -------------------------------------------------------

/** Someone a referral reply points to, and whether the workspace already has them. */
export interface ReferralCandidate {
  name: string;
  email: string;
  evidence: string;
  contact_id: string | null;
  contact_name: string | null;
  contact_email: string | null;
}

export interface ReferralResult {
  contact_id: string;
  contact_name: string;
  email: string;
  campaign_id: string;
  campaign_name: string;
  enrollment_id: string;
  drafted: boolean;
  error: string;
}

/** Someone worth writing to again because something happened at their company (spec §19). */
export interface RestartSuggestion {
  enrollment_id: string;
  contact_id: string;
  contact_name: string;
  account_id: string;
  account_name: string;
  campaign_id: string;
  campaign_name: string;
  reason: "quiet" | "later";
  signal_id: string;
  signal_kind: string;
  signal_title: string;
  signal_at: string;
  likelihood: ReplyLikelihood["band"];
}

/** A campaign the old engine finished, kept read-only as history (spec §13). */
export interface LegacyCampaign {
  id: string;
  name: string;
  status: "completed" | "cancelled" | "failed";
  created_at: string;
  targets: number;
  sent: number;
}
