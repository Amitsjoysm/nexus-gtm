"""Prospector's request and response models.

**The search row declares no raw email, phone, domain or URL field** (spec §5). Pydantic drops
anything a response model does not declare, so a provider value cannot reach the browser by being
forgotten in a serializer: only the masks, the ``has_*`` flags and the business-card facts exist.
The one exception is ``record``, filled only for a row the workspace already holds and only from
the workspace's own Contact/Account row, never from the provider's payload.
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StringConstraints

from nexus.prospector.filters import SearchFilters, WorkspaceFilters
from nexus.prospector.selection import Selection

View = Literal["contacts", "companies"]


class SavedRecordOut(BaseModel):
    kind: Literal["contact", "account"]
    id: str
    account_id: str | None = None
    email: str | None = None
    phone: str | None = None
    domain: str | None = None


class ProspectRowOut(BaseModel):
    row_key: str
    api_page: int
    api_page_size: int
    saved: bool = False
    record: SavedRecordOut | None = None
    first_name: str = ""
    last_name: str = ""
    title: str = ""
    department: str = ""
    seniority: str = ""
    company_name: str = ""
    country: str = ""
    state: str = ""
    city: str = ""
    industry: str = ""
    employee_size_range: str = ""
    email_masked: str = ""
    phone_masked: str = ""
    domain_masked: str = ""
    linkedin_masked: str = ""
    has_email: bool = False
    has_direct_phone: bool = False
    has_linkedin: bool = False


class SearchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    view: View
    scope: Literal["total", "net_new"] = "total"
    filters: SearchFilters = Field(default_factory=SearchFilters)
    page: int = Field(1, ge=1, le=400)
    page_size: Literal[25, 50, 100] = 25
    cursor: str | None = Field(None, max_length=600)
    include_count: StrictBool = False


class SearchOut(BaseModel):
    rows: list[ProspectRowOut]
    page: int
    page_size: int
    has_more: bool
    total: int | None = None
    next_cursor: str | None = None
    scanned: int = 0
    exhausted: bool = False
    scan_capped: bool = False


class SavedIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    view: View
    filters: SearchFilters = Field(default_factory=SearchFilters)
    workspace: WorkspaceFilters = Field(default_factory=WorkspaceFilters)
    page: int = Field(1, ge=1, le=10_000)
    page_size: Literal[25, 50, 100] = 25


class SavedRowOut(BaseModel):
    """A workspace record: the tenant's own data, shown as it is on Contacts and Accounts."""

    kind: Literal["contact", "account"]
    id: str
    account_id: str
    name: str = ""
    title: str = ""
    seniority: str = ""
    email: str | None = None
    email_status: str | None = None
    phone: str | None = None
    company_name: str = ""
    domain: str | None = None
    country: str | None = None
    region: str | None = None
    city: str | None = None
    industry: str | None = None
    employee_count: int | None = None
    enrichment_source: str | None = None


class SavedOut(BaseModel):
    rows: list[SavedRowOut]
    page: int
    page_size: int
    total: int


class FieldCatalogOut(BaseModel):
    view: View
    sections: list[dict]
    #: Whether the provider can count matches for this view ("Show total"). See
    #: `client.COUNT_SUPPORTED`.
    can_count: bool = False


RequestId = Field(pattern=r"^[A-Za-z0-9_-]{8,64}$")
IdOrNone = Field(None, pattern=r"^[A-Za-z0-9_-]{1,64}$")


class QuoteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selection: Selection
    action: Literal["save", "list", "reveal", "company_list", "export"] = "save"
    #: Contacts view: which paid fields to deliver. Email and direct phone are bought separately,
    #: so a rep can reveal only a phone, only an email, or both (2026-11-18).
    include_emails: StrictBool = True
    include_phones: StrictBool = False


class QuoteOut(BaseModel):
    """What the confirm dialog shows. Quantities per capability, priced as the meter prices them."""

    records: int
    charges: dict[str, int]
    credits: dict[str, float]
    total_credits: float
    already_paid: int
    held: int
    unavailable: int
    #: Of the selected records, how many have an email / a direct phone at the provider, whatever
    #: was asked for: the dialog says "6 of 25 have a direct phone" beside the phone choice.
    with_email: int = 0
    with_phone: int = 0
    #: Records already in the workspace that get a field they were missing (bought or re-delivered).
    filled: int = 0
    balance: float | None = None
    #: Whether the server would let this delivery through: the billing engine's own preflight, not
    #: a client-side comparison with the balance (which refused workspaces the engine allows).
    enough: bool = True
    #: Why not, in a sentence, when ``enough`` is false.
    reason: str | None = None
    #: False when the plan takes no credits for this (an unlimited or internal plan, no
    #: subscription, billing off): the dialog says it is included instead of showing a balance.
    uses_credits: bool = True


class DeliverIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selection: Selection
    action: Literal["save", "list", "reveal", "company_list"] = "save"
    list_id: str | None = IdOrNone
    include_emails: StrictBool = True
    include_phones: StrictBool = False
    request_id: str = RequestId


class ExportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selection: Selection
    include_emails: StrictBool = True
    include_phones: StrictBool = False
    request_id: str = RequestId


class VerifyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contact_ids: list[Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,64}$")]] = \
        Field(min_length=1, max_length=1000)


class VerifyResultOut(BaseModel):
    contact_id: str
    status: str
    email_kept: bool


class VerifyOut(BaseModel):
    mode: Literal["inline", "queued"]
    count: int
    charged: int
    results: list[VerifyResultOut] = Field(default_factory=list)


class DeliverOut(BaseModel):
    created: int = 0
    reused: int = 0
    listed: int = 0
    held: int = 0
    unavailable: int = 0
    already_paid: int = 0
    #: Records already in the workspace that were given a field they were missing.
    filled: int = 0
    dropped_linkedin: int = 0
    charged: dict[str, int] = Field(default_factory=dict)
    record_ids: list[str] = Field(default_factory=list)
    list_id: str | None = None


__all__ = [
    "FieldCatalogOut", "ProspectRowOut", "SavedIn", "SavedOut", "SavedRecordOut", "SavedRowOut",
    "SearchIn", "SearchOut",
]
