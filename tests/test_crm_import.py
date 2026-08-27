# tests/test_crm_import.py
"""Pulling accounts and contacts FROM a connected CRM.

The CRM connection only ever pushed outward — `push_account` writes NEXUS-enriched firmographics
back to HubSpot or Salesforce. There was no way for a customer to say "bring my book across", and
no contact pull at all.

Two properties carry the weight here:

* **The count is bounded.** Every imported account enters the refresh pipeline and starts spending
  credits, so an import that quietly pulls a 100,000-row CRM is a bill nobody agreed to.
* **A person is attached to the right company.** The CRM's own account domain, then the account
  name as the CRM spells it, then the email domain. Going straight to the email domain puts
  everyone at an agency, a subsidiary or a personal address onto the wrong company — and a contact
  on the wrong account is a rep phoning a stranger with someone else's context.
"""
from __future__ import annotations

from sqlalchemy import select

from nexus.core.tenancy import TenantSession
from nexus.ingestion.crm import CRMAccount, CRMContact
from nexus.models.account import Account, Contact
from nexus.models.identity import Tenant


class FakeCRM:
    """A connector that answers from fixtures, so these rules are testable with no live CRM."""

    source = "hubspot"

    def __init__(self, accounts=None, contacts=None, fail: str | None = None):
        self._accounts = accounts or []
        self._contacts = contacts or []
        self._fail = fail
        self.contacts_limit_seen: int | None = None

    async def fetch_accounts(self):
        if self._fail == "accounts":
            raise RuntimeError("CRM is down")
        return list(self._accounts)

    async def fetch_contacts(self, *, limit: int = 200):
        if self._fail == "contacts":
            raise RuntimeError("CRM is down")
        self.contacts_limit_seen = limit
        return list(self._contacts)[:limit]


async def _ts(session, slug: str = "crm") -> TenantSession:
    tenant = Tenant(name=slug.upper(), slug=slug)
    session.add(tenant)
    await session.flush()
    return TenantSession(session, tenant.id)


# ---- limits ----------------------------------------------------------------------------------

def test_the_limit_is_clamped():
    from nexus.imports.crm_pull import DEFAULT_LIMIT, MAX_LIMIT, clamp_limit

    assert clamp_limit(None) == DEFAULT_LIMIT, "no limit must not mean unlimited"
    assert clamp_limit(0) == 1
    assert clamp_limit(-5) == 1
    assert clamp_limit(50) == 50
    assert clamp_limit(10_000_000) == MAX_LIMIT, "an unbounded pull is an unbounded bill"


async def test_only_the_requested_number_of_accounts_is_imported(fresh_db):
    from nexus.core.db import get_sessionmaker
    from nexus.imports.crm_pull import import_accounts_from_crm

    remote = [CRMAccount(external_id=str(i), name=f"Co {i}", domain=f"co{i}.com")
              for i in range(50)]
    async with get_sessionmaker()() as s:
        ts = await _ts(s)
        result = await import_accounts_from_crm(ts, FakeCRM(accounts=remote), limit=10)
        assert result["created"] == 10
        assert len((await s.scalars(select(Account))).all()) == 10


async def test_the_limit_reaches_the_connector_for_contacts(fresh_db):
    """Bounding after the fetch still downloads everything. The limit must go down to the query."""
    from nexus.core.db import get_sessionmaker
    from nexus.imports.crm_pull import import_contacts_from_crm

    crm = FakeCRM(contacts=[CRMContact(external_id="1", full_name="A", email="a@x.com")])
    async with get_sessionmaker()() as s:
        ts = await _ts(s)
        await import_contacts_from_crm(ts, crm, limit=25)
        assert crm.contacts_limit_seen == 25


# ---- accounts --------------------------------------------------------------------------------

async def test_accounts_import_and_keep_the_crm_link(fresh_db):
    """crm_id / crm_source are what let a later push update rather than create a duplicate."""
    from nexus.core.db import get_sessionmaker
    from nexus.imports.crm_pull import import_accounts_from_crm

    remote = [CRMAccount(external_id="hs-1", name="Acme Corp", domain="acme.com",
                         industry="SaaS", country="United States", employee_count=200)]
    async with get_sessionmaker()() as s:
        ts = await _ts(s)
        await import_accounts_from_crm(ts, FakeCRM(accounts=remote))
        row = (await s.scalars(select(Account))).one()
        assert (row.name, row.domain, row.industry) == ("Acme Corp", "acme.com", "SaaS")
        assert row.employee_count == 200
        assert (row.crm_id, row.crm_source) == ("hs-1", "hubspot")


async def test_a_second_pull_updates_rather_than_duplicating(fresh_db):
    from nexus.core.db import get_sessionmaker
    from nexus.imports.crm_pull import import_accounts_from_crm

    remote = [CRMAccount(external_id="hs-1", name="Acme Corp", domain="acme.com")]
    async with get_sessionmaker()() as s:
        ts = await _ts(s)
        first = await import_accounts_from_crm(ts, FakeCRM(accounts=remote))
        second = await import_accounts_from_crm(ts, FakeCRM(accounts=remote))
        assert (first["created"], second["created"]) == (1, 0)
        assert second["updated"] == 1
        assert len((await s.scalars(select(Account))).all()) == 1


async def test_a_crm_account_matches_an_existing_row_by_domain(fresh_db):
    """A customer who already worked the account by hand must not get a second copy of it."""
    from nexus.core.db import get_sessionmaker
    from nexus.imports.crm_pull import import_accounts_from_crm

    async with get_sessionmaker()() as s:
        ts = await _ts(s)
        ts.add(Account(tenant_id=ts.tenant_id, name="Acme", domain="acme.com"))
        await ts.flush()
        result = await import_accounts_from_crm(
            ts, FakeCRM(accounts=[CRMAccount(external_id="hs-9", name="Acme Inc",
                                             domain="https://www.acme.com/")]))
        assert result["updated"] == 1
        row = (await s.scalars(select(Account))).one()
        assert row.crm_id == "hs-9", "the CRM link must be recorded on the row we already had"


# ---- contacts --------------------------------------------------------------------------------

async def test_a_contact_attaches_by_the_crm_account_domain(fresh_db):
    from nexus.core.db import get_sessionmaker
    from nexus.imports.crm_pull import import_contacts_from_crm

    async with get_sessionmaker()() as s:
        ts = await _ts(s)
        ts.add(Account(tenant_id=ts.tenant_id, name="Acme", domain="acme.com"))
        await ts.flush()
        await import_contacts_from_crm(ts, FakeCRM(contacts=[
            CRMContact(external_id="c1", full_name="Jane Roe", email="jane@personal-gmail.com",
                       title="Director of Facilities", account_domain="acme.com"),
        ]))
        contact = (await s.scalars(select(Contact))).one()
        account = (await s.scalars(select(Account))).one()
        assert contact.account_id == account.id, (
            "the CRM said which company this person belongs to; falling back to the email domain "
            "would have attached them to a personal-email 'company'"
        )
        assert contact.title == "Director of Facilities"


async def test_a_contact_falls_back_to_the_account_name(fresh_db):
    """HubSpot returns a company NAME on the contact, not always a domain."""
    from nexus.core.db import get_sessionmaker
    from nexus.imports.crm_pull import import_contacts_from_crm

    async with get_sessionmaker()() as s:
        ts = await _ts(s)
        ts.add(Account(tenant_id=ts.tenant_id, name="Acme Corp", domain="acme.com"))
        await ts.flush()
        await import_contacts_from_crm(ts, FakeCRM(contacts=[
            CRMContact(external_id="c1", full_name="Jane Roe", email="jane@x.com",
                       account_name="Acme Corp"),
        ]))
        contact = (await s.scalars(select(Contact))).one()
        assert contact.account_id == (await s.scalars(select(Account))).one().id


async def test_re_pulling_does_not_duplicate_a_contact(fresh_db):
    from nexus.core.db import get_sessionmaker
    from nexus.imports.crm_pull import import_contacts_from_crm

    remote = [CRMContact(external_id="c1", full_name="Jane Roe", email="Jane@Acme.com",
                         account_domain="acme.com")]
    async with get_sessionmaker()() as s:
        ts = await _ts(s)
        first = await import_contacts_from_crm(ts, FakeCRM(contacts=remote))
        second = await import_contacts_from_crm(ts, FakeCRM(contacts=remote))
        assert (first["created"], second["created"]) == (1, 0)
        assert len((await s.scalars(select(Contact))).all()) == 1


async def test_a_blank_crm_field_never_erases_a_stored_value(fresh_db):
    """A sparsely-filled CRM record must not wipe a title enrichment established."""
    from nexus.core.db import get_sessionmaker
    from nexus.imports.crm_pull import import_contacts_from_crm

    async with get_sessionmaker()() as s:
        ts = await _ts(s)
        await import_contacts_from_crm(ts, FakeCRM(contacts=[
            CRMContact(external_id="c1", full_name="Jane Roe", email="jane@acme.com",
                       title="Director of Facilities", account_domain="acme.com")]))
        await import_contacts_from_crm(ts, FakeCRM(contacts=[
            CRMContact(external_id="c1", full_name="Jane Roe", email="jane@acme.com",
                       title=None, account_domain="acme.com")]))
        assert (await s.scalars(select(Contact))).one().title == "Director of Facilities"


# ---- failure posture -------------------------------------------------------------------------

async def test_a_crm_outage_reports_rather_than_raising(fresh_db):
    """The import screen must say what went wrong, not return a 500."""
    from nexus.core.db import get_sessionmaker
    from nexus.imports.crm_pull import import_accounts_from_crm, import_contacts_from_crm

    async with get_sessionmaker()() as s:
        ts = await _ts(s)
        a = await import_accounts_from_crm(ts, FakeCRM(fail="accounts"))
        c = await import_contacts_from_crm(ts, FakeCRM(fail="contacts"))
        for result in (a, c):
            assert result["created"] == 0
            assert result["errors"], "a failed pull must explain itself"


async def test_a_connector_without_contact_support_returns_nothing(fresh_db):
    """`fetch_contacts` is concrete on the base class, returning [], so adding it cannot break a
    connector that does not implement it."""
    from nexus.ingestion.crm import CRMConnector

    assert hasattr(CRMConnector, "fetch_contacts")
