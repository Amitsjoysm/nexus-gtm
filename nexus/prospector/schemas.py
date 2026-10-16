"""Prospector's request and response models.

**The search row declares no raw email, phone, domain or URL field** (spec §5). Pydantic drops
anything a response model does not declare, so a provider value cannot reach the browser by being
forgotten in a serializer: only the masks, the ``has_*`` flags and the business-card facts exist.
The one exception is ``record``, filled only for a row the workspace already holds and only from
the workspace's own Contact/Account row, never from the provider's payload.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool

from nexus.prospector.filters import SearchFilters, WorkspaceFilters

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


__all__ = [
    "FieldCatalogOut", "ProspectRowOut", "SavedIn", "SavedOut", "SavedRecordOut", "SavedRowOut",
    "SearchIn", "SearchOut",
]
