"""Lists built by hand: companies or people, chosen one at a time or in bulk.

A list used to be only a saved filter over accounts, made once and never opened again. It now has a
kind, holds exactly the members someone put in it, and is archived rather than deleted.

Rules that are easy to break:

* **A member is validated against the caller's workspace, and a stranger's id is skipped.** Every
  lookup goes through the TenantSession, so an id from another workspace simply is not found; it is
  counted as ``skipped`` rather than raised, because a bulk add of 300 rows should not fail for one.
* **Kinds do not mix.** An account list takes companies and a contact list takes people. Mixing them
  would make "start a campaign from this list" mean two different things for one list.
* **Archive, never delete, the list row.** The old ``campaigns.list_id`` is NOT NULL and engagement
  campaigns keep ``source_list_id``. The members ARE deleted, so a list nobody can see stops keeping
  its companies on the hot refresh cycle (``tiering._on_a_list``).
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import delete, func, or_, select

from nexus.core.db import utcnow
from nexus.core.tenancy import TenantSession
from nexus.models.account import Account, Contact
from nexus.models.workflow import ListItem, ProspectList

KINDS = ("account", "contact")
#: The most ids one add or remove call accepts. The Accounts page shows at most 200 rows and a
#: discovery page 25, so this is headroom, not a limit anyone reaches by clicking.
MAX_IDS = 2000


class ListError(ValueError):
    """A request the list cannot honour, phrased for the person who made it."""


@dataclass(slots=True)
class AddResult:
    added: int
    already: int
    skipped: int
    members: int


def _clean(ids) -> list[str]:
    """Distinct, non-empty ids in the order given."""
    seen: dict[str, None] = {}
    for raw in ids or ():
        value = (raw or "").strip()
        if value:
            seen.setdefault(value, None)
    return list(seen)


async def get_live(ts: TenantSession, list_id: str) -> ProspectList | None:
    """The list if it exists in this workspace and has not been archived."""
    plist = await ts.get(ProspectList, list_id)
    if plist is None or plist.archived_at is not None:
        return None
    return plist


async def live_lists(ts: TenantSession, kind: str | None = None) -> list[ProspectList]:
    where = [ProspectList.archived_at.is_(None)]
    if kind:
        where.append(ProspectList.kind == kind)
    rows = await ts.list(ProspectList, *where)
    rows.sort(key=lambda pl: pl.created_at, reverse=True)
    return rows


async def counts(ts: TenantSession, list_ids: list[str]) -> dict[str, tuple[int, int]]:
    """``{list_id: (members, distinct accounts)}`` in one query."""
    if not list_ids:
        return {}
    rows = (await ts.session.execute(
        select(ListItem.list_id, func.count(), func.count(func.distinct(ListItem.account_id)))
        .where(ListItem.tenant_id == ts.tenant_id, ListItem.list_id.in_(list_ids))
        .group_by(ListItem.list_id)
    )).all()
    return {list_id: (int(members), int(accounts)) for list_id, members, accounts in rows}


async def member_count(ts: TenantSession, list_id: str) -> int:
    return (await counts(ts, [list_id])).get(list_id, (0, 0))[0]


async def create(ts: TenantSession, *, name: str, kind: str,
                 owner_user_id: str | None) -> ProspectList:
    name = (name or "").strip()
    if not name:
        raise ListError("Give the list a name.")
    if kind not in KINDS:
        raise ListError("A list holds either companies (account) or people (contact).")
    plist = ProspectList(tenant_id=ts.tenant_id, name=name[:200], kind=kind,
                         owner_user_id=owner_user_id, filter={})
    ts.add(plist)
    await ts.flush()
    return plist


async def add_members(ts: TenantSession, plist: ProspectList, *, account_ids=(),
                      contact_ids=()) -> AddResult:
    account_ids, contact_ids = _clean(account_ids), _clean(contact_ids)
    if len(account_ids) + len(contact_ids) > MAX_IDS:
        raise ListError(f"Add at most {MAX_IDS} at a time.")
    if plist.kind == "account" and contact_ids:
        raise ListError("This is an account list: add companies to it, or use a contact list "
                        "for people.")
    if plist.kind == "contact" and account_ids:
        raise ListError("This is a contact list: add people to it, or use an account list for "
                        "companies.")

    existing = await ts.list(ListItem, ListItem.list_id == plist.id)
    added = already = skipped = 0
    if plist.kind == "account":
        held = {i.account_id for i in existing if not i.contact_id}
        found = {a.id for a in await ts.list(Account, Account.id.in_(account_ids))} \
            if account_ids else set()
        for account_id in account_ids:
            if account_id not in found:
                skipped += 1
            elif account_id in held:
                already += 1
            else:
                ts.add(ListItem(tenant_id=ts.tenant_id, list_id=plist.id, account_id=account_id))
                held.add(account_id)
                added += 1
    else:
        held = {i.contact_id for i in existing if i.contact_id}
        people = {c.id: c for c in await ts.list(
            Contact, Contact.id.in_(contact_ids), Contact.deleted_at.is_(None))} \
            if contact_ids else {}
        for contact_id in contact_ids:
            person = people.get(contact_id)
            if person is None:
                skipped += 1
            elif contact_id in held:
                already += 1
            else:
                ts.add(ListItem(tenant_id=ts.tenant_id, list_id=plist.id,
                                account_id=person.account_id, contact_id=contact_id))
                held.add(contact_id)
                added += 1
    if added:
        plist.updated_at = utcnow()
    await ts.flush()
    return AddResult(added=added, already=already, skipped=skipped, members=len(held))


async def remove_members(ts: TenantSession, plist: ProspectList, *, account_ids=(),
                         contact_ids=()) -> int:
    account_ids, contact_ids = _clean(account_ids), _clean(contact_ids)
    clauses = []
    if account_ids:
        clauses.append((ListItem.account_id.in_(account_ids)) & ListItem.contact_id.is_(None))
    if contact_ids:
        clauses.append(ListItem.contact_id.in_(contact_ids))
    if not clauses:
        return 0
    result = await ts.session.execute(
        delete(ListItem)
        .where(ListItem.tenant_id == ts.tenant_id, ListItem.list_id == plist.id, or_(*clauses))
        .execution_options(synchronize_session=False)
    )
    removed = int(result.rowcount or 0)
    if removed:
        plist.updated_at = utcnow()
    await ts.flush()
    return removed


async def rename(ts: TenantSession, plist: ProspectList, name: str) -> ProspectList:
    name = (name or "").strip()
    if not name:
        raise ListError("Give the list a name.")
    plist.name = name[:200]
    await ts.flush()
    return plist


async def archive(ts: TenantSession, plist: ProspectList) -> None:
    await ts.session.execute(
        delete(ListItem)
        .where(ListItem.tenant_id == ts.tenant_id, ListItem.list_id == plist.id)
        .execution_options(synchronize_session=False)
    )
    plist.archived_at = utcnow()
    await ts.flush()


@dataclass(slots=True)
class Member:
    account_id: str
    account_name: str
    domain: str | None
    industry: str | None
    country: str | None
    employee_count: int | None
    contact_id: str | None = None
    full_name: str | None = None
    title: str | None = None
    email: str | None = None
    email_status: str | None = None
    added_at: object = None


async def members(ts: TenantSession, plist: ProspectList, *, limit: int = 100, offset: int = 0,
                  q: str | None = None) -> tuple[int, list[Member]]:
    """One page of members, newest first, and the total that matches ``q``."""
    if plist.kind == "contact":
        stmt = (select(ListItem, Contact, Account)
                .join(Contact, Contact.id == ListItem.contact_id)
                .join(Account, Account.id == ListItem.account_id))
    else:
        stmt = (select(ListItem, Account)
                .join(Account, Account.id == ListItem.account_id)
                .where(ListItem.contact_id.is_(None)))
    stmt = stmt.where(ListItem.tenant_id == ts.tenant_id, ListItem.list_id == plist.id)
    if q and q.strip():
        like = f"%{q.strip().lower()}%"
        fields = [func.lower(Account.name), func.lower(func.coalesce(Account.domain, ""))]
        if plist.kind == "contact":
            fields += [func.lower(Contact.full_name), func.lower(func.coalesce(Contact.email, "")),
                       func.lower(func.coalesce(Contact.title, ""))]
        stmt = stmt.where(or_(*[f.like(like) for f in fields]))
    total = int(await ts.session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = (await ts.session.execute(
        stmt.order_by(ListItem.created_at.desc()).limit(max(1, min(limit, 500)))
        .offset(max(0, offset))
    )).all()
    out: list[Member] = []
    for row in rows:
        if plist.kind == "contact":
            item, person, account = row
        else:
            (item, account), person = row, None
        out.append(Member(
            account_id=account.id, account_name=account.name, domain=account.domain,
            industry=account.industry, country=account.country,
            employee_count=account.employee_count,
            contact_id=getattr(person, "id", None), full_name=getattr(person, "full_name", None),
            title=getattr(person, "title", None), email=getattr(person, "email", None),
            email_status=getattr(person, "email_status", None), added_at=item.created_at,
        ))
    return total, out


async def campaign_contact_ids(ts: TenantSession, plist: ProspectList) -> list[str]:
    """Who a campaign built from this list emails: the people a contact list names, or everyone
    with an address at the companies an account list holds. Deleted contacts never."""
    items = await ts.list(ListItem, ListItem.list_id == plist.id)
    named = {i.contact_id for i in items if i.contact_id}
    whole = {i.account_id for i in items if not i.contact_id}
    clauses = []
    if named:
        clauses.append(Contact.id.in_(named))
    if whole:
        clauses.append(Contact.account_id.in_(whole))
    if not clauses:
        return []
    people = await ts.list(Contact, or_(*clauses), Contact.deleted_at.is_(None),
                           Contact.email.is_not(None))
    return [c.id for c in people if (c.email or "").strip()]
