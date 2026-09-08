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


# ---- widening the mapping targets ---------------------------------------------------------------
#
# The picker offered eight account fields, and everything else landed in `custom_fields` keyed by
# the RAW CSV HEADER. Three consequences, each reported as "the import lost my data":
#
#   * `tech_stack` could not be mapped, so an imported technographic column was data the product
#     held and the relevance engine could not read.
#   * `crm_id` could not be mapped, so importing a CRM export created a second row for an account
#     the CRM already knew, and the next sync pushed the duplicate back.
#   * A workspace that had DEFINED a custom field could not target it. A file with a
#     "Territory (2026)" column stored the value under that literal string, where the `territory`
#     field it defined never saw it.

async def test_a_technology_column_can_be_mapped_onto_the_scored_field(fresh_db):
    """`tech_stack` is what the relevance engine scores on. Landing it in `custom_fields` was the
    difference between an account scoring on its stack and scoring on nothing."""
    from nexus.core.db import get_sessionmaker
    from nexus.imports.csv_ingest import import_accounts_csv

    async with get_sessionmaker()() as s:
        ts = await _ts(s, "techmap")
        csv = b"company,website,stack\nAcme Corp,acme.com,\"Snowflake, dbt; Looker\"\n"
        await import_accounts_csv(
            ts, content=csv,
            mapping={"company": "name", "website": "domain", "stack": "tech_stack"},
        )
        row = (await s.scalars(select(Account))).one()
        assert row.tech_stack == ["Snowflake", "dbt", "Looker"], row.tech_stack


async def test_an_imported_stack_is_unioned_not_replaced(fresh_db):
    """Same rule as the shared company store, and as the blank-cell rule right beside it: a
    three-column CSV naming the two technologies a rep cares about must not delete the eleven an
    enrichment provider was paid to find."""
    from nexus.core.db import get_sessionmaker
    from nexus.imports.csv_ingest import import_accounts_csv

    async with get_sessionmaker()() as s:
        ts = await _ts(s, "techunion")
        s.add(Account(tenant_id=ts.tenant_id, name="Acme Corp", domain="acme.com",
                      tech_stack=["Salesforce", "Snowflake"]))
        await s.flush()

        await import_accounts_csv(
            ts, content=b"website,stack\nacme.com,\"snowflake, dbt\"\n",
            mapping={"website": "domain", "stack": "tech_stack"},
        )
        row = (await s.scalars(select(Account))).one()
        # Case-insensitive dedupe: "snowflake" must not become a second entry beside "Snowflake".
        assert row.tech_stack == ["Salesforce", "Snowflake", "dbt"], row.tech_stack


async def test_the_crm_id_can_be_mapped_so_a_sync_matches(fresh_db):
    """An ops CSV is usually a CRM export. Without this the imported row carries no CRM identity and
    the next sync creates a duplicate of a record the CRM already has."""
    from nexus.core.db import get_sessionmaker
    from nexus.imports.csv_ingest import import_accounts_csv

    async with get_sessionmaker()() as s:
        ts = await _ts(s, "crmid")
        await import_accounts_csv(
            ts, content=b"company,website,sfid\nAcme,acme.com,0016A00000XyZ\n",
            mapping={"company": "name", "website": "domain", "sfid": "crm_id"},
        )
        row = (await s.scalars(select(Account))).one()
        assert row.crm_id == "0016A00000XyZ"


async def test_a_column_can_be_mapped_onto_a_defined_custom_field(fresh_db):
    """THE bug. Mapping to `custom:territory` writes `custom_fields["territory"]` — the key the
    workspace's own filters read — rather than the CSV's header text."""
    from nexus.core.db import get_sessionmaker
    from nexus.imports.csv_ingest import import_accounts_csv

    async with get_sessionmaker()() as s:
        ts = await _ts(s, "customfield")
        await import_accounts_csv(
            ts, content=b"company,website,Territory (2026)\nAcme,acme.com,EMEA North\n",
            mapping={
                "company": "name", "website": "domain",
                "Territory (2026)": "custom:territory",
            },
        )
        row = (await s.scalars(select(Account))).one()
        assert row.custom_fields.get("territory") == "EMEA North", row.custom_fields
        # And the raw header is NOT also stored: a mapped column is mapped, not mapped and kept.
        assert "Territory (2026)" not in row.custom_fields


async def test_an_unmapped_column_is_still_kept_under_its_own_name(fresh_db):
    """The existing contract, unchanged. An ops CSV always carries columns we have no column for,
    and those columns are usually the reason the list was built."""
    from nexus.core.db import get_sessionmaker
    from nexus.imports.csv_ingest import import_accounts_csv

    async with get_sessionmaker()() as s:
        ts = await _ts(s, "extras")
        await import_accounts_csv(
            ts, content=b"company,website,Tier\nAcme,acme.com,Strategic\n",
            mapping={"company": "name", "website": "domain"},
        )
        row = (await s.scalars(select(Account))).one()
        assert row.custom_fields.get("Tier") == "Strategic"


async def test_a_custom_target_wins_over_a_colliding_raw_header(fresh_db):
    """The operator said this column IS the field. A raw header that happens to share the key must
    not overwrite the thing they chose."""
    from nexus.core.db import get_sessionmaker
    from nexus.imports.csv_ingest import import_accounts_csv

    async with get_sessionmaker()() as s:
        ts = await _ts(s, "collide")
        await import_accounts_csv(
            ts,
            content=b"company,website,territory,Region owner\nAcme,acme.com,EMEA,unmapped\n",
            mapping={"company": "name", "website": "domain", "territory": "custom:territory"},
        )
        row = (await s.scalars(select(Account))).one()
        assert row.custom_fields.get("territory") == "EMEA"
        assert row.custom_fields.get("Region owner") == "unmapped"


async def test_contacts_take_custom_targets_too(fresh_db):
    """Both entities have `custom_fields` and both had the same gap."""
    from nexus.core.db import get_sessionmaker
    from nexus.imports.csv_ingest import import_contacts_csv

    async with get_sessionmaker()() as s:
        ts = await _ts(s, "ccustom")
        await import_contacts_csv(
            ts, content=b"email,name,Persona\nvp@acme.com,Dana Vega,Economic buyer\n",
            mapping={"email": "email", "name": "full_name", "Persona": "custom:persona"},
        )
        row = (await s.scalars(select(Contact))).one()
        assert row.custom_fields.get("persona") == "Economic buyer"


async def test_a_custom_target_the_workspace_has_not_defined_is_refused(fresh_db):
    """`custom:` must not become a way to write arbitrary keys into `custom_fields` — that is the
    namespace the workspace's own filters read. The endpoint resolves targets against the
    DEFINITIONS, not against whatever the client sent."""
    import pytest
    from fastapi import HTTPException

    from nexus.api.routers.imports import ACCOUNT_FIELDS, _parse_mapping

    allowed = set(ACCOUNT_FIELDS) | {"custom:territory"}
    # The defined one passes.
    assert _parse_mapping('{"T": "custom:territory"}', allowed) == {"T": "custom:territory"}
    # An undefined one is named in the error rather than silently dropped.
    with pytest.raises(HTTPException) as exc:
        _parse_mapping('{"T": "custom:anything_i_like"}', allowed)
    assert "custom:anything_i_like" in str(exc.value.detail)


def test_every_mappable_field_has_a_label_in_the_picker():
    """The picker builds itself from the server's field list, so a field added there appears
    immediately — labelled if somebody labelled it, and as its raw key (`postal_code`,
    `crm_source`) if not. A dropdown option reading `annual_revenue` is a form asking for something
    in a vocabulary only this repo speaks.

    There is no frontend test runner here, so this reads the source, the same approach as the nav
    and route-guard tests.
    """
    import pathlib
    import re

    from nexus.api.routers.imports import ACCOUNT_FIELDS, CONTACT_FIELDS

    src = pathlib.Path("frontend/src/components/imports/RecordImportModal.tsx").read_text(
        encoding="utf-8"
    )
    block = re.search(r"const LABELS[^=]*=\s*\{(.*?)\n\};", src, re.S)
    assert block, "LABELS not found — was it renamed?"
    labelled = set(re.findall(r"^\s{2}([a-z_]+):", block.group(1), re.M))

    missing = (set(ACCOUNT_FIELDS) | set(CONTACT_FIELDS)) - labelled
    assert not missing, f"no label for mappable field(s) {sorted(missing)}"


def test_the_picker_offers_the_workspaces_own_fields():
    """THE bug this closed. Without a `custom:` target, every unmapped column landed in
    `custom_fields` keyed by its RAW CSV HEADER — so a workspace that had defined `territory` and
    uploaded a "Territory (2026)" column stored the value under that literal string, invisible to
    the field it defined and to every filter reading it."""
    import pathlib

    from nexus.imports.csv_ingest import CUSTOM_PREFIX

    assert CUSTOM_PREFIX == "custom:"
    src = pathlib.Path("frontend/src/components/imports/RecordImportModal.tsx").read_text(
        encoding="utf-8"
    )
    assert "customFields" in src and "c.target" in src, (
        "the picker no longer offers the workspace's own field definitions"
    )
    assert "account_custom_fields" in pathlib.Path("frontend/src/lib/types.ts").read_text(
        encoding="utf-8"
    )
