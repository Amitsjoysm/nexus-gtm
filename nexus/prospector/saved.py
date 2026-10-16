"""The Saved tab (decisions D13, D14): the workspace's own Accounts and Contacts, filtered.

It never calls the provider: these are the tenant's records, so the tab is free, fast and shows real
values. Every filter is translated to SQL over columns the product already stores, AND across
sections and OR within one, and paginated in the database. Filters workspace records do not store
(``fields.FieldSpec.saved=False``, today SIC codes) are refused with a sentence, never ignored.

Where values live (spec §7): company location on ``Account.country/region/postal_code`` and
``custom_fields["city"]``; the person's location on ``Contact.custom_fields["location"]``, written
by Prospector when it saves a contact; department in ``Contact.custom_fields["department"]``; tech
stack is a JSON list, matched on its quoted text so one expression serves SQLite and Postgres.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import String, and_, cast, exists, func, not_, or_, select

from nexus.core.db import utcnow
from nexus.models.account import Account, Contact
from nexus.models.signal import SignalEvent
from nexus.models.workflow import ListItem
from nexus.companies.resolution import normalise_domain
from nexus.prospector import fields as catalog
from nexus.prospector.errors import ProspectorError
from nexus.prospector.filters import SearchFilters, WorkspaceFilters
from nexus.prospector.ranges import parse_range
from nexus.prospector.schemas import SavedOut, SavedRowOut

JOB_POSTING_KINDS = ("job_posting", "hiring")


def _validate(view: str, filters: SearchFilters) -> None:
    for key in filters.filled():
        section, spec = catalog.section_of(key), catalog.field(key)
        if view not in section.views:
            raise ProspectorError(
                422, f"{section.label} is not a filter for {view}. Clear it or switch views.")
        if not spec.saved:
            raise ProspectorError(
                422, f"{section.label}: {spec.label} is not stored for workspace records, so it "
                     "cannot filter the Saved tab.")


def _lower(column):
    return func.lower(func.coalesce(column, ""))


def _any_like(column, values: list[str]):
    return or_(*[_lower(column).like(f"%{v.lower()}%") for v in values])


def _any_in(column, values: list[str]):
    return _lower(column).in_([v.lower() for v in values])


def _json_text(column, *path: str):
    target = column[path[0]] if len(path) == 1 else column[path]
    return target.as_string()


def _ranges(column, labels: list[str]):
    clauses = []
    for label in labels:
        bounds = parse_range(label)
        if bounds is None:
            continue
        low, high = bounds
        clauses.append(and_(column >= low, column <= high) if high is not None else column >= low)
    return or_(*clauses) if clauses else None


def _company_clauses(f: SearchFilters) -> list:
    out = []
    if f.company_names:
        out.append(_any_like(Account.name, f.company_names))
    if f.company_domains:
        domains = [d for d in (normalise_domain(x) for x in f.company_domains) if d]
        out.append(Account.domain.in_(domains or ["-"]))
    if f.company_countries:
        out.append(_any_in(Account.country, f.company_countries))
    if f.company_states:
        out.append(_any_in(Account.region, f.company_states))
    if f.company_cities:
        out.append(_any_in(_json_text(Account.custom_fields, "city"), f.company_cities))
    if f.company_zips:
        out.append(_any_in(Account.postal_code, f.company_zips))
    for column, labels in ((Account.employee_count, f.employee_sizes),
                           (Account.annual_revenue, f.revenue_ranges)):
        if labels:
            clause = _ranges(column, labels)
            if clause is None:
                raise ProspectorError(422, f"These ranges cannot be read: {', '.join(labels)}.")
            out.append(clause)
    if f.industries:
        out.append(_any_like(Account.industry, f.industries))
    if f.exclude_industries:
        out.append(not_(_any_like(Account.industry, f.exclude_industries)))
    sub = _json_text(Account.custom_fields, "sub_industry")
    if f.sub_industries:
        out.append(_any_like(sub, f.sub_industries))
    if f.exclude_sub_industries:
        out.append(not_(_any_like(sub, f.exclude_sub_industries)))
    if f.keywords or f.exclude_keywords:
        haystack = [Account.name, Account.industry, sub,
                    _json_text(Account.custom_fields, "keywords"),
                    _json_text(Account.custom_fields, "description")]
        if f.keywords:
            out.append(or_(*[_any_like(col, f.keywords) for col in haystack]))
        if f.exclude_keywords:
            out.append(not_(or_(*[_any_like(col, f.exclude_keywords) for col in haystack])))
    if f.technologies:
        stack = func.lower(cast(Account.tech_stack, String))
        out.append(or_(*[stack.like(f'%"{t.lower()}"%') for t in f.technologies]))
    if f.has_linkedin is not None:
        has = func.coalesce(_json_text(Account.custom_fields, "linkedin_url"), "") != ""
        out.append(has if f.has_linkedin else not_(has))
    return out


def _contact_clauses(f: SearchFilters) -> list:
    out = []
    if f.first_name:
        out.append(_lower(Contact.full_name).like(f"{f.first_name.lower()}%"))
    if f.last_name:
        out.append(_lower(Contact.full_name).like(f"%{f.last_name.lower()}"))
    if f.titles:
        out.append(_any_like(Contact.title, f.titles))
    if f.departments:
        out.append(_any_like(_json_text(Contact.custom_fields, "department"), f.departments))
    if f.seniorities:
        out.append(_any_in(Contact.seniority, f.seniorities))
    for key, values in (("country", f.person_countries), ("state", f.person_states),
                        ("city", f.person_cities), ("zip", f.person_zips)):
        if values:
            out.append(_any_in(_json_text(Contact.custom_fields, "location", key), values))
    if f.has_direct_phone is not None:
        has = func.coalesce(Contact.phone, "") != ""
        out.append(has if f.has_direct_phone else not_(has))
    return out


def _signal_exists(kinds, days: int):
    since = utcnow() - timedelta(days=days)
    return exists(select(SignalEvent.id).where(
        SignalEvent.account_id == Account.id, SignalEvent.tenant_id == Account.tenant_id,
        SignalEvent.kind.in_(list(kinds)), SignalEvent.occurred_at >= since))


def _workspace_clauses(view: str, w: WorkspaceFilters) -> list:
    out = []
    if w.email_statuses:
        if view != "contacts":
            raise ProspectorError(422, "Email status is a filter for contacts.")
        named = [s for s in w.email_statuses if s != "none"]
        clauses = [Contact.email_status.in_(named)] if named else []
        if "none" in w.email_statuses:
            clauses.append(func.coalesce(Contact.email_status, "") == "")
        out.append(or_(*clauses))
    if w.list_ids:
        if view == "contacts":
            out.append(or_(
                Contact.id.in_(select(ListItem.contact_id).where(
                    ListItem.list_id.in_(w.list_ids), ListItem.contact_id.isnot(None))),
                Contact.account_id.in_(select(ListItem.account_id).where(
                    ListItem.list_id.in_(w.list_ids), ListItem.contact_id.is_(None)))))
        else:
            out.append(Account.id.in_(select(ListItem.account_id).where(
                ListItem.list_id.in_(w.list_ids))))
    if w.signal_kinds:
        out.append(_signal_exists(w.signal_kinds, w.signal_days))
    if w.funding:
        out.append(_signal_exists(("funding",), w.signal_days))
    if w.job_postings:
        out.append(_signal_exists(JOB_POSTING_KINDS, w.signal_days))
    return out


async def saved_page(ts, *, view: str, filters: SearchFilters, workspace: WorkspaceFilters,
                     page: int, page_size: int) -> SavedOut:
    _validate(view, filters)
    where = [Account.tenant_id == ts.tenant_id, *_company_clauses(filters),
             *_workspace_clauses(view, workspace)]
    if view == "contacts":
        where += [Contact.tenant_id == ts.tenant_id, Contact.deleted_at.is_(None),
                  *_contact_clauses(filters)]
        base = select(Contact, Account).join(Account, Account.id == Contact.account_id)
        count = select(func.count()).select_from(Contact).join(
            Account, Account.id == Contact.account_id)
        order = (Contact.created_at.desc(), Contact.id.desc())
    else:
        where += [Account.archived_at.is_(None)]
        base = select(Account)
        count = select(func.count()).select_from(Account)
        order = (Account.created_at.desc(), Account.id.desc())

    total = int((await ts.session.execute(count.where(*where))).scalar() or 0)
    rows = (await ts.session.execute(
        base.where(*where).order_by(*order).offset((page - 1) * page_size).limit(page_size)
    )).all()
    if view == "contacts":
        out = [_contact_out(contact, account) for contact, account in rows]
    else:
        out = [_account_out(row[0]) for row in rows]
    return SavedOut(rows=out, page=page, page_size=page_size, total=total)


def _city(account: Account) -> str | None:
    value = (account.custom_fields or {}).get("city")
    return str(value) if value else None


def _contact_out(c: Contact, a: Account) -> SavedRowOut:
    return SavedRowOut(
        kind="contact", id=c.id, account_id=a.id, name=c.full_name or "", title=c.title or "",
        seniority=c.seniority or "", email=c.email, email_status=c.email_status, phone=c.phone,
        company_name=a.name or "", domain=a.domain, country=a.country, region=a.region,
        city=_city(a), industry=a.industry, employee_count=a.employee_count,
        enrichment_source=c.enrichment_source,
    )


def _account_out(a: Account) -> SavedRowOut:
    return SavedRowOut(
        kind="account", id=a.id, account_id=a.id, name=a.name or "", company_name=a.name or "",
        domain=a.domain, country=a.country, region=a.region, city=_city(a), industry=a.industry,
        employee_count=a.employee_count,
    )
