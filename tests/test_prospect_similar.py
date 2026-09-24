"""Find similar companies: LinkedIn's own "similar pages" first, then web search.

Decided with the product owner 2026-09-24. Measured facts this pins:

* LinkedIn lists ~12 similar pages per company and **none carries a website** (0 of 72 observed),
  so each must be looked up before it can be offered, and a page whose lookup has no website of its
  own is dropped: the domain is what an account is.
* The seed's page must be PROVEN: ``linkedin.com/company/vanta`` is a chauffeur firm. A page found
  by name is used only when its website is the account's own domain.
* Pages already in the shared store are resolved from it for free; only the rest are bought, in
  one bulk run, and stored for everyone.
"""
from __future__ import annotations

import pytest

from nexus.integrations.apify import ApifyNotConfigured
from nexus.lookalike.service import LookalikeService
from nexus.models import Account
from nexus.prospecting import similar
from nexus.prospecting.companies import store_companies
from nexus.prospecting.linkedin import parse_company
from tests.conftest import make_tenant, tenant_session


def _page(slug: str, website: str | None, *, name: str | None = None, similar_to=(),
          asked: str | None = None, industry: int = 4) -> dict:
    return {
        "id": str(abs(hash(slug)) % 10**6), "universalName": slug,
        "linkedinUrl": f"https://www.linkedin.com/company/{slug}/",
        "name": name or slug.replace("-", " ").title(), "website": website,
        "employeeCount": 150, "employeeCountRange": {"start": 51, "end": 200},
        "description": f"{slug} makes compliance software",
        "locations": [{"headquarter": True, "parsed": {"countryCode": "US"}}],
        "industries": [{"id": str(industry), "name": "Software Development"}],
        "similarOrganizations": [
            {"linkedinUrl": f"https://www.linkedin.com/company/{s}/", "name": s.title(),
             "website": None} for s in similar_to
        ],
        "originalQuery": {"search": asked or f"https://www.linkedin.com/company/{slug}/"},
    }


class DetailsClient:
    """The company-details actor: pages by URL or by name, recording every run."""

    def __init__(self, pages: dict[str, dict], *, by_name: dict[str, dict] | None = None,
                 error: Exception | None = None):
        self.pages = pages
        self.by_name = by_name or {}
        self.error = error
        self.runs: list[dict] = []

    async def run_actor(self, actor, run_input, *, timeout=None):
        assert actor == "linkedin_company"
        self.runs.append(run_input)
        if self.error:
            raise self.error
        if "searches" in run_input:
            return [self.by_name[n] for n in run_input["searches"] if n in self.by_name]
        return [self.pages[u] for u in run_input["companies"] if u in self.pages]


SEED = _page("vanta-security", "https://vanta.com", name="Vanta",
             similar_to=("drata", "secureframe", "no-site-co", "vanta-security"))
DRATA = _page("drata", "https://drata.com")
SECUREFRAME = _page("secureframe", "https://secureframe.com")
NO_SITE = _page("no-site-co", None)


def _url(slug):
    return f"https://www.linkedin.com/company/{slug}/"


async def _seed_account(tid, *, domain="vanta.com", name="Vanta"):
    async with tenant_session(tid) as ts:
        acc = Account(tenant_id=tid, name=name, domain=domain, industry="Software Development")
        ts.add(acc)
        await ts.flush()
        return acc.id


async def _similar(tid, account_id, client, limit=10):
    async with tenant_session(tid) as ts:
        account = await ts.get(Account, account_id)
        return await similar.similar_companies(ts, account, limit=limit, client=client)


async def test_stored_peers_cost_nothing_and_a_page_without_a_website_is_bought_once():
    tid = await make_tenant("sim1")
    await store_companies([parse_company(p) for p in (SEED, DRATA, SECUREFRAME)])
    aid = await _seed_account(tid)
    client = DetailsClient({_url("no-site-co"): NO_SITE})

    first = await _similar(tid, aid, client)
    second = await _similar(tid, aid, client)

    assert [c.domain for c in first.companies] == ["drata.com", "secureframe.com"]
    assert client.runs == [{"companies": [_url("no-site-co")]}], (
        "only the page nobody had looked up was bought, and only once")
    assert second.discarded == {"no_website": 1}, "remembered, and still reported"


async def test_unknown_peers_are_bought_in_one_run_stored_and_gated():
    tid = await make_tenant("sim2")
    await store_companies([parse_company(SEED)])
    aid = await _seed_account(tid)
    client = DetailsClient({_url("drata"): DRATA, _url("secureframe"): SECUREFRAME,
                            _url("no-site-co"): NO_SITE})

    result = await _similar(tid, aid, client)

    assert len(client.runs) == 1
    assert sorted(client.runs[0]["companies"]) == sorted(
        [_url("drata"), _url("secureframe"), _url("no-site-co")]), "the seed itself is not re-asked"
    assert [c.domain for c in result.companies] == ["drata.com", "secureframe.com"]
    assert result.discarded == {"no_website": 1}

    again = await _similar(tid, aid, DetailsClient({}))
    assert [c.domain for c in again.companies] == ["drata.com", "secureframe.com"], "stored"


async def test_a_seed_page_found_by_name_is_used_only_when_it_is_the_accounts_own():
    tid = await make_tenant("sim3")
    aid = await _seed_account(tid)
    chauffeurs = _page("vanta", "https://www.vantaexec.co.uk/", name="VANTA - Chauffeurs",
                       similar_to=("limo-co",), asked="Vanta")
    client = DetailsClient({}, by_name={"Vanta": chauffeurs})

    result = await _similar(tid, aid, client)

    assert result.companies == []
    assert result.notes["linkedin"] == "no_verified_page"
    assert len(client.runs) == 1, "no similar list was bought for somebody else's page"


async def test_a_seed_page_found_by_name_that_proves_itself_is_used_and_linked():
    tid = await make_tenant("sim4")
    aid = await _seed_account(tid)
    client = DetailsClient({_url("drata"): DRATA, _url("secureframe"): SECUREFRAME},
                           by_name={"Vanta": {**SEED, "originalQuery": {"search": "Vanta"}}})

    result = await _similar(tid, aid, client)

    assert [c.domain for c in result.companies] == ["drata.com", "secureframe.com"]
    async with tenant_session(tid) as ts:
        account = await ts.get(Account, aid)
    assert account.company_id, "the account is linked to the shared row its page proved"


async def test_linkedin_unavailable_is_a_note_not_an_error():
    tid = await make_tenant("sim5")
    aid = await _seed_account(tid)

    result = await _similar(tid, aid, DetailsClient({}, error=ApifyNotConfigured("x")))

    assert result.companies == [] and result.notes["linkedin"] == "not_configured"


async def test_an_account_without_a_domain_is_not_looked_up():
    tid = await make_tenant("sim6")
    aid = await _seed_account(tid, domain=None)
    client = DetailsClient({})

    result = await _similar(tid, aid, client)

    assert result.companies == [] and client.runs == []


# ---- through the lookalike service ------------------------------------------------------------

async def test_the_lookalike_service_offers_linkedin_peers_before_web_search(monkeypatch):
    tid = await make_tenant("sim7")
    await store_companies([parse_company(p) for p in (SEED, DRATA, SECUREFRAME)])
    aid = await _seed_account(tid)
    async with tenant_session(tid) as ts:
        ts.add(Account(tenant_id=tid, name="Drata", domain="drata.com"))

    def no_web():
        raise AssertionError("web search was asked although LinkedIn answered")

    monkeypatch.setattr("nexus.lookalike.service.exa_search", no_web)
    monkeypatch.setattr(similar, "_client_for", lambda client: DetailsClient({}))

    async with tenant_session(tid) as ts:
        account = await ts.get(Account, aid)
        found = await LookalikeService().find(ts, account, limit=10)

    assert {lk.domain for lk in found} == {"drata.com", "secureframe.com"}
    assert {lk.source for lk in found} == {"linkedin"}
    tracked = {lk.domain: lk.already_tracked for lk in found}
    assert tracked == {"drata.com": True, "secureframe.com": False}


async def test_the_lookalike_service_falls_back_to_web_search_when_linkedin_has_nothing(
    monkeypatch,
):
    tid = await make_tenant("sim8")
    aid = await _seed_account(tid)
    asked: list[str] = []

    class Web:
        async def search_companies(self, query, *, limit, exclude_domains=None):
            asked.append(query)
            return []

    monkeypatch.setattr("nexus.lookalike.service.exa_search", lambda: Web())
    monkeypatch.setattr(similar, "_client_for",
                        lambda client: DetailsClient({}, error=ApifyNotConfigured("x")))

    async with tenant_session(tid) as ts:
        account = await ts.get(Account, aid)
        found = await LookalikeService().find(ts, account, limit=10)

    assert found == [] and len(asked) == 1


@pytest.mark.parametrize("limit", [1])
async def test_the_limit_is_respected(limit):
    tid = await make_tenant("sim9")
    await store_companies([parse_company(p) for p in (SEED, DRATA, SECUREFRAME)])
    aid = await _seed_account(tid)

    result = await _similar(tid, aid, DetailsClient({}), limit=limit)

    assert len(result.companies) == limit
