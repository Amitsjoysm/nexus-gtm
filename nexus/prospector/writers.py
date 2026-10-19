"""Write provider records into the workspace as Accounts and Contacts.

* **Accounts** go through ``accounts.dedupe.find_existing_account`` (normalised domain, else exact
  normalised name), so a company the workspace already has, under any spelling of its domain, is
  reused and never duplicated. An existing account is not modified: a held record is the
  workspace's, and a provider value must not overwrite what a person or a CRM put there.
* **The LinkedIn URL** is kept only when ``enrichment.linkedin.company_page_url`` says it IS a
  company page (CLAUDE.md "LinkedIn URLs"); anything else is dropped and reported.
* **Contacts** are matched by normalised email in the workspace, else by name in the same account.
  A new one is saved with ``enrichment_source="sourcing:prospector"`` (the campaign hold, D19), the
  provider's email as delivered with no verdict, the direct phone only when it was bought, and the
  department and the person's location in ``custom_fields`` (read by the Saved tab).
"""
from __future__ import annotations

from sqlalchemy import func

from nexus.accounts.dedupe import find_existing_account, normalise_on_write
from nexus.enrichment.linkedin import company_page_url
from nexus.models.account import Account, Contact
from nexus.prospector.hold import SOURCE
from nexus.prospector.identity import domain_of
from nexus.prospector.records import VendorContact


async def upsert_account(ts, *, name: str, url: str, country: str = "", state: str = "",
                         city: str = "", industry: str = "", employee_size_range: str = "",
                         linkedin_url: str = "", owner_user_id: str | None = None,
                         ) -> tuple[Account, bool, bool]:
    """``(account, created, linkedin_dropped)``."""
    domain = domain_of(url)
    existing = await find_existing_account(ts, domain=domain or None, name=name)
    if existing is not None:
        return existing, False, False
    page = company_page_url(linkedin_url) if linkedin_url else None
    custom: dict[str, object] = {}
    if city:
        custom["city"] = city
    if employee_size_range:
        custom["employee_size_range"] = employee_size_range
    if page:
        custom["linkedin_url"] = page
    account = Account(
        tenant_id=ts.tenant_id, name=(name or domain or "Unknown company")[:255],
        domain=normalise_on_write(domain) if domain else None,
        country=(country or None), region=(state or None), industry=(industry or None),
        source="prospector", owner_user_id=owner_user_id, custom_fields=custom,
    )
    ts.add(account)
    await ts.flush()
    return account, True, bool(linkedin_url) and page is None


async def upsert_contact(ts, account: Account, record: VendorContact, *,
                         include_phone: bool) -> tuple[Contact, bool]:
    """``(contact, created)``."""
    email = (record.email or "").strip()
    if email:
        existing = await ts.first(Contact, func.lower(Contact.email) == email.lower(),
                                  Contact.deleted_at.is_(None))
        if existing is not None:
            return existing, False
    full_name = record.full_name or email or "Unknown"
    existing = await ts.first(Contact, Contact.account_id == account.id,
                              func.lower(Contact.full_name) == full_name.lower(),
                              Contact.deleted_at.is_(None))
    if existing is not None:
        return existing, False
    location = {k: v for k, v in (("country", record.country), ("state", record.state),
                                  ("city", record.city), ("zip", record.zip)) if v}
    custom: dict[str, object] = {}
    if record.department:
        custom["department"] = record.department
    if location:
        custom["location"] = location
    contact = Contact(
        tenant_id=ts.tenant_id, account_id=account.id, full_name=full_name[:200],
        title=(record.title or None), seniority=(record.seniority[:40] or None),
        email=(email or None), phone=(record.direct_phone[:40] if include_phone and
                                      record.direct_phone else None),
        enrichment_source=SOURCE, email_status=None, custom_fields=custom,
    )
    ts.add(contact)
    await ts.flush()
    return contact, True
