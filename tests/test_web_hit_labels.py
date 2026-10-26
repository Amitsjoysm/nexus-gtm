"""A web search hit carries only what the hit itself says (SDR feedback, 2026-10-26).

Reported as "we chose Oil & Gas; it pulled Manufacturing companies and labelled them Oil & Gas",
but no industry was spared. `SearchBackedCompanySearchProvider` searched "<every ICP industry>
companies in <every country>" and then wrote the ICP's FIRST industry and FIRST country onto every
hit. Measured on the live Exa API with an "Oil & Gas, Manufacturing" ICP: 24 hits, mostly oilfield
equipment makers, every one labelled "Oil & Gas". Locally, 111 of 111 web-found accounts in one
workspace read exactly its first industry ("Manufacturing", for software vendors such as Zuora) and
"United States", one of them a company in the UAE; two other workspaces had the same with
"Software & SaaS".

The label is what the search was asked, not anything the company or a source said, and it was
permanent: enrichment fills blanks only, the fit score then credited the ICP for matching itself,
the stamped country passed the geography gate, and the shared store handed it to other workspaces.
"""
from __future__ import annotations

from collections import Counter

import pytest

from nexus.core.config import get_settings
from nexus.integrations.apify import ApifyNotConfigured
from nexus.integrations.company_search import SearchBackedCompanySearchProvider
from nexus.integrations.search import SearchHit, SearchProvider
from nexus.prospecting import companies as chain
from tests.conftest import make_tenant, seed_relevance_profile, tenant_session

ICP = {"industries": ["Oil & Gas", "Manufacturing"], "linkedin_industry_ids": [57, 25],
       "countries": ["United States"]}


class _Search(SearchProvider):
    name = "fake"

    def __init__(self, hits):
        self.hits = hits
        self.queries: list[str] = []

    async def search(self, query, *, limit=5):
        self.queries.append(query)
        return list(self.hits)[:limit]


def _hits():
    return [
        SearchHit(title="Lincoln Manufacturing, Inc.", url="https://lincolnmanufacturing.com/",
                  snippet="Lincoln Manufacturing builds pressure control equipment."),
        SearchHit(title="United Technical Services", url="https://uts.ae/about",
                  snippet="Engineering services in Abu Dhabi."),
    ]


class _NoLinkedIn:
    async def run_actor(self, *args, **kwargs):
        raise ApifyNotConfigured("not in this test")


@pytest.mark.parametrize("industries, countries", [
    (["Oil & Gas", "Manufacturing"], ["United States"]),
    (["Software & SaaS", "Fintech"], ["United Kingdom", "Germany"]),
    (["Healthcare"], ["India"]),
    (["Logistics", "Retail"], ["Canada"]),
    (["Manufacturing", "SaaS / Software", "Energy"], ["Australia"]),
])
async def test_a_web_hit_carries_no_industry_or_country_it_did_not_state(industries, countries):
    """Every industry, not one: locally the stamp filed software vendors as "Manufacturing" and as
    "Software & SaaS", whichever the workspace happened to list first."""
    search = _Search(_hits())
    provider = SearchBackedCompanySearchProvider(search)

    found = await provider.search({"industries": industries, "geo": countries})

    assert [c.domain for c in found] == ["lincolnmanufacturing.com", "uts.ae"]
    assert [(c.industry, c.country) for c in found] == [(None, None), (None, None)]
    assert all("industry" not in c.provenance and "country" not in c.provenance for c in found)
    # Every ICP industry is still asked for; none is written onto what comes back.
    assert all(i in search.queries[0] for i in industries)


async def test_a_web_find_is_never_saved_under_the_icps_own_industry(monkeypatch):
    from nexus.prospecting.deliver import deliver
    from nexus.relevance.engine import get_profile

    monkeypatch.setattr(get_settings(), "icp_discovery_enrich_candidates", False)
    tid = await make_tenant("webhit1")
    async with tenant_session(tid) as ts:
        await seed_relevance_profile(ts, icp=ICP)
    provider = SearchBackedCompanySearchProvider(_Search(_hits()))
    async with tenant_session(tid) as ts:
        result = await chain.find_icp_companies(ts, ICP, 2, client=_NoLinkedIn(),
                                                web_search=provider.search)
        assert {c.source for c in result.candidates} == {"web"}
        assert [(c.industry, c.country) for c in result.candidates] == [(None, None), (None, None)]

        ids, _ = await deliver(ts, await get_profile(ts), result.candidates, limit=2,
                               source="auto_discovery", min_fit=0, discarded=Counter())
        from nexus.models.account import Account

        accounts = [await ts.get(Account, i) for i in ids]
    assert [(a.industry, a.country) for a in accounts] == [(None, None), (None, None)]


async def test_a_foreign_headquarters_is_no_longer_hidden_by_a_stamped_country(monkeypatch):
    """Enrichment fills blanks only, so a stamped "United States" used to survive it, and the
    geography gate compared the ICP with itself. Now the enriched country is what the gate reads."""
    from nexus.discovery import auto
    from nexus.prospecting.deliver import deliver
    from nexus.relevance.engine import get_profile

    async def enrich(ts, accounts, *, concurrency):
        for account in accounts:
            if account.domain == "uts.ae" and not account.country:
                account.country = "United Arab Emirates"

    monkeypatch.setattr(auto, "_enrich_candidates", enrich)
    monkeypatch.setattr(get_settings(), "icp_discovery_enrich_candidates", True)
    tid = await make_tenant("webhit2")
    async with tenant_session(tid) as ts:
        await seed_relevance_profile(ts, icp=ICP)
    provider = SearchBackedCompanySearchProvider(_Search(_hits()))
    discarded = Counter()
    async with tenant_session(tid) as ts:
        result = await chain.find_icp_companies(ts, ICP, 2, client=_NoLinkedIn(),
                                                web_search=provider.search)
        ids, _ = await deliver(ts, await get_profile(ts), result.candidates, limit=2,
                               source="auto_discovery", min_fit=0, discarded=discarded)
        from nexus.models.account import Account

        domains = [(await ts.get(Account, i)).domain for i in ids]
    assert domains == ["lincolnmanufacturing.com"]
    assert discarded["outside_icp"] == 1


@pytest.mark.parametrize("url", [
    "https://exa.ai/company/waystar", "https://exa.ai/", "https://search.brave.com/search?q=acme",
    "https://serper.dev/", "https://www.firecrawl.dev/app",
])
async def test_a_search_vendors_own_page_is_never_a_company(url):
    """Measured: three accounts in three workspaces were filed under `exa.ai` (Waystar, Intuit
    Mailchimp, Eyelit Technologies), so domain dedupe would merge them and the email finder would
    guess `@exa.ai` addresses for their staff."""
    from nexus.prospecting.linkedin import company_domain

    provider = SearchBackedCompanySearchProvider(_Search([
        SearchHit(title="Waystar", url=url, snippet="Waystar is a Software Development company."),
        SearchHit(title="Waystar", url="https://www.waystar.com/", snippet="Healthcare payments."),
    ]))

    found = await provider.search({"industries": ["Healthcare"]})

    assert [c.domain for c in found] == ["waystar.com"]
    assert company_domain(url) == ""
