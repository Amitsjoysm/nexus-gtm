"""Pydantic request/response schemas for the API."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, EmailStr, Field


# ---- auth ----
class SignupRequest(BaseModel):
    company_name: str
    company_slug: str = Field(pattern=r"^[a-z0-9][a-z0-9\-]{1,79}$")
    email: EmailStr
    full_name: str
    password: str = Field(min_length=8)


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


# ---- relevance ----
class RelevanceProfileIn(BaseModel):
    icp: dict = Field(default_factory=dict)
    value_props: list[dict] = Field(default_factory=list)
    product_context: str = ""


class RelevanceProfileOut(RelevanceProfileIn):
    id: str


# ---- accounts / contacts ----
class AccountIn(BaseModel):
    name: str
    domain: str | None = None
    industry: str | None = None
    employee_count: int | None = None
    country: str | None = None
    tech_stack: list[str] = Field(default_factory=list)


class AccountOut(AccountIn):
    id: str
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
    email_confidence: float
    phone_confidence: float
    enrichment_source: str | None = None


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
    automation_enabled: bool


class AutomationSettingsOut(BaseModel):
    automation_enabled: bool


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
