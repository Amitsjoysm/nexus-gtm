"""Find contacts: people at the account's own LinkedIn page with the ICP's titles, then web search.

Decided with the product owner 2026-09-24: LinkedIn first, web search as the fallback; only the
titles the ICP names, never "all employees"; the email comes from our own verified finder.

Measured on the one real run: asked for sales leaders at ``linkedin.com/company/vanta`` the actor
returned a Fleet Coordinator at Flex E Lease. That page is a chauffeur firm, so the page must be
the account's own (proven by its website), and every row must still name this company and fit a
title before it becomes a contact. Duplicates are refused by LinkedIn profile and by name.
"""
from __future__ import annotations

import pytest

from nexus.contacts.sourcing import source_account_contacts
from nexus.integrations.apify import ApifyNotConfigured
from nexus.integrations.contact_search import ContactCandidate
from nexus.models import Account, Contact
from nexus.models.relevance import RelevanceProfile
from nexus.prospecting.companies import store_companies
from nexus.prospecting.linkedin import parse_company
from tests.conftest import make_tenant, tenant_session

VANTA_PAGE = {
    "id": "35462987", "universalName": "vanta-security",
    "linkedinUrl": "https://www.linkedin.com/company/vanta-security/", "name": "Vanta",
    "website": "https://vanta.com", "employeeCountRange": {"start": 1001, "end": 5000},
    "locations": [{"headquarter": True, "parsed": {"countryCode": "US"}}],
    "industries": [{"id": "4", "name": "Software Development"}],
}
CHAUFFEURS = {**VANTA_PAGE, "id": "7098076", "universalName": "vanta",
              "linkedinUrl": "https://www.linkedin.com/company/vanta/",
              "name": "VANTA - Chauffeurs", "website": "https://www.vantaexec.co.uk/",
              "originalQuery": {"search": "Vanta"}}


def _person(pid: str, first: str, last: str, title: str, company: str = "Vanta") -> dict:
    return {"id": pid, "linkedinUrl": f"https://www.linkedin.com/in/{pid}",
            "firstName": first, "lastName": last,
            "currentPositions": [{"companyName": company, "title": title, "current": True}],
            "location": {"linkedinText": "San Francisco, California, United States"}}


class Actors:
    def __init__(self, *, people=(), by_name=None, error=None):
        self.people = list(people)
        self.by_name = by_name or {}
        self.error = error
        self.runs: list[tuple[str, dict]] = []

    async def run_actor(self, actor, run_input, *, timeout=None):
        self.runs.append((actor, run_input))
        if self.error:
            raise self.error
        if actor == "linkedin_company":
            return [self.by_name[n] for n in run_input.get("searches", []) if n in self.by_name]
        assert actor == "linkedin_company_employees"
        return list(self.people)


class Web:
    def __init__(self, found=()):
        self.found = list(found)
        self.calls = 0

    async def contact_search(self, account, icp, *, limit):
        self.calls += 1
        return list(self.found)


@pytest.fixture
def web(monkeypatch):
    """Web search, faked on the REAL registry: the email finder reads its verifier from there."""
    from nexus.integrations.registry import get_registry

    fake = Web([ContactCandidate(full_name="Web Person", title="VP Sales", source="exa",
                                 linkedin_url="https://www.linkedin.com/in/web-person")])
    monkeypatch.setattr(get_registry(), "contact_search", fake.contact_search)
    return fake


async def _account(tid, *, titles=("VP of Sales", "Head of Sales"), stored_page=True):
    if stored_page:
        await store_companies([parse_company(VANTA_PAGE)])
    async with tenant_session(tid) as ts:
        ts.add(RelevanceProfile(tenant_id=tid, icp={"buyer_titles": list(titles)} if titles else
                                {"industries": ["SaaS"]}))
        account = Account(tenant_id=tid, name="Vanta", domain="vanta.com")
        ts.add(account)
        await ts.flush()
        return account.id


async def _source(tid, aid, actors, limit=5):
    async with tenant_session(tid) as ts:
        account = await ts.get(Account, aid)
        return [(c.full_name, c.title, c.linkedin_url)
                for c in await source_account_contacts(ts, account, limit=limit,
                                                       linkedin_client=actors)]


async def test_only_proven_buyers_at_this_company_become_contacts(web):
    tid = await make_tenant("pc1")
    aid = await _account(tid)
    async with tenant_session(tid) as ts:
        ts.add(Contact(tenant_id=tid, account_id=aid, full_name="Kept Already",
                       linkedin_url="https://linkedin.com/in/dup-url/"))
        ts.add(Contact(tenant_id=tid, account_id=aid, full_name="Sam  Same"))
    actors = Actors(people=[
        _person("ACwRight1", "Dana", "Reed", "VP of Sales"),
        _person("ACwFleet", "Pat", "Other", "Fleet Coordinator", company="Flex E Lease"),
        _person("ACwOffice", "Lee", "Office", "Office Manager"),
        _person("dup-url", "Kept", "Elsewhere", "Head of Sales"),
        _person("ACwSame", "Sam", "Same", "Head of Sales"),
        _person("ACwRight1", "Dana", "Reed", "VP of Sales"),
    ])

    created = await _source(tid, aid, actors)

    assert created == [("Dana Reed", "VP of Sales", "https://www.linkedin.com/in/ACwRight1")]
    actor, body = actors.runs[-1]
    assert actor == "linkedin_company_employees"
    assert body["companies"] == ["https://www.linkedin.com/company/vanta-security/"]
    assert body["jobTitles"] == ["VP of Sales", "Head of Sales"]
    assert body["profileScraperMode"].startswith("Short")
    assert web.calls == 0


async def test_no_icp_titles_means_linkedin_is_not_asked_for_everyone(web):
    tid = await make_tenant("pc2")
    aid = await _account(tid, titles=())
    actors = Actors(people=[_person("ACw1", "A", "B", "Anything")])

    created = await _source(tid, aid, actors)

    assert actors.runs == []
    assert [c[0] for c in created] == ["Web Person"] and web.calls == 1


async def test_a_page_that_is_not_the_accounts_own_is_never_asked_for_its_people(web):
    tid = await make_tenant("pc3")
    aid = await _account(tid, stored_page=False)
    actors = Actors(by_name={"Vanta": CHAUFFEURS},
                    people=[_person("ACwFleet", "Pat", "Other", "Fleet Coordinator")])

    created = await _source(tid, aid, actors)

    assert [a for a, _ in actors.runs] == ["linkedin_company"], "the employees were not bought"
    assert [c[0] for c in created] == ["Web Person"] and web.calls == 1


async def test_linkedin_unavailable_falls_back_to_web_search(web):
    tid = await make_tenant("pc4")
    aid = await _account(tid)

    created = await _source(tid, aid, Actors(error=ApifyNotConfigured("x")))

    assert [c[0] for c in created] == ["Web Person"] and web.calls == 1


async def test_when_nobody_on_linkedin_fits_web_search_is_asked(web):
    tid = await make_tenant("pc5")
    aid = await _account(tid)
    actors = Actors(people=[_person("ACwOffice", "Lee", "Office", "Office Manager")])

    created = await _source(tid, aid, actors)

    assert [c[0] for c in created] == ["Web Person"] and web.calls == 1


async def test_the_limit_is_respected(web):
    tid = await make_tenant("pc6")
    aid = await _account(tid)
    actors = Actors(people=[_person(f"ACw{i}", f"P{i}", "Q", "VP of Sales") for i in range(6)])

    created = await _source(tid, aid, actors, limit=2)

    assert len(created) == 2
    assert actors.runs[-1][1]["maxItems"] <= 25


async def test_web_search_does_not_add_a_person_already_on_the_account_under_another_url(
    monkeypatch,
):
    from nexus.integrations.registry import get_registry

    tid = await make_tenant("pc7")
    aid = await _account(tid, titles=())
    async with tenant_session(tid) as ts:
        ts.add(Contact(tenant_id=tid, account_id=aid, full_name="Dana Reed",
                       linkedin_url="https://www.linkedin.com/in/dana-reed/"))
    fake = Web([
        ContactCandidate(full_name="D. Reed", title="VP Sales", source="exa",
                         linkedin_url="https://uk.linkedin.com/in/dana-reed?trk=x"),
        ContactCandidate(full_name="Robin  Vale", title="Head of Sales", source="exa",
                         linkedin_url="https://www.linkedin.com/in/robin-vale"),
        ContactCandidate(full_name="robin vale", title="Head of Sales", source="exa"),
    ])
    monkeypatch.setattr(get_registry(), "contact_search", fake.contact_search)

    created = await _source(tid, aid, Actors())

    assert [c[0] for c in created] == ["Robin  Vale"], "the same profile and the same name, once"
