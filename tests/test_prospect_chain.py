"""Where ICP companies come from, in order: the shared database, then LinkedIn, then web search.

Decided with the product owner 2026-09-24:

* **Database first, always.** Companies any workspace's search has already paid for are offered
  before anything is bought. The check against what this workspace already holds is an SQL
  anti-join, so it has no size limit. The old discovery sent at most 256 held domains to the search
  as exclusions; a workspace holding 3,500 got the same top results back every day.
* **LinkedIn for the shortfall**, reading pages from a cursor SHARED by every workspace: a page
  once read lands in the shared store, and a second workspace gets those companies from the
  database step instead of paying to read the page again. Past LinkedIn's 1,000-result ceiling a
  query splits by size band, then country, then industry, each with its own cursor.
* **Web search only when LinkedIn delivered nothing or failed.**
* **No domain, no company.** A row without its own website is counted and dropped, never stored.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from nexus.core.db import get_platform_sessionmaker, utcnow
from nexus.integrations.apify import ApifyError, ApifyNotConfigured
from nexus.integrations.company_search import CompanyCandidate
from nexus.models import Account, Company, CompanyCountry, ProspectCursor
from nexus.prospecting import companies as chain
from tests.conftest import make_tenant, tenant_session

ICP = {"industries": ["Software Development"], "linkedin_industry_ids": [4],
       "countries": ["United Kingdom"], "employee_min": 50, "employee_max": 250}


def _row(n: int, *, website: str | None = "default", countries=("GB",), hq=None,
         band=(51, 200), industry=4, total=120, page=1) -> dict:
    website = f"https://co{n}.example-co.io" if website == "default" else website
    return {
        "id": str(1000 + n), "universalName": f"co{n}",
        "linkedinUrl": f"https://www.linkedin.com/company/co{n}/",
        "name": f"Company {n}", "website": website,
        "employeeCount": 100, "employeeCountRange": {"start": band[0], "end": band[1]},
        "locations": [{"headquarter": (c == (hq or countries[0])), "parsed": {"countryCode": c}}
                      for c in countries],
        "industries": [{"id": str(industry), "name": "Software Development"}],
        "similarOrganizations": [],
        "_meta": {"pagination": {"totalPages": max(1, -(-total // 50)), "pageNumber": page,
                                 "pageSize": 50, "totalResultCount": total}},
    }


class PagedClient:
    """A company-search actor that serves pages by ``startPage`` and records what it was asked."""

    def __init__(self, pages: dict[int, list[dict]] | None = None, *, error: Exception | None = None):
        self.pages = pages or {}
        self.error = error
        self.calls: list[dict] = []

    async def run_actor(self, actor, run_input, *, timeout=None):
        assert actor == "linkedin_company_search"
        self.calls.append(run_input)
        if self.error:
            raise self.error
        return self.pages.get(run_input["startPage"], [])


class Web:
    def __init__(self, candidates: list[CompanyCandidate] | None = None):
        self.candidates = candidates or []
        self.calls = 0

    async def __call__(self, icp, *, limit, exclude_domains=None):
        self.calls += 1
        return list(self.candidates)


async def _store(*rows: dict) -> None:
    from nexus.prospecting.linkedin import parse_company

    await chain.store_companies([parse_company(r) for r in rows])


async def _hold(tid: str, *domains: str) -> None:
    async with tenant_session(tid) as ts:
        for d in domains:
            ts.add(Account(tenant_id=tid, name=d, domain=d))


async def _run(tid, n, *, client=None, web=None, icp=ICP):
    async with tenant_session(tid) as ts:
        return await chain.find_icp_companies(
            ts, icp, n, client=client or PagedClient(), web_search=web or Web())


# ---- the database step -------------------------------------------------------------------------

async def test_the_database_answers_first_and_nothing_is_bought():
    tid = await make_tenant("db1")
    await _store(_row(1), _row(2), _row(3))
    await _hold(tid, "co2.example-co.io")
    client, web = PagedClient(), Web()

    result = await _run(tid, 2, client=client, web=web)

    assert sorted(c.domain for c in result.candidates) == ["co1.example-co.io", "co3.example-co.io"]
    assert {c.source for c in result.candidates} == {"database"}
    assert client.calls == [] and web.calls == 0


async def test_holding_more_than_the_old_exclusion_cap_still_finds_what_is_new():
    tid = await make_tenant("db2")
    rows = [_row(i) for i in range(300)]
    await _store(*rows)
    await _hold(tid, *[f"co{i}.example-co.io" for i in range(300) if i != 257])

    result = await _run(tid, 5)

    assert [c.domain for c in result.candidates] == ["co257.example-co.io"]


async def test_a_country_matches_any_office_not_only_the_headquarters():
    tid = await make_tenant("db3")
    await _store(_row(1, countries=("US", "GB"), hq="US"), _row(2, countries=("US",)))

    result = await _run(tid, 5, client=PagedClient())

    assert [c.domain for c in result.candidates] == ["co1.example-co.io"]
    assert result.candidates[0].country == "United States", "the account keeps its HQ country"


async def test_a_size_band_outside_the_icp_is_not_offered():
    tid = await make_tenant("db4")
    await _store(_row(1, band=(1001, 5000)), _row(2, band=(201, 500)))

    result = await _run(tid, 5)

    assert [c.domain for c in result.candidates] == ["co2.example-co.io"], "201-500 overlaps 50-250"


async def test_a_child_industry_counts_for_its_parent():
    tid = await make_tenant("db5")
    await _store(_row(1, industry=129))            # Capital Markets, under Financial Services (43)

    result = await _run(tid, 5, icp={**ICP, "linkedin_industry_ids": [43]})

    assert [c.domain for c in result.candidates] == ["co1.example-co.io"]


# ---- LinkedIn for the shortfall ----------------------------------------------------------------

async def test_the_shortfall_is_bought_and_everything_read_is_kept_for_later():
    tid = await make_tenant("li1")
    await _store(_row(1))
    await _hold(tid, "co3.example-co.io")
    client = PagedClient({1: [_row(2), _row(3), _row(4, website=None), _row(5), _row(6)]})

    result = await _run(tid, 3, client=client)

    assert [(c.domain, c.source) for c in result.candidates] == [
        ("co1.example-co.io", "database"),
        ("co2.example-co.io", "linkedin"), ("co5.example-co.io", "linkedin")]
    assert result.discarded == {"no_website": 1, "already_held": 1}
    async with get_platform_sessionmaker()() as s:
        stored = set((await s.scalars(select(Company.domain))).all())
    assert "co6.example-co.io" in stored, "a company read and not needed waits in the database"
    assert not any("co4" in d for d in stored), "nothing without a domain is ever stored"


async def test_a_second_workspace_reads_the_next_page_not_the_first():
    first, second = await make_tenant("li2a"), await make_tenant("li2b")
    client = PagedClient({1: [_row(i, total=500) for i in range(1, 4)],
                          2: [_row(i, total=500, page=2) for i in range(4, 7)]})

    await _run(first, 3, client=client)
    result = await _run(second, 5, client=client)

    assert [c["startPage"] for c in client.calls] == [1, 2]
    assert [c.source for c in result.candidates].count("database") == 3, "page 1 came free"


async def test_linkedin_is_not_asked_without_industry_codes():
    tid = await make_tenant("li3")
    client = PagedClient({1: [_row(1)]})
    web = Web([CompanyCandidate(name="Webco", domain="webco.io", industry=None, country=None)])

    result = await _run(tid, 2, client=client, web=web, icp={"countries": ["United Kingdom"]})

    assert client.calls == []
    assert [c.source for c in result.candidates] == ["web"]
    assert result.notes["linkedin"] == "no_industry_codes"


async def test_past_the_ceiling_the_query_splits_by_size_band():
    tid = await make_tenant("li4")
    client = PagedClient({1: [_row(1, total=3032)]})

    await _run(tid, 60, client=client)

    assert client.calls[0]["companySize"] == ["11-50", "51-200", "201-500"]
    bands_read = list(dict.fromkeys(tuple(c["companySize"]) for c in client.calls[1:]))
    assert bands_read == [("11-50",), ("51-200",), ("201-500",)]


async def test_an_exhausted_query_is_not_read_again_until_it_goes_stale():
    tid = await make_tenant("li5")
    client = PagedClient({1: [_row(1, total=1)]})
    await _run(tid, 5, client=client)
    await _run(tid, 5, client=client)
    assert len(client.calls) == 1, "one page of one: the cursor is exhausted"

    async with get_platform_sessionmaker()() as s:
        cursor = (await s.scalars(select(ProspectCursor))).one()
        cursor.last_read_at = utcnow() - timedelta(days=chain.REOPEN_AFTER_DAYS + 1)
        await s.commit()
    await _run(tid, 5, client=client)
    assert len(client.calls) == 2, "LinkedIn's results change; a stale cursor is read again"


# ---- web search only when LinkedIn gave nothing ------------------------------------------------

@pytest.mark.parametrize("error, note", [
    (ApifyNotConfigured("no key"), "not_configured"),
    (ApifyError("Apify refused actor (401)"), "failed"),
])
async def test_a_linkedin_failure_falls_through_to_web_search(error, note):
    tid = await make_tenant(f"wf{note[:3]}")
    web = Web([CompanyCandidate(name="Webco", domain="https://www.webco.io/", industry=None,
                                country="United Kingdom", employee_count=120)])

    result = await _run(tid, 2, client=PagedClient(error=error), web=web)

    assert [(c.domain, c.source) for c in result.candidates] == [("webco.io", "web")]
    assert result.notes["linkedin"] == note
    async with get_platform_sessionmaker()() as s:
        cursor = (await s.scalars(select(ProspectCursor))).one()
    assert cursor.next_page == 1, "a page nobody read is released, not skipped"


async def test_web_search_is_not_used_when_linkedin_delivered_something():
    tid = await make_tenant("wf3")
    web = Web([CompanyCandidate(name="Webco", domain="webco.io")])

    result = await _run(tid, 5, client=PagedClient({1: [_row(1, total=1)]}), web=web)

    assert web.calls == 0 and [c.source for c in result.candidates] == ["linkedin"]


async def test_web_results_pass_the_same_gates():
    tid = await make_tenant("wf4")
    await _hold(tid, "held.io")
    web = Web([
        CompanyCandidate(name="Held", domain="held.io"),
        CompanyCandidate(name="Social", domain="https://www.facebook.com/somebiz"),
        CompanyCandidate(name="Too big", domain="big.io", employee_count=9000),
        CompanyCandidate(name="Elsewhere", domain="fr.io", country="France"),
        CompanyCandidate(name="Fine", domain="fine.io"),
        CompanyCandidate(name="Fine again", domain="www.fine.io"),
    ])

    result = await _run(tid, 5, client=PagedClient(error=ApifyNotConfigured("x")), web=web)

    assert [c.domain for c in result.candidates] == ["fine.io"]
    assert result.discarded == {"already_held": 1, "no_website": 1, "outside_icp": 2,
                                "duplicate": 1}


# ---- the shared store --------------------------------------------------------------------------

async def test_storing_fills_blanks_and_the_domain_wins_over_a_second_page():
    from nexus.prospecting.linkedin import parse_company

    await chain.store_companies([parse_company(_row(1))])
    sub = _row(1)
    sub.update(linkedinUrl="https://www.linkedin.com/company/co1-subsidiary/", name="Sub")
    await chain.store_companies([parse_company(sub)])

    async with get_platform_sessionmaker()() as s:
        company = (await s.scalars(select(Company))).one()
        codes = (await s.scalars(select(CompanyCountry.country_code))).all()
        count = await s.scalar(select(func.count()).select_from(Company))
    assert count == 1
    assert company.linkedin_url == "https://www.linkedin.com/company/co1/"
    assert company.name == "Company 1"
    assert company.linkedin_industry_id == 4 and company.hq_country_code == "GB"
    assert (company.employee_range_min, company.employee_range_max) == (51, 200)
    assert codes == ["GB"]
