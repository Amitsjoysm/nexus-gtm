"""A similar person arrives with their employer, and the add form arrives with its domain.

Reported 2026-10-02: "when we find similar contacts the company name and domain still have to be
typed in." Reproduced on the local deploy for David Corr: 4 of 10 results had no company, and two
more had a territory or a team where the company should be ("UK&I", "Security & Cloud"). The
employer was in the data all along: every LinkedIn snippet Exa returns carries an `## Experience`
section whose current role is marked `(Current)` and links the company's own LinkedIn page. The
parser read only the headline line.

So the current Experience entry now names the company and its page, and the add form resolves that
page to the company's own website (the shared store first, then one company-details run, stored for
every workspace), falling back to the old name search. It still only PREFILLS a form the rep
confirms: a name is not an identity, and neither is a guess.
"""
from __future__ import annotations

import pytest

from nexus.models.account import Account, Contact
from tests.test_contact_lookalike_sourcing import _Registry, _hit, _source
from tests.test_prospect_similar import DetailsClient, _page

# The three shapes seen live on 2026-10-02 (names and slugs changed; structure exact).
INLINE = (
    "# Finn Example\n\nVP Enterprise Sales, UK&I\n\nUnited Kingdom (GB)\n\n## About\n\nx\n\n"
    "## Experience\n\n### VP Enterprise Sales, UK&I - "
    "[Remotely](https://www.linkedin.com/company/remotely-co) (Current)\n\n"
    "Jan 2026 - Present (8 months)\n\n### Regional Director - "
    "[Old Co](https://www.linkedin.com/company/old-co)\n\n2019 - 2025"
)
GROUPED = (
    "# Simon Example\n\nVice President UK Fieldops Sales\n\nReading, England (GB)\n\n"
    "## Experience\n\n### [Fieldops Software](https://www.linkedin.com/company/fieldops-software)\n\n"
    "#### Vice President UK Sales (Current)\n\nApr 2022 - Present\n\n#### Enterprise Sales Director"
)
TEAM_HEADLINE = (
    "# Kieran Example\n\nVP Sales | EMEA GTM Leadership | SaaS, Security & Cloud\n\nLondon (GB)\n\n"
    "## Experience\n\n### Vice President Sales - UK I - "
    "[Secureco](https://www.linkedin.com/company/secureco) (Current)\n\nMar 2024 - Present"
)
NO_EXPERIENCE = "# Dana Example\n\nVP Revenue Operations at Ramp\n\nNew York, United States (US)"


# ---- reading the current employer ----------------------------------------------------------------

@pytest.mark.parametrize(("snippet", "company", "page"), [
    (INLINE, "Remotely", "https://www.linkedin.com/company/remotely-co/"),
    (GROUPED, "Fieldops Software", "https://www.linkedin.com/company/fieldops-software/"),
    (TEAM_HEADLINE, "Secureco", "https://www.linkedin.com/company/secureco/"),
    (NO_EXPERIENCE, "", ""),
])
def test_the_current_employer_is_read_from_the_experience_section(snippet, company, page):
    from nexus.lookalike.contacts import _current_employer

    assert _current_employer(snippet) == (company, page)


def test_a_past_role_is_never_the_employer():
    from nexus.lookalike.contacts import _current_employer

    past_only = ("# P\n\nVP Sales\n\n## Experience\n\n### VP Sales - "
                 "[Old Co](https://www.linkedin.com/company/old-co)\n\n2019 - 2025")
    assert _current_employer(past_only) == ("", "")


async def test_a_result_carries_the_employer_and_its_page(monkeypatch):
    reg = _Registry(results=[
        _hit("Finn Example", "https://www.linkedin.com/in/finn-example", INLINE),
        _hit("Kieran Example", "https://www.linkedin.com/in/kieran-example", TEAM_HEADLINE),
        _hit("Dana Example", "https://www.linkedin.com/in/dana-example", NO_EXPERIENCE),
    ])
    seed = Contact(id="c1", account_id="a1", full_name="Alex Kim", title="VP Sales",
                   linkedin_url="https://www.linkedin.com/in/alex-kim")
    out = await _source(monkeypatch, seed, reg,
                        account=Account(id="a1", name="Acme", domain="acme.com"))
    by_name = {p.full_name: p for p in out}
    # "UK&I" (a territory) and "Security & Cloud" (a team) are gone; the real employers are in.
    assert by_name["Finn Example"].company == "Remotely"
    assert by_name["Finn Example"].company_linkedin_url == \
        "https://www.linkedin.com/company/remotely-co/"
    assert by_name["Kieran Example"].company == "Secureco"
    # A headline employer still works when there is no Experience section.
    assert by_name["Dana Example"].company == "Ramp"
    assert by_name["Dana Example"].company_linkedin_url == ""
    assert "company_linkedin_url" in by_name["Finn Example"].as_dict()


def test_the_api_declares_the_page_or_pydantic_drops_it():
    """The response model is built with `ContactLookalikeOut(**as_dict())`: an undeclared field is
    silently dropped (the same trap that hid `dated` on the call brief)."""
    from nexus.api.schemas import ContactLookalikeOut

    assert "company_linkedin_url" in ContactLookalikeOut.model_fields


# ---- resolving the domain from the company's own page --------------------------------------------

PAGE = "https://www.linkedin.com/company/remotely-co/"


async def test_a_page_in_the_shared_store_resolves_without_a_lookup():
    from nexus.enrichment.company_domain import resolve_from_linkedin_page
    from nexus.prospecting.companies import store_companies
    from nexus.prospecting.linkedin import parse_company

    await store_companies([parse_company(_page("remotely-co", "https://remotely.io"))])
    client = DetailsClient({})
    found = await resolve_from_linkedin_page(PAGE, client=client)
    assert found is not None and found.domain == "remotely.io"
    assert client.runs == [], "a page the store already knows must not be bought again"


async def test_an_unknown_page_is_looked_up_once_and_stored_for_everyone():
    from sqlalchemy import select

    from nexus.core.db import get_platform_sessionmaker
    from nexus.enrichment.company_domain import resolve_from_linkedin_page
    from nexus.models.company import Company

    client = DetailsClient({PAGE: _page("remotely-co", "https://remotely.io")})
    found = await resolve_from_linkedin_page(PAGE, client=client)
    assert found is not None and found.domain == "remotely.io"
    assert len(client.runs) == 1
    async with get_platform_sessionmaker()() as s:
        row = (await s.scalars(select(Company).where(Company.domain == "remotely.io"))).first()
    assert row is not None and row.linkedin_url == PAGE


async def test_a_page_with_no_website_gives_no_domain_and_is_not_bought_twice():
    from nexus.enrichment.company_domain import resolve_from_linkedin_page

    client = DetailsClient({PAGE: _page("remotely-co", None)})
    assert await resolve_from_linkedin_page(PAGE, client=client) is None
    assert await resolve_from_linkedin_page(PAGE, client=client) is None
    assert len(client.runs) == 1


async def test_a_failed_lookup_never_raises():
    from nexus.enrichment.company_domain import resolve_from_linkedin_page

    assert await resolve_from_linkedin_page(PAGE, client=DetailsClient({}, error=RuntimeError("x"))) \
        is None
    assert await resolve_from_linkedin_page("https://example.com/not-linkedin", client=None) is None


async def test_the_endpoint_prefers_the_page_and_falls_back_to_the_name(client, monkeypatch):
    from nexus.enrichment import company_domain as mod
    from tests.conftest import auth, signup

    token = await signup(client, slug="empdomain", email="o@empdomain.com", company="Emp")
    asked: list[str] = []

    async def from_page(url, *, client=None):
        asked.append(url)
        return mod.CompanyDomain(domain="remotely.io", url=url, title="Remotely")

    async def from_name(name, *, search=None):
        return mod.CompanyDomain(domain="by-name.com", url="https://by-name.com", title=name)

    monkeypatch.setattr(mod, "resolve_from_linkedin_page", from_page)
    monkeypatch.setattr(mod, "resolve_company_domain", from_name)
    r = await client.get("/api/accounts/company-domain", headers=auth(token),
                         params={"name": "Remotely", "linkedin_url": PAGE})
    assert r.status_code == 200, r.text
    assert r.json()["domain"] == "remotely.io" and r.json()["source"] == "linkedin"
    assert asked == [PAGE]

    async def nothing(url, *, client=None):
        return None

    monkeypatch.setattr(mod, "resolve_from_linkedin_page", nothing)
    r = await client.get("/api/accounts/company-domain", headers=auth(token),
                         params={"name": "Remotely", "linkedin_url": PAGE})
    assert r.json()["domain"] == "by-name.com" and r.json()["source"] == "search"
    # Without a page, the name search is all there is, exactly as before.
    r = await client.get("/api/accounts/company-domain", headers=auth(token),
                         params={"name": "Remotely"})
    assert r.json()["domain"] == "by-name.com"


def test_the_add_form_passes_the_page():
    from pathlib import Path

    src = Path("frontend/src/components/AddSimilarPerson.tsx").read_text(encoding="utf-8")
    assert "person.company_linkedin_url" in src
