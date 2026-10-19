"""Which provider records a person selected, re-derived on the server.

The browser only ever holds row keys (tenant-bound HMACs). A selection names rows by
(provider page, page size, row key), or asks for everything matching up to 1,000 (D17). Every
record is re-read from the shared cache (or re-fetched, at the cost of one provider page), and the
row key recomputed and compared: no value is ever taken from the client. A key not found on its
page (the provider's data moved, or the key belongs to another workspace) is ``unavailable`` and
counted, never guessed at.

Resolution reads and writes only the platform cache, so it runs before any tenant write.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from nexus.prospector.client import InfoJoyClient
from nexus.prospector.fetch import fetch_page
from nexus.prospector.filters import SearchFilters
from nexus.prospector.holdings import held_companies, held_contacts
from nexus.prospector.identity import company_identity, contact_identity, row_key
from nexus.prospector.records import VendorCompany, VendorContact, parse_company, parse_contact

MAX_SELECTION = 1000
#: Select-all reads provider pages this size, and at most this many of them on Net new.
BULK_PAGE = 500
MAX_BULK_PAGES = 5


class RowRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_page: int = Field(ge=1, le=400)
    api_page_size: int = Field(ge=1, le=500)
    row_key: str = Field(pattern=r"^[0-9a-f]{32}$")


class AllMatching(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int = Field(ge=1, le=MAX_SELECTION)


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    view: Literal["contacts", "companies"]
    scope: Literal["total", "net_new"] = "total"
    filters: SearchFilters = Field(default_factory=SearchFilters)
    rows: list[RowRef] = Field(default_factory=list, max_length=MAX_SELECTION)
    all_matching: AllMatching | None = None

    @model_validator(mode="after")
    def _one_way(self) -> "Selection":
        if bool(self.rows) == bool(self.all_matching):
            raise ValueError("Select rows, or ask for all matching rows: one of the two.")
        return self


Record = VendorContact | VendorCompany


@dataclass(slots=True)
class Resolved:
    records: list[tuple[str, Record]] = field(default_factory=list)
    unavailable: int = 0
    #: Rows bought from the provider while resolving (cache misses), for the COGS record.
    vendor_rows: int = 0


def _parse(view: str):
    if view == "contacts":
        return parse_contact, contact_identity
    return parse_company, company_identity


async def resolve(ts, selection: Selection, *, client: InfoJoyClient | None = None) -> Resolved:
    canonical = selection.filters.canonical(selection.view)
    if selection.all_matching:
        return await _all_matching(ts, selection, canonical, client)
    return await _by_rows(ts, selection, canonical, client)


async def _by_rows(ts, selection: Selection, canonical: dict, client) -> Resolved:
    parse, identify = _parse(selection.view)
    out = Resolved()
    pages: dict[tuple[int, int], dict[str, tuple[str, Record]]] = {}
    seen: set[str] = set()
    for ref in selection.rows:
        page_key = (ref.api_page, ref.api_page_size)
        if page_key not in pages:
            fetched = await fetch_page(selection.view, canonical, page=ref.api_page,
                                       size=ref.api_page_size, client=client)
            out.vendor_rows += fetched.vendor_rows
            index: dict[str, tuple[str, Record]] = {}
            for item in fetched.parsed.items:
                record = parse(item)
                ident = identify(record)
                if ident:
                    index[row_key(ts.tenant_id, ident)] = (ident, record)
            pages[page_key] = index
        hit = pages[page_key].get(ref.row_key)
        if hit is None:
            out.unavailable += 1
        elif hit[0] not in seen:
            seen.add(hit[0])
            out.records.append(hit)
    return out


async def _all_matching(ts, selection: Selection, canonical: dict, client) -> Resolved:
    parse, identify = _parse(selection.view)
    limit = selection.all_matching.limit
    out = Resolved()
    seen: set[str] = set()
    for page in range(1, MAX_BULK_PAGES + 1):
        fetched = await fetch_page(selection.view, canonical, page=page, size=BULK_PAGE,
                                   client=client)
        out.vendor_rows += fetched.vendor_rows
        records = [parse(item) for item in fetched.parsed.items]
        held: dict = {}
        if selection.scope == "net_new":
            held = await (held_contacts(ts, records) if selection.view == "contacts"
                          else held_companies(ts, records))
        for record in records:
            ident = identify(record)
            if not ident or ident in seen or ident in held:
                continue
            seen.add(ident)
            out.records.append((ident, record))
            if len(out.records) >= limit:
                return out
        if len(records) < BULK_PAGE or (selection.scope == "total" and page * BULK_PAGE >= limit):
            break
    return out
