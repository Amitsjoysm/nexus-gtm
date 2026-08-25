"""Pydantic request/response schemas for the API."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, Field


# ---- auth ----
class SignupRequest(BaseModel):
    company_name: str
    company_slug: str = Field(pattern=r"^[a-z0-9][a-z0-9\-]{1,79}$")
    email: EmailStr
    full_name: str
    password: str = Field(min_length=8)


class RegisterStartRequest(BaseModel):
    """Step 1 of OTP registration — same fields as SignupRequest; a code is emailed, no account
    is created yet."""

    company_name: str
    company_slug: str = Field(pattern=r"^[a-z0-9][a-z0-9\-]{1,79}$")
    email: EmailStr
    full_name: str
    password: str = Field(min_length=8)


class RegisterStartResponse(BaseModel):
    email: str
    expires_in_s: int          # how long the code is valid
    resend_in_s: int           # cooldown before another code can be requested
    message: str = "Verification code sent to your email."


class RegisterVerifyRequest(BaseModel):
    email: EmailStr
    code: str = Field(min_length=4, max_length=12)


class RegisterResendRequest(BaseModel):
    email: EmailStr


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    email: EmailStr
    token: str = Field(min_length=16)
    new_password: str = Field(min_length=8)


class MessageResponse(BaseModel):
    """Generic, enumeration-safe acknowledgement for forgot/reset flows."""

    message: str


class LoginRequest(BaseModel):
    email: EmailStr
    password: str
    tenant_slug: str | None = None  # disambiguate when the user belongs to many tenants


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    tenant_id: str
    role: str


class TenantOut(BaseModel):
    tenant_id: str
    name: str
    slug: str
    role: str


class SwitchTenantRequest(BaseModel):
    tenant_id: str


class NewWorkspaceRequest(BaseModel):
    """Create another workspace (tenant/org) owned by the already-authenticated user."""

    name: str = Field(min_length=1, max_length=200)
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9\-]{1,79}$")


# ---- MFA ----
class MFAEnrollRequest(BaseModel):
    """``totp`` = authenticator app, ``email`` = code mailed to the account address."""

    method: Literal["totp", "email"] = "totp"


class MFAEnrollResponse(BaseModel):
    method: str
    # TOTP only. ``secret``/``provisioning_uri`` are returned exactly once, at enrolment — the
    # server keeps only the sealed copy and can never show them again.
    secret: str | None = None
    provisioning_uri: str | None = None
    # Also shown exactly once. Empty when the user already holds unused codes, so enrolling a
    # second method does not invalidate the printout they already have.
    recovery_codes: list[str] = Field(default_factory=list)
    code_sent: bool = False
    expires_in_s: int = 0


class MFAConfirmRequest(BaseModel):
    method: Literal["totp", "email"] = "totp"
    code: str = Field(min_length=1, max_length=32)


class MFACodeRequest(BaseModel):
    """A current second-factor code (or a recovery code) proving control of the account."""

    code: str = Field(min_length=1, max_length=32)


class MFAStatusResponse(BaseModel):
    enabled: bool
    methods: list[str] = Field(default_factory=list)          # confirmed — these gate login
    pending_methods: list[str] = Field(default_factory=list)  # enrolled but never confirmed
    recovery_codes_remaining: int = 0


class MFARecoveryCodesResponse(BaseModel):
    recovery_codes: list[str]


class MFAChallengeResponse(BaseModel):
    """The login response for a user with a confirmed second factor.

    Deliberately carries no ``access_token``: ``challenge_token`` is a single-purpose,
    short-TTL credential that authorizes nothing but ``POST /auth/mfa/verify``.
    """

    mfa_required: bool = True
    challenge_token: str
    methods: list[str]
    expires_in_s: int


class MFAVerifyRequest(BaseModel):
    challenge_token: str
    code: str = Field(min_length=1, max_length=32)
    method: str | None = None  # optional hint; omitted means "try every confirmed factor"


class MFAChallengeResendRequest(BaseModel):
    challenge_token: str
    method: Literal["email"] = "email"


# ---- relevance ----
class RelevanceProfileIn(BaseModel):
    icp: dict = Field(default_factory=dict)
    value_props: list[dict] = Field(default_factory=list)
    product_context: str = ""


class RelevanceProfileOut(RelevanceProfileIn):
    id: str


class TitleRecommendationIn(BaseModel):
    """Ask the engine which buyer titles to target. Give an ``account_id`` to use that account's
    firmographics, or pass firmographics directly; both may be combined (explicit fields win)."""
    account_id: str | None = None
    industry: str | None = None
    employee_count: int | None = None
    tech_stack: list[str] = Field(default_factory=list)
    department: str | None = None      # optional filter to one function (e.g. "Sales")
    limit: int = 8


class TitleRecommendationOut(BaseModel):
    title: str
    priority_score: int                # 0..100
    confidence: float                  # 0..1
    department: str
    buying_influence: str              # economic_buyer | champion | technical_evaluator | end_user
    reason: str
    alternatives: list[str] = Field(default_factory=list)


class SuggestTitlesIn(BaseModel):
    """Generate up to `limit` (max 10) buyer titles from the WHOLE ICP. Pass the current draft
    fields to suggest from unsaved edits; leave all empty to use the saved profile's ICP."""
    industries: list[str] = Field(default_factory=list)
    employee_min: int | None = None
    employee_max: int | None = None
    required_tech: list[str] = Field(default_factory=list)
    buyer_titles: list[str] = Field(default_factory=list)
    limit: int = 10


class AnalyzeWebsiteIn(BaseModel):
    url: str


# ---- accounts / contacts ----
class AccountIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    domain: str | None = None
    industry: str | None = None
    employee_count: int | None = None
    country: str | None = None
    tech_stack: list[str] = Field(default_factory=list)


class AccountOut(AccountIn):
    id: str
    fit_score: int | None = None        # latest ICP relevance score (0..100), for the Fit column
    linkedin_url: str | None = None      # company LinkedIn (from enrichment), so reps can verify
    description: str | None = None        # one-line "what they do" (from enrichment)
    # Extra firmographics / technographics surfaced from web enrichment (stored in custom_fields).
    # Surfaced so the Account 360 Overview can show a fuller picture than name/industry/size alone.
    sub_industry: str | None = None       # more specific niche (e.g. "Neobank")
    revenue: str | None = None            # annual revenue estimate, e.g. "$10M-$50M"
    region: str | None = None             # state / province / region
    city: str | None = None
    keywords: list[str] = Field(default_factory=list)  # focus keywords describing what they do
    source: str | None = None             # discovery | csv | crm — where the account came from
    # CRM trust signals: where this record syncs to and when it last did. Surfaced in the UI
    # ("Synced to Salesforce · 2m ago") so reps can trust the data they act on.
    crm_source: str | None = None
    crm_synced_at: str | None = None


class ContactIn(BaseModel):
    full_name: str
    title: str | None = None
    seniority: str | None = None
    email: str | None = None
    phone: str | None = None
    linkedin_url: str | None = None


class ContactOut(ContactIn):
    id: str
    account_id: str
    email_status: str | None = None      # verifier verdict: valid|risky|invalid|unknown|None
    email_confidence: float
    email_checked_at: str | None = None  # ISO time of last deliverability check; None = never
    email_provider: str | None = None    # detected ESP: gsuite|office365|outlook|yahoo|custom|…
    phone_confidence: float
    enrichment_source: str | None = None


class WorkspaceContactOut(BaseModel):
    """A contact with its account context, for the workspace-wide Contacts list."""

    id: str
    account_id: str
    account_name: str
    account_domain: str | None = None
    full_name: str
    title: str | None = None
    seniority: str | None = None
    email: str | None = None
    email_status: str | None = None
    email_confidence: float = 0.0
    # ISO timestamp of the last deliverability check (any verdict). None = never checked.
    email_checked_at: str | None = None
    email_provider: str | None = None    # detected ESP: gsuite|office365|outlook|yahoo|custom|…
    phone: str | None = None
    phone_confidence: float = 0.0
    linkedin_url: str | None = None
    enrichment_source: str | None = None


class ReverifyResult(BaseModel):
    """Outcome of a contact-email re-verification pass over the workspace."""

    checked: int
    updated: int
    statuses: dict[str, int] = Field(default_factory=dict)


# ---- Cold calling --------------------------------------------------------------------------
class CallTaskOut(BaseModel):
    """A queued call with its account/contact context, for the call power-list."""

    id: str
    account_id: str
    account_name: str
    contact_id: str | None = None
    contact_name: str | None = None
    title: str | None = None
    phone: str | None = None
    reason: str = ""
    priority: int = 0
    status: str = "open"
    source: str = "manual"
    due_at: str | None = None
    has_script: bool = False


class CreateCallTaskIn(BaseModel):
    account_id: str
    contact_id: str | None = None
    reason: str = ""
    priority: int = 50


class CallScriptOut(BaseModel):
    opener: str = ""
    hook: str = ""
    value_prop: str = ""
    discovery_questions: list[str] = Field(default_factory=list)
    objections: list[dict] = Field(default_factory=list)
    cta: str = ""
    voicemail: str = ""


class DispositionIn(BaseModel):
    disposition: str
    notes: str = ""
    duration_s: int | None = None
    next_step: str | None = None
    # Set when the call was placed live: pulls the provider's real duration, recording, and
    # transcript onto the activity. Absent for click-to-dial, which logs manually.
    provider_call_id: str | None = None


class CallActivityOut(BaseModel):
    id: str
    call_task_id: str | None = None
    account_id: str
    contact_id: str | None = None
    disposition: str
    notes: str = ""
    duration_s: int | None = None
    next_step: str | None = None
    occurred_at: str
    # Populated only for live calls; NULL for click-to-dial and for recordings the provider has
    # not finished processing.
    provider_call_id: str | None = None
    recording_url: str | None = None
    transcript: str | None = None


class TelephonyStatusOut(BaseModel):
    """Whether this deployment can place live calls — never the credentials themselves."""

    provider: str = "stub"
    mode: str = "manual"            # "manual" (click-to-dial) | "live"
    from_number: str = ""           # caller ID shown to the prospect; not a secret
    configured: bool = False
    record_calls: bool = False
    detail: str | None = None       # why a selected provider is unusable, for ops


class DialIn(BaseModel):
    # The rep's own phone. A live bridge rings it first, then dials the prospect; the stub
    # ignores it because the rep's device does the dialling.
    agent_number: str | None = None


class DialOut(BaseModel):
    mode: str = "manual"
    dial_url: str | None = None
    provider_call_id: str | None = None


class CallBriefContact(BaseModel):
    name: str
    title: str | None = None
    seniority: str | None = None
    email: str | None = None
    email_status: str | None = None
    phone: str | None = None
    linkedin_url: str | None = None
    role_angle: str = ""
    source: str | None = None       # enrichment provenance (where the contact came from)


class CallBriefAccount(BaseModel):
    name: str
    domain: str | None = None
    industry: str | None = None
    employee_count: int | None = None
    country: str | None = None
    tech_stack: list[str] = Field(default_factory=list)
    fit_score: int | None = None    # latest ICP composite (0..100)
    fit_rationale: str = ""         # the relevance engine's reasoning (a citable source)
    source: str | None = None       # where the firmographics came from


class CallBriefInsights(BaseModel):
    """Person-level social insights (Apify et al.), surfaced with their source."""

    headline: str = ""
    summary: str = ""
    recent_posts: list[str] = Field(default_factory=list)
    interests: list[str] = Field(default_factory=list)
    source: str = ""
    fetched_at: str | None = None


class CallBriefSignal(BaseModel):
    title: str
    body: str = ""
    kind: str = ""
    source: str = ""
    url: str | None = None
    strength: float = 0.0
    occurred_at: str = ""
    is_personal: bool = False       # tied to this contact (vs. account-level)


class CallBriefOut(BaseModel):
    """The pre-call research dossier: who they are, their company, signals, and talking points —
    every block sourced, so the SDR is well-researched before dialing."""

    contact: CallBriefContact | None = None
    account: CallBriefAccount | None = None
    insights: CallBriefInsights | None = None
    signals: list[CallBriefSignal] = Field(default_factory=list)
    talking_points: list[str] = Field(default_factory=list)


class LookalikeOut(BaseModel):
    name: str
    domain: str
    url: str | None = None
    snippet: str = ""
    score: int
    reasons: list[str] = Field(default_factory=list)
    source: str = ""
    already_tracked: bool = False


class LookalikeResponse(BaseModel):
    seed_account_id: str
    seed_domain: str | None = None
    lookalikes: list[LookalikeOut] = Field(default_factory=list)


class ContactLookalikeOut(BaseModel):
    contact_id: str
    full_name: str
    account_id: str
    account_name: str = ""
    title: str | None = None
    seniority: str | None = None
    email: str | None = None
    linkedin_url: str | None = None
    score: int
    reasons: list[str] = Field(default_factory=list)


class ContactLookalikeResponse(BaseModel):
    seed_contact_id: str
    lookalikes: list[ContactLookalikeOut] = Field(default_factory=list)


# ---- outcomes (feedback loop) ----
class OutcomeIn(BaseModel):
    stage: str  # one of nexus.outcomes.service.STAGES
    account_id: str | None = None
    contact_id: str | None = None
    campaign_id: str | None = None  # attribute this outcome to the campaign that drove it
    meta: dict = Field(default_factory=dict)


class OutcomeOut(BaseModel):
    id: str
    stage: str
    account_id: str | None = None
    contact_id: str | None = None
    campaign_id: str | None = None
    industry: str | None = None
    employee_count: int | None = None
    country: str | None = None
    tech_count: int = 0
    created_at: str


class LearnedWeightsOut(BaseModel):
    weights: dict[str, float]
    learned: bool
    sample_size: int
    defaults: dict[str, float]


class OutcomeSummaryOut(BaseModel):
    total: int
    by_stage: dict[str, int]
    positive: int


# ---- agents ----
class AgentRunRequest(BaseModel):
    account_id: str | None = None
    inputs: dict = Field(default_factory=dict)


class AgentRunResponse(BaseModel):
    agent: str
    status: str
    output: dict
    error: str | None = None
    latency_ms: int
    tokens: int
    run_id: str | None = None


# ---- inbox ----
class TriageOut(BaseModel):
    """Glanceable triage cues for an inbox row: intent recency, reachability, grounding."""

    signal_kind: str | None = None
    signal_strength: float | None = None
    signal_age_hours: float | None = None
    deliverability: str | None = None
    email_confidence: float | None = None
    research_ready: bool = False


class InboxTaskOut(BaseModel):
    id: str
    title: str
    reason: str
    priority: int
    status: str
    account_id: str | None = None
    suggested_action: dict
    triage: TriageOut | None = None
    # SLA aging: when the task entered the queue and how long it has sat there. The UI
    # renders this as a commitment cue ("In queue 2d") so old tasks don't silently rot.
    created_at: str | None = None
    age_hours: float | None = None


# ---- lists ----
class ListBuildRequest(BaseModel):
    name: str
    filter: dict = Field(default_factory=dict)


class ProspectListOut(BaseModel):
    """A saved segment: its name and how many accounts it currently holds."""

    id: str
    name: str
    accounts: int
    created_at: datetime


# ---- workspaces ----
class WorkspaceIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class WorkspaceOut(BaseModel):
    id: str
    name: str


class AutomationSettingsIn(BaseModel):
    """Partial update: omitted fields keep their stored value."""

    automation_enabled: bool | None = None
    # Net-new strict-ICP accounts to discover per day for this workspace (SDR-selectable).
    icp_daily_count: int | None = Field(default=None, ge=5, le=100)


class AutomationSettingsOut(BaseModel):
    automation_enabled: bool
    # None -> the platform default applies (shown via icp_daily_default).
    icp_daily_count: int | None = None
    icp_daily_default: int = 20


class EmailSettingsIn(BaseModel):
    """Workspace SMTP config. `password` is write-only: omit/blank to keep the stored one."""

    provider: str = Field(default="gmail", max_length=20)  # gmail | outlook | office365 | smtp
    host: str = Field(default="", max_length=200)
    port: int = Field(default=587, ge=1, le=65535)
    username: str = Field(default="", max_length=320)
    password: str | None = Field(default=None, max_length=512)
    from_email: str = Field(default="", max_length=320)
    from_name: str = Field(default="", max_length=120)
    use_tls: bool = True
    enabled: bool = False


class EmailSettingsOut(BaseModel):
    provider: str = "gmail"
    host: str = ""
    port: int = 587
    username: str = ""
    from_email: str = ""
    from_name: str = ""
    use_tls: bool = True
    enabled: bool = False
    has_password: bool = False           # never return the secret itself
    verified_at: str | None = None


class EmailTestIn(BaseModel):
    to: str | None = Field(default=None, max_length=320)  # defaults to the requester's email


class EmailTestOut(BaseModel):
    ok: bool
    detail: str = ""


# ---- multiple sending mailboxes (email_settings["accounts"]) ----
class EmailAccountIn(BaseModel):
    """One sending mailbox. `password` is write-only: omit/blank to keep the stored one."""

    label: str = Field(default="", max_length=80)
    provider: str = Field(default="gmail", max_length=20)
    host: str = Field(default="", max_length=200)
    port: int = Field(default=587, ge=1, le=65535)
    username: str = Field(default="", max_length=320)
    password: str | None = Field(default=None, max_length=512)
    from_email: str = Field(default="", max_length=320)
    from_name: str = Field(default="", max_length=120)
    use_tls: bool = True
    enabled: bool = True


class EmailAccountOut(BaseModel):
    id: str
    label: str = ""
    provider: str = "gmail"
    host: str = ""
    port: int = 587
    username: str = ""
    from_email: str = ""
    from_name: str = ""
    use_tls: bool = True
    enabled: bool = True
    default: bool = False
    has_password: bool = False           # never return the secret itself
    verified_at: str | None = None


class MailboxOut(BaseModel):
    """Slim, send-ready mailbox for the approval gate (approvers, not just admins, see these)."""

    id: str
    label: str = ""
    from_email: str = ""
    default: bool = False


# ---- members ----
_ROLE_PATTERN = r"^(owner|admin|manager|rep)$"


class MemberInviteRequest(BaseModel):
    email: EmailStr
    full_name: str
    password: str = Field(min_length=8)
    role: str = Field(default="rep", pattern=_ROLE_PATTERN)
    workspace_id: str | None = None


class MemberRoleUpdate(BaseModel):
    role: str = Field(pattern=_ROLE_PATTERN)


class MemberOut(BaseModel):
    membership_id: str
    user_id: str
    email: str
    full_name: str
    role: str
    workspace_id: str | None = None


# ---- signals (library) ----
class SignalOut(BaseModel):
    id: str
    account_id: str | None = None
    contact_id: str | None = None
    kind: str
    source: str
    title: str
    body: str | None = None
    url: str | None = None
    strength: float
    occurred_at: str


# ---- integrations: CRM ----
class CRMAccountIn(BaseModel):
    external_id: str
    name: str
    domain: str | None = None
    industry: str | None = None
    employee_count: int | None = None
    country: str | None = None


class CRMSyncRequest(BaseModel):
    source: str = Field(default="salesforce", pattern=r"^(salesforce|hubspot)$")
    accounts: list[CRMAccountIn] = Field(default_factory=list)


class CRMSyncResponse(BaseModel):
    source: str
    synced: int
    account_ids: list[str]


class CRMPushResponse(BaseModel):
    """Result of writing a NEXUS-enriched account (+ contacts) back to the CRM."""

    ok: bool
    source: str
    external_id: str | None = None
    contacts: int = 0


class CRMSyncStatusOut(BaseModel):
    enabled: bool      # crm_sync_enabled (global) AND this tenant's automation_enabled
    provider: str      # configured crm_provider (stub|salesforce|hubspot)
    pending: int       # accounts due for sync (never synced or changed since last sync)
    synced: int        # accounts already up to date


# ---- workspace audit trail ----
class AuditEntryOut(BaseModel):
    """One audited action. ``meta`` never carries a secret — see nexus/core/audit.py."""

    id: str
    action: str
    actor_user_id: str | None = None
    actor_email: str | None = None
    target_type: str = ""
    target_id: str = ""
    meta: dict = Field(default_factory=dict)
    created_at: str


# ---- integrations: per-tenant CRM connection ----
class CRMConnectionIn(BaseModel):
    """A tenant's own CRM credentials.

    ``access_token`` is write-only: omit it or send a blank string to keep the stored secret, so
    an admin can change ``api_base`` without re-entering the token.
    """

    provider: str = Field(default="hubspot", max_length=16)
    access_token: str | None = Field(default=None, max_length=512)
    api_base: str = Field(default="", max_length=255)


class CRMConnectionOut(BaseModel):
    """Everything the server will say about a CRM connection. The secret is not on this list,
    and must never be added to it."""

    provider: str                     # effective provider
    source: str                       # tenant | env | none
    has_credentials: bool = False     # a secret is stored — never the secret itself
    status: str = "none"              # none | unverified | connected | error
    api_base: str = ""
    verified_at: str | None = None
    last_error: str | None = None
    updated_at: str | None = None


class CRMConnectionTestOut(BaseModel):
    ok: bool
    label: str = ""
    detail: str = ""


# ---- integrations: per-tenant SEP connection ----
class SEPConnectionIn(BaseModel):
    """A tenant's own SEP credentials. ``api_key`` is write-only: omit or blank to keep it."""

    provider: str = Field(default="salesloft", max_length=16)
    api_key: str | None = Field(default=None, max_length=512)


class SEPConnectionOut(BaseModel):
    """Everything the server will say about a SEP connection — the secret is not on this list."""

    provider: str
    source: str                       # tenant | default
    has_credentials: bool = False
    status: str = "none"              # none | unverified | connected | error
    verified_at: str | None = None
    last_error: str | None = None
    updated_at: str | None = None


# ---- integrations: SEP ----
class SEPPushRequest(BaseModel):
    sequence: str = "default"
    contact_id: str | None = None
    email: str | None = None
    payload: dict = Field(default_factory=dict)


class SEPPushResponse(BaseModel):
    ok: bool
    platform: str
    detail: dict


# ---- alerts ----
class AlertOut(BaseModel):
    id: str
    title: str
    body: str
    severity: str
    channel: str
    status: str
    account_id: str | None = None
    signal_id: str | None = None
    source: str
    meta: dict


# ---- plays ----
class PlayIn(BaseModel):
    name: str
    enabled: bool = True
    trigger: dict = Field(default_factory=dict)
    actions: list[dict] = Field(default_factory=list)


class PlayOut(PlayIn):
    id: str


# ---- analytics: live activity feed ----
class ActivityItemOut(BaseModel):
    """One entry in the dashboard's unified live feed (signal / alert / score / agent run)."""

    id: str
    kind: str            # signal | alert | account_scored | agent_run
    title: str
    detail: str = ""
    account_id: str | None = None
    account_name: str | None = None
    at: str              # ISO-8601 timestamp, newest-first in the response
    tone: str = "neutral"  # neutral | info | success | warning | critical


class OAuthStartOut(BaseModel):
    """Where to send the admin's browser to authorize this deployment's app."""

    authorize_url: str
