"""Where every account and contact value came from (SDR feedback, 2026-10-26).

Asked: "show the source of every account and contact, so SDRs can validate it". Each row carried
one column saying how the ROW arrived (`accounts.source`, `contacts.enrichment_source`), nothing
said who wrote the industry, the country or the email, and the email finder OVERWROTE the contact's
column: measured on a probe of 20 accounts, every one of 21 people found by web search and then
given an email read `pattern_verified`, so where the person came from was gone.

`field_sources` (migration 0073) records, per field, the source, the evidence and the time.
"""
from __future__ import annotations

from collections import Counter

import pytest

from nexus.integrations.provenance import display_sources, record
from nexus.models.account import Account, Contact
from nexus.prospecting.companies import Candidate
from tests.conftest import make_tenant, seed_relevance_profile, tenant_session

ICP = {"industries": ["Software Development"], "linkedin_industry_ids": [4],
       "countries": ["United Kingdom"]}


async def _deliver(tid, cand):
    from nexus.prospecting.deliver import deliver
    from nexus.relevance.engine import get_profile

    async with tenant_session(tid) as ts:
        ids, _ = await deliver(ts, await get_profile(ts), [cand], limit=1,
                               source="auto_discovery", min_fit=0, discarded=Counter())
        return await ts.get(Account, ids[0])


async def _workspace(slug):
    tid = await make_tenant(slug)
    async with tenant_session(tid) as ts:
        await seed_relevance_profile(ts, icp=ICP)
    return tid


# ---- accounts ----------------------------------------------------------------------------------

async def test_a_linkedin_delivered_account_names_its_page_for_every_field(monkeypatch):
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "icp_discovery_enrich_candidates", False)
    tid = await _workspace("prov1")
    page = "https://www.linkedin.com/company/acme-soft/"
    account = await _deliver(tid, Candidate(
        domain="acme-soft.io", name="Acme Soft", source="linkedin", industry="Software Development",
        country="United Kingdom", employee_count=120, linkedin_url=page))

    sources = account.field_sources
    for field in ("name", "domain", "industry", "country", "employee_count"):
        assert sources[field]["source"] == "linkedin" and sources[field]["ref"] == page, field


async def test_a_web_find_names_the_hit_and_claims_nothing_else(monkeypatch):
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "icp_discovery_enrich_candidates", False)
    tid = await _workspace("prov2")
    account = await _deliver(tid, Candidate(domain="beta.io", name="Beta", source="web",
                                            url="https://beta.io/about"))

    assert account.field_sources["domain"] == {
        **account.field_sources["domain"], "source": "web_search", "ref": "https://beta.io/about"}
    assert set(account.field_sources) == {"name", "domain"}


def test_enrichment_records_the_route_and_the_pages_it_read_for_what_it_filled():
    from nexus.enrichment.account import SearchBackedAccountEnricher

    account = Account(tenant_id="t", name="Gamma", domain="gamma.io", country="Germany")
    enricher = SearchBackedAccountEnricher(search=None, llm=None)

    filled = enricher.apply(account, {
        "industry": "Logistics", "country": "France", "employee_count": 80,
        "_refs": ["https://gamma.io/about", "https://news.example/gamma"],
    })

    assert set(filled) == {"industry", "employee_count"}, "a held country is never overwritten"
    entry = account.field_sources["industry"]
    assert entry["source"] == "enrichment_web"
    assert entry["refs"] == ["https://gamma.io/about", "https://news.example/gamma"]
    assert "country" not in account.field_sources


def test_a_source_database_fill_says_which_database():
    from nexus.enrichment.account import SearchBackedAccountEnricher

    account = Account(tenant_id="t", name="Delta", domain="delta.io")
    SearchBackedAccountEnricher(search=None, llm=None).apply(
        account, {"industry": "Retail"}, source="source_database", detail="acme_db")

    assert account.field_sources["industry"]["source"] == "source_database"
    assert account.field_sources["industry"]["detail"] == "acme_db"


# ---- contacts ----------------------------------------------------------------------------------

@pytest.fixture
def strict(monkeypatch):
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "email_verify_provider", "reacher,dns")


@pytest.mark.parametrize("arrived", ["sourcing:linkedin", "sourcing:search", "crm:hubspot",
                                     "csv_import"])
async def test_the_email_finder_no_longer_overwrites_where_the_person_came_from(strict, arrived):
    from nexus.enrichment.providers import VerifyingPatternEmailProvider
    from nexus.enrichment.waterfall import WaterfallEnricher
    from nexus.verification import STATUS_VALID, EmailVerification

    async def verify(email):
        status = STATUS_VALID if email == "jane.doe@acme.com" else "invalid"
        return EmailVerification(email=email, status=status, confidence=0.95, source="reacher")

    tid = await make_tenant("prov-" + arrived.replace(":", "-"))
    async with tenant_session(tid) as ts:
        acc = Account(tenant_id=tid, name="Acme", domain="acme.com")
        ts.add(acc)
        await ts.flush()
        contact = Contact(tenant_id=tid, account_id=acc.id, full_name="Jane Doe",
                          enrichment_source=arrived)
        ts.add(contact)
        await ts.flush()
        await WaterfallEnricher(providers=[VerifyingPatternEmailProvider(verify=verify)],
                                verify=verify).enrich_contact(ts, contact, acc)

    assert contact.email == "jane.doe@acme.com"
    assert contact.enrichment_source == arrived
    email = contact.field_sources["email"]
    assert email["source"] == "pattern_verified"
    assert email["detail"] == "valid, checked at the mailbox"


async def test_people_found_on_linkedin_name_their_profile():
    from nexus.contacts.sourcing import _persist_linkedin_people
    from nexus.prospecting.linkedin import LinkedInPerson

    profile = "https://www.linkedin.com/in/jane-doe-1/"
    tid = await make_tenant("prov3")
    async with tenant_session(tid) as ts:
        acc = Account(tenant_id=tid, name="Acme", domain="acme.com")
        ts.add(acc)
        await ts.flush()
        people = [LinkedInPerson(linkedin_url=profile, first_name="Jane", last_name="Doe",
                                 title="VP Sales", company_name="Acme"),
                  LinkedInPerson(linkedin_url="https://www.linkedin.com/in/raj-2/",
                                 first_name="Raj", last_name="K", title="CRO", company_name="Acme")]
        created = await _persist_linkedin_people(ts, acc, people,
                                                 pooled={"linkedin.com/in/raj-2"})
    jane, raj = created
    assert jane.field_sources["person"] == {**jane.field_sources["person"],
                                            "source": "linkedin", "ref": profile}
    assert jane.field_sources["title"]["source"] == "linkedin"
    assert raj.field_sources["person"]["source"] == "pool"


async def test_a_prospector_record_says_infojoy_and_that_its_email_is_unverified():
    from nexus.prospector.records import VendorContact
    from nexus.prospector.writers import upsert_account, upsert_contact

    tid = await make_tenant("prov4")
    async with tenant_session(tid) as ts:
        account, _, _ = await upsert_account(ts, name="Epsilon", url="epsilon.io",
                                             country="Canada", industry="Retail")
        contact, _ = await upsert_contact(ts, account, VendorContact(
            first_name="Ana", last_name="Silva", title="COO", department="", seniority="",
            email="ana@epsilon.io", direct_phone="", company_name="Epsilon",
            company_url="epsilon.io", country="Canada"), include_phone=False)
    assert account.field_sources["industry"]["source"] == "infojoy"
    assert contact.field_sources["email"]["source"] == "infojoy"
    assert "unverified" in contact.field_sources["email"]["detail"]


# ---- what the screens read -----------------------------------------------------------------------

def test_a_value_from_before_sources_were_kept_says_so_rather_than_guessing():
    account = Account(tenant_id="t", name="Old Co", domain="old.io", industry="Retail",
                      source="csv_import")
    record(account, "domain", "manual")

    shown = display_sources(account)

    assert shown["domain"]["source"] == "manual"
    assert shown["industry"]["source"] == "not_recorded"
    assert shown["industry"]["arrived_via"] == "csv_import"


async def test_the_api_returns_each_fields_source(client):
    from tests.conftest import auth, signup

    token = await signup(client, slug="prov5", email="o@prov5.com", company="Prov5")
    r = await client.post("/api/accounts", headers=auth(token),
                          json={"name": "Zeta", "domain": "zeta.io", "industry": "Retail"})
    assert r.status_code in (200, 201), r.text
    account = r.json()
    assert account["field_sources"]["industry"]["source"] == "manual"

    c = await client.post(f"/api/accounts/{account['id']}/contacts", headers=auth(token),
                          json={"full_name": "Ana Silva", "title": "COO"})
    assert c.status_code in (200, 201), c.text
    assert c.json()["field_sources"]["person"]["source"] == "manual"
