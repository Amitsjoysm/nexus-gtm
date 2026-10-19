"""Export selected rows to CSV: the paid delivery as a file (spec §9.4).

Charged exactly like saving (D3-D6): quoted field by field, preflighted for the whole file,
recorded in the purchase ledger and metered in one savepoint, and committed by the endpoint before
the file is returned, so a failed download is retried under a new request for free. Phones are a
column only when they were bought. A row the workspace already holds exports the workspace's own
values (free). Every cell goes through ``csv_export.safe_cell``: provider text is
attacker-influenced, and a cell starting a formula must reach a spreadsheet as text.
"""
from __future__ import annotations

from sqlalchemy import select

from nexus.api.csv_export import safe_cell
from nexus.models.account import Account, Contact
from nexus.prospector import charges
from nexus.prospector.deliver import prepare
from nexus.prospector.fetch import record_browse_cost
from nexus.prospector.identity import domain_of
from nexus.prospector.pricing import Line
from nexus.prospector.records import VendorCompany, VendorContact
from nexus.prospector.selection import Selection

CONTACT_COLUMNS = ("first_name", "last_name", "title", "department", "seniority", "email",
                   "direct_phone", "company_name", "company_domain", "country", "state", "city")
COMPANY_COLUMNS = ("company_name", "domain", "country", "state", "city", "industry",
                   "employee_size_range", "linkedin_url")


def _columns(view: str, include_phones: bool) -> list[str]:
    if view == "companies":
        return list(COMPANY_COLUMNS)
    return [c for c in CONTACT_COLUMNS if include_phones or c != "direct_phone"]


async def _own(ts, lines: list[Line]) -> dict[str, dict]:
    """The workspace's own values for held rows, keyed by record id, in two queries at most."""
    contact_ids = [ln.held.record_id for ln in lines if ln.held and ln.held.kind == "contact"]
    account_ids = [ln.held.record_id for ln in lines if ln.held and ln.held.kind == "account"]
    out: dict[str, dict] = {}
    if contact_ids:
        rows = (await ts.session.execute(
            select(Contact, Account).join(Account, Account.id == Contact.account_id)
            .where(Contact.tenant_id == ts.tenant_id, Contact.id.in_(contact_ids)))).all()
        for c, a in rows:
            first, _, last = (c.full_name or "").partition(" ")
            where = (c.custom_fields or {}).get("location") or {}
            out[c.id] = {
                "first_name": first, "last_name": last, "title": c.title or "",
                "department": (c.custom_fields or {}).get("department", ""),
                "seniority": c.seniority or "", "email": c.email or "",
                "direct_phone": c.phone or "", "company_name": a.name or "",
                "company_domain": a.domain or "", "country": where.get("country", ""),
                "state": where.get("state", ""), "city": where.get("city", ""),
            }
    if account_ids:
        rows = (await ts.session.scalars(select(Account).where(
            Account.tenant_id == ts.tenant_id, Account.id.in_(account_ids)))).all()
        for a in rows:
            custom = a.custom_fields or {}
            out[a.id] = {
                "company_name": a.name or "", "domain": a.domain or "",
                "country": a.country or "", "state": a.region or "",
                "city": custom.get("city", ""), "industry": a.industry or "",
                "employee_size_range": custom.get("employee_size_range", ""),
                "linkedin_url": custom.get("linkedin_url", ""),
            }
    return out


def _vendor(record: VendorContact | VendorCompany) -> dict:
    if isinstance(record, VendorCompany):
        return {
            "company_name": record.company_name, "domain": domain_of(record.company_url),
            "country": record.country, "state": record.state, "city": record.city,
            "industry": record.industry, "employee_size_range": record.employee_size_range,
            "linkedin_url": record.linkedin_url,
        }
    return {
        "first_name": record.first_name, "last_name": record.last_name, "title": record.title,
        "department": record.department, "seniority": record.seniority, "email": record.email,
        "direct_phone": record.direct_phone, "company_name": record.company_name,
        "company_domain": domain_of(record.company_url), "country": record.country,
        "state": record.state, "city": record.city,
    }


async def export_rows(ts, *, user_id: str, selection: Selection, include_phones: bool,
                      request_id: str, client=None) -> tuple[str, list[str], list[list]]:
    q, bought = await prepare(ts, selection=selection, action="export",
                              include_phones=include_phones, client=client)
    await charges.preflight_all(ts, q.charges)
    async with ts.session.begin_nested():
        await charges.record_purchases(ts, q.lines, request_id=request_id, links={})
        await charges.meter_all(ts, q.charges, request_id=request_id, user_id=user_id)
    await record_browse_cost(ts, rows=bought, user_id=user_id, view=selection.view)

    columns = _columns(selection.view, include_phones)
    own = await _own(ts, q.lines)
    rows = []
    for line in q.lines:
        values = own.get(line.held.record_id, {}) if line.held else _vendor(line.record)
        rows.append([safe_cell(values.get(column, "")) for column in columns])
    filename = f"prospector-{selection.view}.csv"
    return filename, columns, rows
