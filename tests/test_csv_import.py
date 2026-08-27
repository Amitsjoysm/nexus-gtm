# tests/test_csv_import.py
"""CSV import that CREATES accounts and contacts.

`custom_fields.import_csv` already existed and does something different: it *annotates* rows that
already match and **skips** everything else. So a GTM team arriving with a list of companies they
already work had no way to bring it in — the first blocker a tester reported on 2026-08-27, and the
one that stops an ops evaluation before it starts.

Identity is the normalised domain (accounts) and the normalised email (contacts), mirroring
`nexus/companies/` and `nexus/people/`. A name match is how two organisations become one row, and
that bug family has shipped six times in this codebase.
"""
from __future__ import annotations

from sqlalchemy import select

from nexus.core.tenancy import TenantSession
from nexus.models.account import Account, Contact
from nexus.models.identity import Tenant


async def _ts(session, slug: str = "imp") -> TenantSession:
    tenant = Tenant(name=slug.upper(), slug=slug)
    session.add(tenant)
    await session.flush()
    return TenantSession(session, tenant.id)


# ---- accounts --------------------------------------------------------------------------------

async def test_rows_become_accounts(fresh_db):
    from nexus.core.db import get_sessionmaker
    from nexus.imports.csv_ingest import import_accounts_csv

    async with get_sessionmaker()() as s:
        ts = await _ts(s)
        csv = b"company,website,country\nAcme Corp,acme.com,United States\nBeta Inc,beta.io,Canada\n"
        result = await import_accounts_csv(
            ts, content=csv,
            mapping={"company": "name", "website": "domain", "country": "country"},
        )
        assert result["created"] == 2
        assert result["skipped"] == 0
        rows = (await s.scalars(select(Account))).all()
        assert {r.name for r in rows} == {"Acme Corp", "Beta Inc"}
        assert {r.country for r in rows} == {"United States", "Canada"}


async def test_a_second_import_updates_rather_than_duplicating(fresh_db):
    """Re-uploading a corrected list must not double the book — the thing that makes an import
    tool untrustworthy the first time it happens."""
    from nexus.core.db import get_sessionmaker
    from nexus.imports.csv_ingest import import_accounts_csv

    async with get_sessionmaker()() as s:
        ts = await _ts(s)
        csv = b"company,website\nAcme Corp,acme.com\n"
        mapping = {"company": "name", "website": "domain"}
        first = await import_accounts_csv(ts, content=csv, mapping=mapping)
        second = await import_accounts_csv(ts, content=csv, mapping=mapping)
        assert (first["created"], second["created"]) == (1, 0)
        assert second["updated"] == 1
        assert len((await s.scalars(select(Account))).all()) == 1


async def test_the_domain_is_normalised(fresh_db):
    """'https://www.Acme.com/pricing' and 'acme.com' are one company."""
    from nexus.core.db import get_sessionmaker
    from nexus.imports.csv_ingest import import_accounts_csv

    async with get_sessionmaker()() as s:
        ts = await _ts(s)
        await import_accounts_csv(
            ts, content=b"company,website\nAcme,https://www.Acme.com/pricing\n",
            mapping={"company": "name", "website": "domain"},
        )
        assert (await s.scalars(select(Account))).one().domain == "acme.com"


async def test_a_row_with_no_name_and_no_domain_is_reported(fresh_db):
    """A silently dropped row looks like data loss, and the operator has no way to find it."""
    from nexus.core.db import get_sessionmaker
    from nexus.imports.csv_ingest import import_accounts_csv

    async with get_sessionmaker()() as s:
        ts = await _ts(s)
        result = await import_accounts_csv(
            ts, content=b"company,website\n,\nReal Co,real.com\n",
            mapping={"company": "name", "website": "domain"},
        )
        assert result["created"] == 1
        assert result["skipped"] == 1
        assert result["errors"] and "row 2" in result["errors"][0]


async def test_unmapped_columns_land_in_custom_fields(fresh_db):
    """An ops CSV always carries columns we have no column for, and they are usually the reason the
    list was built — territory, tier, owner. Dropping them throws that away."""
    from nexus.core.db import get_sessionmaker
    from nexus.imports.csv_ingest import import_accounts_csv

    async with get_sessionmaker()() as s:
        ts = await _ts(s)
        await import_accounts_csv(
            ts, content=b"company,website,segment,owner\nAcme,acme.com,Enterprise West,jo\n",
            mapping={"company": "name", "website": "domain"},
        )
        row = (await s.scalars(select(Account))).one()
        assert row.custom_fields.get("segment") == "Enterprise West"
        assert row.custom_fields.get("owner") == "jo"


async def test_a_blank_cell_never_erases_a_stored_value(fresh_db):
    """A partial re-upload — three columns exported out of a CRM — must not wipe firmographics the
    product already paid an enrichment provider for."""
    from nexus.core.db import get_sessionmaker
    from nexus.imports.csv_ingest import import_accounts_csv

    async with get_sessionmaker()() as s:
        ts = await _ts(s)
        await import_accounts_csv(
            ts, content=b"company,website,industry\nAcme,acme.com,SaaS\n",
            mapping={"company": "name", "website": "domain", "industry": "industry"},
        )
        await import_accounts_csv(
            ts, content=b"company,website,industry\nAcme,acme.com,\n",
            mapping={"company": "name", "website": "domain", "industry": "industry"},
        )
        assert (await s.scalars(select(Account))).one().industry == "SaaS"


async def test_numbers_with_commas_and_currency_parse(fresh_db):
    """Ops spreadsheets format numbers for humans."""
    from nexus.core.db import get_sessionmaker
    from nexus.imports.csv_ingest import import_accounts_csv

    async with get_sessionmaker()() as s:
        ts = await _ts(s)
        await import_accounts_csv(
            ts, content=b'company,website,staff,rev\nAcme,acme.com,"1,200","$25,000,000"\n',
            mapping={"company": "name", "website": "domain",
                     "staff": "employee_count", "rev": "annual_revenue"},
        )
        row = (await s.scalars(select(Account))).one()
        assert row.employee_count == 1200
        assert row.annual_revenue == 25_000_000


# ---- contacts --------------------------------------------------------------------------------

async def test_contacts_attach_to_an_existing_account(fresh_db):
    from nexus.core.db import get_sessionmaker
    from nexus.imports.csv_ingest import import_accounts_csv, import_contacts_csv

    async with get_sessionmaker()() as s:
        ts = await _ts(s)
        await import_accounts_csv(ts, content=b"company,website\nAcme,acme.com\n",
                                  mapping={"company": "name", "website": "domain"})
        result = await import_contacts_csv(
            ts,
            content=(b"name,email,role,company_domain\n"
                     b"Jane Roe,jane@acme.com,Director of Facilities,acme.com\n"),
            mapping={"name": "full_name", "email": "email", "role": "title",
                     "company_domain": "account_domain"},
        )
        assert result["created"] == 1
        contact = (await s.scalars(select(Contact))).one()
        assert contact.title == "Director of Facilities"
        assert len((await s.scalars(select(Account))).all()) == 1, "must not create a second account"


async def test_a_contact_for_an_unknown_company_creates_the_account(fresh_db):
    """An ops team uploads contacts without having uploaded companies first. Refusing would make
    the two imports order-dependent for a reason invisible from the upload screen."""
    from nexus.core.db import get_sessionmaker
    from nexus.imports.csv_ingest import import_contacts_csv

    async with get_sessionmaker()() as s:
        ts = await _ts(s)
        await import_contacts_csv(
            ts, content=b"name,email,company_domain\nJohn Doe,john@newco.com,newco.com\n",
            mapping={"name": "full_name", "email": "email", "company_domain": "account_domain"},
        )
        assert (await s.scalars(select(Account))).one().domain == "newco.com"


async def test_the_account_falls_back_to_the_email_domain(fresh_db):
    """No company column at all — right far more often than wrong for a work address, and a contact
    with no account cannot be actioned."""
    from nexus.core.db import get_sessionmaker
    from nexus.imports.csv_ingest import import_contacts_csv

    async with get_sessionmaker()() as s:
        ts = await _ts(s)
        await import_contacts_csv(
            ts, content=b"name,email\nJane Roe,jane@acme.com\n",
            mapping={"name": "full_name", "email": "email"},
        )
        assert (await s.scalars(select(Account))).one().domain == "acme.com"


async def test_re_importing_does_not_duplicate_a_contact(fresh_db):
    from nexus.core.db import get_sessionmaker
    from nexus.imports.csv_ingest import import_contacts_csv

    async with get_sessionmaker()() as s:
        ts = await _ts(s)
        csv = b"name,email,company_domain\nJane Roe,Jane@Acme.com,acme.com\n"
        mapping = {"name": "full_name", "email": "email", "company_domain": "account_domain"}
        first = await import_contacts_csv(ts, content=csv, mapping=mapping)
        second = await import_contacts_csv(ts, content=csv, mapping=mapping)
        assert (first["created"], second["created"]) == (1, 0)
        assert second["updated"] == 1
        assert len((await s.scalars(select(Contact))).all()) == 1


async def test_a_row_with_no_email_is_reported(fresh_db):
    from nexus.core.db import get_sessionmaker
    from nexus.imports.csv_ingest import import_contacts_csv

    async with get_sessionmaker()() as s:
        ts = await _ts(s)
        result = await import_contacts_csv(
            ts, content=b"name,email,company_domain\nNo Email,,acme.com\n",
            mapping={"name": "full_name", "email": "email", "company_domain": "account_domain"},
        )
        assert result["skipped"] == 1
        assert result["errors"]


async def test_phone_and_linkedin_are_importable(fresh_db):
    from nexus.core.db import get_sessionmaker
    from nexus.imports.csv_ingest import import_contacts_csv

    async with get_sessionmaker()() as s:
        ts = await _ts(s)
        await import_contacts_csv(
            ts,
            content=(b"name,email,phone,li\n"
                     b"Jane Roe,jane@acme.com,+14155550123,https://linkedin.com/in/janeroe\n"),
            mapping={"name": "full_name", "email": "email", "phone": "phone",
                     "li": "linkedin_url"},
        )
        contact = (await s.scalars(select(Contact))).one()
        assert contact.phone == "+14155550123"
        assert contact.linkedin_url == "https://linkedin.com/in/janeroe"


# ---- encoding --------------------------------------------------------------------------------

async def test_a_windows_excel_export_decodes(fresh_db):
    """Excel on Windows writes cp1252, which is what a GTM team exports. Letting a
    UnicodeDecodeError escape would reject the commonest file in the category."""
    from nexus.core.db import get_sessionmaker
    from nexus.imports.csv_ingest import import_accounts_csv

    async with get_sessionmaker()() as s:
        ts = await _ts(s)
        content = "company,website\nCafé Numérique,cafe.fr\n".encode("cp1252")
        result = await import_accounts_csv(ts, content=content,
                                           mapping={"company": "name", "website": "domain"})
        assert result["created"] == 1
        assert (await s.scalars(select(Account))).one().name == "Café Numérique"
