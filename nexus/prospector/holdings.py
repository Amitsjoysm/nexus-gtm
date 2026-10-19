"""What this workspace already holds, and what it has already paid for.

Two questions asked of every page, each answered in a FIXED number of queries whatever the page
size (no per-row lookups):

* ``held_contacts`` / ``held_companies``: which rows are already records here. A held row is shown
  "Saved" with the workspace's own values (it is the tenant's data, already on Contacts and
  Accounts) and is free to list or save, because nothing new is delivered (spec §9.1). A person is
  matched by normalised email, else by name at the company's domain, so a contact saved without an
  email is still recognised; a company by normalised domain, else by its exact normalised name when
  it has no domain (the ``accounts.dedupe`` rule).
* ``purchased``: which (record, field) pairs the ledger says were already paid for (D6).

Everything goes through the ``TenantSession``: another workspace's records can never be seen here.
Archived accounts count as held, like ``find_existing_account``; deleted contacts do not.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import and_, func, or_, select

from nexus.accounts.dedupe import normalise_name
from nexus.companies.resolution import normalise_domain
from nexus.models.account import Account, Contact
from nexus.models.prospector import ProspectorPurchase
from nexus.prospector.identity import company_identity, contact_identity, domain_of
from nexus.prospector.records import VendorCompany, VendorContact


@dataclass(slots=True)
class Held:
    kind: Literal["contact", "account"]
    record_id: str
    account_id: str | None
    email: str | None = None
    phone: str | None = None
    name: str = ""
    domain: str | None = None


def _name_key(full_name: str, domain: str) -> str:
    name = " ".join((full_name or "").lower().split())
    return f"n:{name}|{domain}" if name and domain else ""


async def held_contacts(ts, records: list[VendorContact]) -> dict[str, Held]:
    """``{identity: Held}`` for the records this workspace already has as live contacts."""
    wanted: dict[str, tuple[str, str]] = {}          # identity -> (email key, name key)
    for record in records:
        ident = contact_identity(record)
        if not ident:
            continue
        email_key = ident if ident.startswith("e:") else ""
        wanted[ident] = (email_key, _name_key(record.full_name, domain_of(record.company_url)))
    if not wanted:
        return {}

    by_email: dict[str, Held] = {}
    emails = sorted({e[2:] for e, _ in wanted.values() if e})
    if emails:
        rows = (await ts.session.execute(
            select(Contact, Account.domain)
            .outerjoin(Account, and_(Account.id == Contact.account_id,
                                     Account.tenant_id == ts.tenant_id))
            .where(Contact.tenant_id == ts.tenant_id, Contact.deleted_at.is_(None),
                   func.lower(Contact.email).in_(emails)))).all()
        for c, domain in rows:
            by_email[f"e:{(c.email or '').strip().lower()}"] = _contact_held(c, domain)

    by_name: dict[str, Held] = {}
    names = sorted({n[2:].split("|", 1)[0] for _, n in wanted.values() if n})
    if names:
        rows = (await ts.session.execute(
            select(Contact, Account).join(Account, Account.id == Contact.account_id)
            .where(Contact.tenant_id == ts.tenant_id, Account.tenant_id == ts.tenant_id,
                   Contact.deleted_at.is_(None),
                   func.lower(Contact.full_name).in_(names)))).all()
        for c, a in rows:
            key = _name_key(c.full_name, normalise_domain(a.domain))
            if key:
                by_name.setdefault(key, _contact_held(c, a.domain))

    out: dict[str, Held] = {}
    for ident, (email_key, name_key) in wanted.items():
        held = (by_email.get(email_key) if email_key else None) or by_name.get(name_key)
        if held is not None:
            out[ident] = held
    return out


def _contact_held(c: Contact, domain: str | None) -> Held:
    """``domain`` is the contact's account's own website, so a Saved row never shows the mask."""
    return Held(kind="contact", record_id=c.id, account_id=c.account_id, email=c.email,
                phone=c.phone, name=c.full_name or "", domain=domain)


async def held_companies(ts, records: list[VendorCompany]) -> dict[str, Held]:
    """``{identity: Held}`` for the records this workspace already has as accounts."""
    idents = {company_identity(r) for r in records} - {""}
    if not idents:
        return {}
    out: dict[str, Held] = {}
    domains = sorted(i[2:] for i in idents if i.startswith("d:"))
    if domains:
        rows = (await ts.session.execute(
            select(Account).where(Account.tenant_id == ts.tenant_id,
                                  Account.domain.in_(domains)))).scalars().all()
        for a in rows:
            ident = f"d:{normalise_domain(a.domain)}"
            if ident in idents:
                out.setdefault(ident, _account_held(a))
    names = sorted(i[2:] for i in idents if i.startswith("n:"))
    if names:
        # A candidate filter (prefix match), then the exact normalised comparison in Python, as
        # `accounts.dedupe.find_existing_account` does: "Hooli, Inc." is stored, "hooli" is asked.
        rows = (await ts.session.execute(
            select(Account).where(Account.tenant_id == ts.tenant_id,
                                  func.coalesce(Account.domain, "") == "",
                                  or_(*[func.lower(Account.name).like(f"{n[:40]}%")
                                        for n in names])))).scalars().all()
        for a in rows:
            ident = f"n:{normalise_name(a.name)}"
            if ident in idents:
                out.setdefault(ident, _account_held(a))
    return out


def _account_held(a: Account) -> Held:
    return Held(kind="account", record_id=a.id, account_id=a.id, name=a.name or "",
                domain=a.domain)


async def purchased(ts, fingerprints: list[str]) -> set[tuple[str, str]]:
    """``{(fingerprint, field)}`` this workspace has already paid for."""
    unique = sorted(set(fingerprints))
    if not unique:
        return set()
    rows = (await ts.session.execute(
        select(ProspectorPurchase.fingerprint, ProspectorPurchase.field)
        .where(ProspectorPurchase.tenant_id == ts.tenant_id,
               ProspectorPurchase.fingerprint.in_(unique)))).all()
    return {(fp, field) for fp, field in rows}
