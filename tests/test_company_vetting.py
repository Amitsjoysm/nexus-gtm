"""Is a LinkedIn row's website really that company's own? And is it a company at all? (2026-10-28)

Measured on a copy of the local database: 6 of 16 accounts a Populate added carried another
company's domain. An acquired or subsidiary company's LinkedIn page lists its owner's website
(Hess Corporation -> chevron.com, Marathon Oil -> conocophillips.com, Diamond Offshore ->
noblecorp.com, Parker Wellbore -> nabors.com, Intermoor -> acteon.com, Pioneer -> pxd.com, which
lands on ExxonMobil), and the shared store then kept chevron.com as "Hess Corporation", 4,337
staff, for every workspace. Across the 261 LinkedIn-read companies on that copy (about forty
industries) the same check found link shorteners (kfrc.co, hubs.ly) and pages that are not
companies ("Stealth Startup", 37,852 staff; a podcast).

Decided with the product owner 2026-10-28:

* A row whose domain names the company is kept without a request.
* A row whose domain does not (6% of the 261) is kept only when its homepage names it: the title's
  first distinctive word of the name, or the name run together, or a landing domain that names
  it. Nabors' title mentions "Wellbore", which is why one word of the name is not enough.
* A doubtful site that does not load is refused: missing beats wrong.
* Refused rows are not delivered and not stored under that domain.
* Discovery leaves out associations, institutes, societies, councils and federations.

Inputs are the real names, domains and homepage titles measured on 2026-10-27.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from nexus.core.db import get_platform_sessionmaker
from nexus.models import Company
from nexus.prospecting import vetting
from nexus.prospecting.linkedin import parse_company

# (domain, LinkedIn name): the domain names the company.
NAMED = [
    ("murphyoilcorp.com", "Murphy Oil Corporation"), ("plains.com", "Plains"),
    ("exxonmobil.com", "ExxonMobil"), ("cbi.com", "CB&I"), ("eogresources.com", "EOG Resources"),
    ("oilstatesintl.com", "Oil States"), ("spe.org", "Society of Petroleum Engineers International"),
    ("api.org", "API - American Petroleum Institute"), ("xtoenergy.com", "XTO Energy"),
    ("tdwilliamson.com", "T.D. Williamson"), ("spglobal.com", "S&P Global Energy"),
    ("hiremilitary.us", "Tenova LLC (HireMilitary)"), ("talentify.us.com", "Talentify.io"),
    ("iopp.org", "Institute of Packaging Professionals (IoPP)"),
    ("ibm.com", "International Business Machines"), ("pg.com", "Procter & Gamble"),
    ("world-kinect.com", "World Kinect"), ("eqt.com", "EQT Corporation"),
]
# The domain does not name the company: these need their homepage to say so.
DOUBTFUL = [
    ("chevron.com", "Hess Corporation"), ("conocophillips.com", "Marathon Oil Corporation"),
    ("noblecorp.com", "Diamond Offshore"), ("nabors.com", "Parker Wellbore"),
    ("pxd.com", "Pioneer Natural Resources Company"), ("kfrc.co", "Kforce Inc"),
    ("hubs.ly", "Shell Catalysts & Technologies"), ("regi.com", "Chevron Fuels and Renewable Solutions"),
    ("acteon.com", "Intermoor"), ("a16z.com", "Andreessen Horowitz"),
    ("clr.com", "Continental Resources"), ("harmonic.ai", "Stealth Startup"),
    ("graycyan.ai", "HonestAI | Manufacturing AI Agents & Automation"),
]


@pytest.mark.parametrize("domain,name", NAMED)
def test_a_domain_that_names_the_company_needs_no_request(domain, name):
    assert vetting.names_company(domain, name)


@pytest.mark.parametrize("domain,name", DOUBTFUL)
def test_a_domain_that_does_not_name_the_company_is_doubtful(domain, name):
    assert not vetting.names_company(domain, name)


# What each doubtful homepage answered: (final host, status, title).
LANDED = {
    "chevron.com": ("www.chevron.com", 200, "Chevron Corporation - Human Energy — Chevron"),
    "conocophillips.com": ("www.conocophillips.com", 200,
                           "Oil & Natural Gas, Exploration & Production ConocoPhillips"),
    "noblecorp.com": ("noblecorp.com", 200, "Home - Noble Corporation"),
    "nabors.com": ("www.nabors.com", 200,
                   "Automated Drilling Rigs, Software, Wellbore Services & Equipment"),
    "pxd.com": ("corporate.exxonmobil.com", 403, "Access Denied"),
    "kfrc.co": ("bitly.com", 200, "Custom BSD Landing Page - Bitly"),
    "regi.com": ("www.regi.com", 200, "Our Lower Carbon Fuel Solutions - Chevron"),
    "acteon.com": ("acteon.com", 200, "Acteon – Offshore Energy Engineering | Acteon"),
    "a16z.com": ("a16z.com", 200, "Andreessen Horowitz | Software Is Eating the World"),
    "clr.com": ("www.clr.com", 200, "Continental Resources - America's Energy Champion"),
    "harmonic.ai": ("harmonic.ai", 200, "Harmonic.ai - The complete startup database"),
    "graycyan.ai": ("graycyan.ai", 200, "Manufacturing AI & Compliance Automation | GrayCyan"),
    "hiremilitary.us": ("tenovallc.com", 200, "HireMilitary"),
    "global-foodsafety.com": ("global-foodsafety.com", 403, "403 Forbidden"),
}


class Landing:
    """The homepages above, as measured; anything else does not load. Records what was asked."""

    def __init__(self):
        self.asked: list[str] = []

    async def __call__(self, domain: str):
        self.asked.append(domain)
        if domain not in LANDED:
            raise OSError("did not load")
        return LANDED[domain]


#: regi.com is Renewable Energy Group's old domain, and its homepage now presents Chevron's
#: renewable fuels business: the page and the site agree, so it is that unit's own website.
KEPT_BY_PAGE = {"a16z.com", "clr.com", "regi.com"}


@pytest.mark.parametrize("domain,name", DOUBTFUL)
async def test_a_doubtful_row_is_kept_only_when_its_homepage_names_it(domain, name):
    vetting.forget()
    assert await vetting.homepage_names(domain, name, fetch=Landing()) == (domain in KEPT_BY_PAGE)


async def test_one_word_of_the_name_in_the_title_is_not_enough():
    vetting.forget()
    # Nabors sells "Wellbore Services"; Parker Wellbore is the company it bought.
    assert not await vetting.homepage_names("nabors.com", "Parker Wellbore", fetch=Landing())


async def test_landing_on_a_domain_that_names_the_company_keeps_it():
    vetting.forget()
    assert await vetting.homepage_names("hiremilitary.us", "Tenova LLC", fetch=Landing())


async def test_a_site_that_refuses_or_does_not_load_is_refused():
    vetting.forget()
    landing = Landing()
    assert not await vetting.homepage_names("global-foodsafety.com", "Global FS International",
                                            fetch=landing)
    assert not await vetting.homepage_names("gone.example", "Gone Co", fetch=landing)


def _li(name: str, domain: str, n: int = 1):
    return parse_company({
        "id": str(5000 + n), "linkedinUrl": f"https://www.linkedin.com/company/c{n}/",
        "name": name, "website": f"https://{domain}",
        "employeeCount": 2000, "employeeCountRange": {"start": 1001, "end": 5000},
        "locations": [{"headquarter": True, "parsed": {"countryCode": "US"}}],
        "industries": [{"id": "57", "name": "Oil and Gas"}],
    })


async def test_vet_asks_only_about_doubtful_rows_and_splits_them():
    vetting.forget()
    landing = Landing()
    rows = [_li("Murphy Oil Corporation", "murphyoilcorp.com", 1),
            _li("Hess Corporation", "chevron.com", 2),
            _li("Andreessen Horowitz", "a16z.com", 3)]

    kept, refused = await vetting.vet(rows, fetch=landing)

    assert [c.domain for c in kept] == ["murphyoilcorp.com", "a16z.com"]
    assert [c.domain for c in refused] == ["chevron.com"]
    assert landing.asked == ["chevron.com", "a16z.com"], "a named domain costs no request"


async def test_a_refused_row_never_reaches_the_shared_store(monkeypatch):
    from nexus.prospecting import companies as chain

    vetting.forget()
    monkeypatch.setattr(vetting, "_fetch_homepage", Landing())
    await chain.store_companies([_li("Hess Corporation", "chevron.com", 2),
                                 _li("Murphy Oil Corporation", "murphyoilcorp.com", 1)])

    async with get_platform_sessionmaker()() as s:
        stored = set((await s.scalars(select(Company.domain))).all())
    assert stored == {"murphyoilcorp.com"}, "chevron.com is never Hess Corporation"


async def test_the_chain_refuses_both_and_the_next_rows_fill_the_request(monkeypatch):
    from tests.conftest import make_tenant
    from tests.test_prospect_chain import ICP, PagedClient, _row, _run

    vetting.forget()
    monkeypatch.setattr(vetting, "_fetch_homepage", Landing())
    hess = {**_row(1), "name": "Hess Corporation", "website": "https://www.chevron.com"}
    api = {**_row(2), "name": "API - American Petroleum Institute", "website": "https://api.org"}
    tid = await make_tenant("vet1")

    result = await _run(tid, 2, client=PagedClient({1: [hess, api, _row(3), _row(4)]}), icp=ICP)

    assert [c.domain for c in result.candidates] == ["co3.example-co.io", "co4.example-co.io"]
    assert result.discarded == {"domain_not_theirs": 1, "association": 1}
    async with get_platform_sessionmaker()() as s:
        stored = set((await s.scalars(select(Company.domain))).all())
    assert "chevron.com" not in stored and "api.org" in stored, \
        "an association is a real organisation under its own domain; only discovery leaves it out"


# ---- rows stored before the guard (`scripts/vet_linkedin_companies.py`) ------------------------

async def _store_unvetted(*rows):
    from nexus.prospecting import companies as chain

    await chain.store_companies(list(rows), vetted=True)


async def _run_script(monkeypatch, *args):
    import sys

    from scripts import vet_linkedin_companies

    monkeypatch.setattr(sys, "argv", ["vet_linkedin_companies.py", *args])
    await vet_linkedin_companies.main()


async def test_the_dry_run_reports_and_changes_nothing(monkeypatch, capsys):
    vetting.forget()
    monkeypatch.setattr(vetting, "_fetch_homepage", Landing())
    await _store_unvetted(_li("Hess Corporation", "chevron.com", 2),
                          _li("Murphy Oil Corporation", "murphyoilcorp.com", 1))

    await _run_script(monkeypatch)

    out = capsys.readouterr().out
    assert "chevron.com" in out and "Hess Corporation" in out and "murphyoilcorp.com" not in out
    async with get_platform_sessionmaker()() as s:
        hess = (await s.scalars(select(Company).where(Company.domain == "chevron.com"))).one()
    assert hess.labels_quarantined_at is None and hess.linkedin_url


async def test_apply_quarantines_and_detaches_so_the_owners_own_page_can_attach(monkeypatch):
    from nexus.prospecting import companies as chain

    vetting.forget()
    monkeypatch.setattr(vetting, "_fetch_homepage", Landing())
    await _store_unvetted(_li("Hess Corporation", "chevron.com", 2),
                          _li("Murphy Oil Corporation", "murphyoilcorp.com", 1))

    await _run_script(monkeypatch, "--apply")

    async with get_platform_sessionmaker()() as s:
        hess = (await s.scalars(select(Company).where(Company.domain == "chevron.com"))).one()
        murphy = (await s.scalars(
            select(Company).where(Company.domain == "murphyoilcorp.com"))).one()
    assert hess.labels_quarantined_at is not None, "offered to nobody"
    assert hess.linkedin_url is None and hess.employee_count is None and hess.name == ""
    assert hess.field_sources["linkedin_url"]["ref"] == "https://www.linkedin.com/company/c2/"
    assert murphy.labels_quarantined_at is None and murphy.linkedin_url

    # Chevron's own page, read later, attaches and lifts the quarantine.
    await chain.store_companies([_li("Chevron", "chevron.com", 9)])
    async with get_platform_sessionmaker()() as s:
        chevron = (await s.scalars(select(Company).where(Company.domain == "chevron.com"))).one()
    assert chevron.linkedin_url == "https://www.linkedin.com/company/c9/"
    assert chevron.name == "Chevron" and chevron.labels_quarantined_at is None


# ---- associations ------------------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "API - American Petroleum Institute", "Society of Petroleum Engineers International",
    "Institute of Packaging Professionals (IoPP)", "National Retail Federation",
    "American Chemistry Council", "Texas Oil & Gas Association",
])
def test_associations_and_institutes_are_named_as_such(name):
    assert vetting.is_association(name)


@pytest.mark.parametrize("name", [
    "Murphy Oil Corporation", "Institutional Capital Group", "Society6", "Federated Hermes",
])
def test_a_word_inside_another_word_does_not_count(name):
    assert not vetting.is_association(name)


def test_the_accepted_cost_a_company_named_with_one_of_the_words_is_left_out():
    # The owner chose the plain word rule knowing this: "Council Oak Partners" is a firm.
    assert vetting.is_association("Council Oak Partners")
