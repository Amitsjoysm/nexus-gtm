"""The three harvestapi LinkedIn actors: what they are asked, how their rows are read, and the gate
every company must pass before it can become an account.

Shapes are the ones captured from one approved run of each actor (2026-09-24). The fixtures are
synthetic: same keys and nesting, invented companies and people, so no real person's data is in
the repository.

Measured facts the tests pin:

* ``linkedin.com/company/vanta`` is *VANTA - Chauffeurs* (vantaexec.co.uk). Vanta the security
  company is ``vanta-security`` (vanta.com). A LinkedIn page is about an account only if the page's
  own website is the account's domain.
* The location filter matches ANY office: a "United Kingdom" search returned US-headquartered
  companies with a London office.
* ``similarOrganizations`` never carry a website (0 of 72 observed).
* Employee rows name the company only as ``currentPositions[].companyName``.
"""
from __future__ import annotations

import pytest

from nexus.integrations.apify import ACTORS
from nexus.prospecting import linkedin as li


def _company(**over) -> dict:
    row = {
        "id": "90001",
        "universalName": "acme-analytics",
        "linkedinUrl": "https://www.linkedin.com/company/acme-analytics/",
        "name": "Acme Analytics",
        "tagline": "Analytics for finance teams",
        "website": "http://www.acme-analytics.io",
        "employeeCount": 286,
        "employeeCountRange": {"start": 51, "end": 200},
        "description": "  Acme builds analytics.  ",
        "locations": [
            {"country": "GB", "headquarter": False, "parsed": {"countryCode": "GB"}},
            {"country": "US", "headquarter": True, "parsed": {"countryCode": "US"}},
            {"country": "US", "headquarter": False, "parsed": {"countryCode": "US"}},
        ],
        "industries": [{"id": "4", "name": "Software Development",
                        "urn": "urn:li:fsd_industryV2:4", "title": "Software Development",
                        "hierarchy": "Technology, Information and Media > Software Development"}],
        "similarOrganizations": [
            {"id": "90002", "universalName": "beta-metrics",
             "linkedinUrl": "https://www.linkedin.com/company/beta-metrics/",
             "name": "Beta Metrics", "website": None,
             "employeeCountRange": {"start": 11, "end": 50},
             "industries": [{"id": "4", "name": "Software Development"}]},
            {"id": "90003", "universalName": "", "linkedinUrl": "",
             "name": "No Page Ltd", "website": None},
        ],
        "_meta": {"pagination": {"totalElements": 1000, "totalPages": 20, "pageNumber": 3,
                                 "previousElements": 100, "pageSize": 50,
                                 "totalResultCount": 3032}},
    }
    row.update(over)
    return row


def _employee(**over) -> dict:
    row = {
        "id": "ACwAAAsynthetic1",
        "linkedinUrl": "https://www.linkedin.com/in/ACwAAAsynthetic1",
        "firstName": "Dana",
        "lastName": "Example",
        "currentPositions": [
            {"companyName": "Acme Analytics", "title": "VP of Sales", "current": True,
             "tenureAtPosition": {}, "tenureAtCompany": {}},
        ],
        "location": {"linkedinText": "London, England, United Kingdom"},
        "_meta": {"pagination": {"totalElements": 1, "totalPages": 1, "pageNumber": 1,
                                 "previousElements": 0, "pageSize": 25},
                  "query": {"currentJobTitles": ["VP of Sales"],
                            "currentCompanies": ["https://www.linkedin.com/company/acme-analytics"]}},
    }
    row.update(over)
    return row


# ---- registry ----------------------------------------------------------------------------------

def test_the_three_actors_are_registered_by_their_ids():
    assert ACTORS["linkedin_company_search"] == "taHaRcqil3scbchuI"
    assert ACTORS["linkedin_company"] == "UwSdACBp7ymaGUJjS"
    assert ACTORS["linkedin_company_employees"] == "Vb6LZkh4EqRlR0Ka9"


# ---- the domain gate ---------------------------------------------------------------------------

@pytest.mark.parametrize("website, expected", [
    ("http://www.acme-analytics.io", "acme-analytics.io"),
    ("https://Acme.com/about?x=1", "acme.com"),
    ("acme.co.uk", "acme.co.uk"),
    ("", ""),
    (None, ""),
    ("https://www.linkedin.com/company/acme", ""),
    ("https://facebook.com/acme", ""),
    ("https://www.instagram.com/acme", ""),
    ("https://x.com/acme", ""),
    ("https://sites.google.com/view/acme", ""),
    ("https://linktr.ee/acme", ""),
    ("https://beacons.ai/acme", ""),
    ("https://apps.apple.com/app/acme/id1", ""),
    ("https://play.google.com/store/apps/details?id=acme", ""),
    ("https://www.crunchbase.com/organization/acme", ""),
    ("https://github.com/acme", ""),
    ("https://gmail.com", ""),
])
def test_only_a_companys_own_website_becomes_its_domain(website, expected):
    assert li.company_domain(website) == expected


def test_a_page_found_by_name_counts_only_when_its_website_is_the_accounts_domain():
    chauffeurs = li.parse_company(_company(
        universalName="vanta", linkedinUrl="https://www.linkedin.com/company/vanta/",
        name="VANTA - Chauffeurs", website="https://www.vantaexec.co.uk/"))
    security = li.parse_company(_company(
        universalName="vanta-security", linkedinUrl="https://www.linkedin.com/company/vanta-security/",
        name="Vanta", website="https://vanta.com"))

    assert not li.page_is_for(chauffeurs, "vanta.com"), "a name match is not proof"
    assert li.page_is_for(security, "www.vanta.com")
    assert not li.page_is_for(security, ""), "no account domain, nothing can be proven"


# ---- reading a company row ---------------------------------------------------------------------

def test_a_company_row_is_read_into_the_fields_the_store_keeps():
    c = li.parse_company(_company())

    assert c.linkedin_url == "https://www.linkedin.com/company/acme-analytics/"
    assert c.linkedin_id == "90001"
    assert c.name == "Acme Analytics"
    assert c.domain == "acme-analytics.io"
    assert c.industry_id == 4 and c.industry_name == "Software Development"
    assert (c.range_min, c.range_max) == (51, 200)
    assert c.employee_count == 286, "kept as reported, even outside its own band"
    assert c.hq_country == "US", "the headquarter flag, not the first location"
    assert c.countries == ["GB", "US"], "every office country, once each, in order"
    assert c.description == "Acme builds analytics."


def test_similar_organisations_keep_their_page_and_never_a_guessed_website():
    c = li.parse_company(_company())
    assert c.similar == [{"name": "Beta Metrics",
                          "linkedin_url": "https://www.linkedin.com/company/beta-metrics/",
                          "industry_id": 4}], "an entry with no page cannot be looked up; dropped"


def test_a_row_without_a_website_parses_with_no_domain():
    c = li.parse_company(_company(website=None))
    assert c is not None and c.domain == ""


def test_a_row_with_neither_a_page_nor_a_name_is_not_a_company():
    assert li.parse_company({"_meta": {}}) is None
    assert li.parse_company("junk") is None


def test_the_headquarters_falls_back_to_the_first_office():
    c = li.parse_company(_company(locations=[
        {"country": "DE", "headquarter": False, "parsed": {"countryCode": "de"}}]))
    assert c.hq_country == "DE" and c.countries == ["DE"]


def test_the_linkedin_page_is_canonical():
    assert li.canonical_company_url("linkedin.com/company/Acme-Analytics?trk=x") == \
        "https://www.linkedin.com/company/acme-analytics/"
    assert li.canonical_company_url("https://uk.linkedin.com/company/acme/about/") == \
        "https://www.linkedin.com/company/acme/"
    assert li.canonical_company_url("https://www.linkedin.com/in/someone") == ""
    assert li.canonical_company_url("") == ""


def test_pagination_is_read_from_the_first_row():
    page = li.page_info([_company(), _company(id="2")])
    assert (page.number, page.total_pages, page.total_results) == (3, 20, 3032)
    assert li.page_info([]) == li.PageInfo(number=0, total_pages=0, total_results=0)


# ---- size bands --------------------------------------------------------------------------------

@pytest.mark.parametrize("lo, hi, bands", [
    (50, 250, ["11-50", "51-200", "201-500"]),
    (None, None, []),
    (5000, None, ["1001-5000", "5001-10000", "10001+"]),
    (None, 10, ["1-10"]),
    (201, 500, ["201-500"]),
])
def test_an_icp_size_range_becomes_every_band_it_overlaps(lo, hi, bands):
    assert li.size_bands(lo, hi) == bands


# ---- asking the actors -------------------------------------------------------------------------

class _FakeClient:
    def __init__(self, items):
        self.items = items
        self.calls: list[tuple[str, dict]] = []

    async def run_actor(self, actor, run_input, *, timeout=None):
        self.calls.append((actor, run_input))
        return self.items


async def test_a_search_reads_one_page_in_full_mode():
    client = _FakeClient([_company(), {"noise": True}])

    companies, page = await li.search_companies(
        industry_ids=[4, 43], locations=["United Kingdom"], sizes=["51-200"], page=3, client=client)

    actor, body = client.calls[0]
    assert actor == "linkedin_company_search"
    assert body == {"scraperMode": "full", "industryIds": ["4", "43"],
                    "locations": ["United Kingdom"], "companySize": ["51-200"],
                    "startPage": 3, "takePages": 1, "maxItems": 50}
    assert [c.name for c in companies] == ["Acme Analytics"]
    assert page.total_pages == 20


async def test_a_search_leaves_out_filters_it_was_not_given():
    client = _FakeClient([])
    await li.search_companies(industry_ids=[4], locations=[], sizes=[], page=1, client=client)
    body = client.calls[0][1]
    assert "locations" not in body and "companySize" not in body


async def test_details_are_looked_up_in_one_run_and_mapped_back_to_the_asked_url():
    beta = _company(id="90002", universalName="beta-metrics",
                    linkedinUrl="https://www.linkedin.com/company/beta-metrics/",
                    name="Beta Metrics", website="https://betametrics.com",
                    originalQuery={"search": "https://www.linkedin.com/company/beta-metrics",
                                   "location": ""})
    client = _FakeClient([beta])

    found = await li.company_details(
        ["https://www.linkedin.com/company/beta-metrics/"], client=client)

    actor, body = client.calls[0]
    assert actor == "linkedin_company"
    assert body == {"companies": ["https://www.linkedin.com/company/beta-metrics/"]}
    assert found["https://www.linkedin.com/company/beta-metrics/"].domain == "betametrics.com"


async def test_no_urls_means_no_run():
    client = _FakeClient([])
    assert await li.company_details([], client=client) == {}
    assert client.calls == []


# ---- employees ---------------------------------------------------------------------------------

async def test_employees_are_asked_for_on_one_page_with_the_icp_titles_in_short_mode():
    client = _FakeClient([_employee()])
    people = await li.company_employees(
        "https://www.linkedin.com/company/acme-analytics/", ["VP of Sales", "CRO"], limit=5,
        client=client)

    actor, body = client.calls[0]
    assert actor == "linkedin_company_employees"
    assert body == {"companies": ["https://www.linkedin.com/company/acme-analytics/"],
                    "jobTitles": ["VP of Sales", "CRO"], "profileScraperMode": "Short ($4 per 1k)",
                    "maxItems": 5}
    assert people[0].full_name == "Dana Example"
    assert people[0].title == "VP of Sales" and people[0].company_name == "Acme Analytics"
    assert people[0].location == "London, England, United Kingdom"


def test_the_current_position_is_the_one_marked_current():
    p = li.parse_employee(_employee(currentPositions=[
        {"companyName": "Old Co", "title": "Rep", "current": False},
        {"companyName": "Acme Analytics", "title": "Head of Sales", "current": True},
    ]))
    assert (p.company_name, p.title) == ("Acme Analytics", "Head of Sales")


def test_a_row_without_a_profile_is_not_a_person():
    assert li.parse_employee(_employee(linkedinUrl="", id="")) is None


def test_an_employee_row_must_prove_the_company_and_the_title():
    from nexus.lookalike.contacts import icp_title_targets

    targets = icp_title_targets({"buyer_titles": ["VP of Sales", "Head of Sales"]})
    right = li.parse_employee(_employee())
    other_company = li.parse_employee(_employee(currentPositions=[
        {"companyName": "Flex E Lease", "title": "Fleet Coordinator", "current": True}]))
    wrong_title = li.parse_employee(_employee(currentPositions=[
        {"companyName": "Acme Analytics Inc.", "title": "Office Manager", "current": True}]))

    assert li.employee_fits(right, "Acme Analytics, Inc.", targets) == (True, "")
    assert li.employee_fits(other_company, "Acme Analytics", targets) == (False, "wrong_company")
    assert li.employee_fits(wrong_title, "Acme Analytics", targets) == (False, "wrong_title")
