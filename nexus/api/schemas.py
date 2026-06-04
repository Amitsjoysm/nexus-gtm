"""Pydantic request/response schemas for the API."""
from __future__ import annotations

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


# ---- lists ----
class ListBuildRequest(BaseModel):
    name: str
    filter: dict = Field(default_factory=dict)


# ---- workspaces ----
class WorkspaceIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class WorkspaceOut(BaseModel):
    id: str
    name: str


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
