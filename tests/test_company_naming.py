"""A discovered company is named after ITSELF, never after the page that mentioned it.

Reported from staging 2026-09-10: "Find lookalikes" on Marketjoy added revpue.com as an account
named **"Marketjoy Competitor"**, and did the same to other domains. Reproduced locally by forcing
the plain-web-search path that staging must be on (any provider but Exa has no `search_companies`,
and Exa with no key silently degrades to DuckDuckGo), and it is not one bad title:

    domain           name stored                            the company, also in the title
    hypergen.io      "B2B Lead Generation Company"          Hypergen
    genriverai.com   "AI-native Outbound for B2B Meetings"  GenRiver
    xpandmedia.io    "B2B Outbound & Pipeline Generation"   Xpand Media
    growbots.com     "Outbound Sales Software"              Growbots

`clean_company_name` always kept the FIRST segment of the page title. SEO titles routinely lead with
a tagline — or with the competitor the page is targeting — and put the brand after the separator.
The DOMAIN is the one thing about a hit that is unambiguously the company, so the name is now the
title segment that corroborates it; never a segment carrying the seed's own name; and the domain
itself when the title offers nothing better.

Exa's `category=company` results already title a hit with the company's name, and those must come
through untouched — including names that do not spell the domain ("Outsource Demand Gen" is
odgleads.com). Regressing them to "Odgleads" would trade one wrong name for another.
"""
from __future__ import annotations

import pytest

from nexus.integrations.company_search import company_name_from_title


# ---- the staging report and its siblings ---------------------------------------------------------

def test_a_page_titled_after_the_seed_is_named_after_its_own_company():
    """The reported bug, exactly."""
    assert company_name_from_title(
        "Marketjoy Competitor | RevPue", "revpue.com", not_named=("Marketjoy", "marketjoy.com")
    ) == "RevPue"


@pytest.mark.parametrize("title,domain,expected", [
    ("B2B Lead Generation Company | Hypergen – Drive More Sales", "hypergen.io", "Hypergen"),
    ("AI-native Outbound for B2B Meetings | GenRiver", "genriverai.com", "GenRiver"),
    ("B2B Outbound & Pipeline Generation | Xpand Media", "xpandmedia.io", "Xpand Media"),
])
def test_a_tagline_first_title_is_named_by_the_segment_matching_the_domain(title, domain, expected):
    assert company_name_from_title(title, domain) == expected


def test_a_brand_inside_a_sentence_is_found():
    """No segment IS the brand here; the brand is a word inside one."""
    assert company_name_from_title(
        "Outbound Sales Software: Meet Your Next Client with Growbots", "growbots.com"
    ) == "Growbots"


def test_a_candidate_is_never_named_after_the_seed():
    """No segment corroborates the domain and the only one there is the seed's. The domain is
    honest; the seed's name on somebody else's account is not."""
    assert company_name_from_title(
        "Marketjoy Alternatives", "revpue.com", not_named=("Marketjoy",)
    ) == "Revpue"


def test_a_comparison_headline_is_not_a_name():
    assert company_name_from_title(
        "Best Lead Generation Agencies Compared", "acmeleads.com"
    ) == "Acmeleads"


def test_no_title_falls_back_to_the_domain():
    assert company_name_from_title(None, "revpue.com") == "Revpue"
    assert company_name_from_title("", "www.hypergen.io") == "Hypergen"


def test_a_hyphenated_name_survives():
    assert company_name_from_title("Coca-Cola | Refresh the World", "coca-cola.com") == "Coca-Cola"


# ---- what must NOT change ---------------------------------------------------------------------------

@pytest.mark.parametrize("title,domain", [
    # Live Exa `category=company` results for Marketjoy, 2026-09-10: already the company's name.
    ("BuzzLead.io", "buzzlead.io"),
    ("WeProspect Co.", "weprospect.co"),
    ("getGTM", "getgtm.ai"),
    ("Lead Gen Jay", "leadgenjay.com"),
    ("LeadXcel", "leadxcelb2b.com"),
    ("Leadrix Marketers Pvt, Ltd.", "leadrixmarketers.com"),
    ("Top Dog Leads LLC", "topdoglead.com"),
    # Real names that do not spell their domain — the case a naive "must match the domain" rule
    # would have regressed to "Odgleads" and "Webleadsinc".
    ("Outsource Demand Gen", "odgleads.com"),
    ("Web Leads Pipeline", "webleadsinc.us"),
])
def test_a_title_that_is_already_the_company_name_is_kept(title, domain):
    assert company_name_from_title(title, domain) == title


@pytest.mark.parametrize("title,domain,expected", [
    ("Opp | B2B Outbound Sales and Go-to-Market Agency", "opp.agency", "Opp"),
    ("Growleady - B2B Lead Generation & Cold Email Agency", "growleady.io", "Growleady"),
    ("LeadBoss | Guaranteed Meetings Booked", "leadboss.co", "LeadBoss"),
    # The brand leads but does not spell the domain: still the leading segment, as before.
    ("LeadGrow AI - Turn Strangers Into Customers", "leadgrowsignals.com", "LeadGrow AI"),
])
def test_a_brand_first_title_keeps_its_brand(title, domain, expected):
    assert company_name_from_title(title, domain) == expected


# ---- both callers use it ----------------------------------------------------------------------------

class _PlainWebSearch:
    """What staging has: a provider with NO `search_companies`, so hits are arbitrary web pages."""

    name = "plain"

    def __init__(self, hits):
        self._hits = hits

    async def search(self, query, *, limit=5):
        return self._hits[:limit]


async def test_lookalikes_are_named_after_the_candidate_not_the_seed():
    from nexus.integrations.search import SearchHit
    from nexus.integrations.search.provider import set_search_provider
    from nexus.lookalike import get_lookalike_service
    from nexus.models.account import Account
    from tests.conftest import make_tenant, tenant_session

    set_search_provider(_PlainWebSearch([
        SearchHit(title="Marketjoy Competitor | RevPue", url="https://revpue.com/", source="plain"),
        SearchHit(title="B2B Lead Generation Company | Hypergen – Drive More Sales",
                  url="https://hypergen.io", source="plain"),
    ]))
    try:
        tid = await make_tenant(slug="nm1")
        async with tenant_session(tid) as ts:
            seed = Account(tenant_id=tid, name="Marketjoy", domain="marketjoy.com")
            ts.add(seed)
            await ts.flush()
            out = await get_lookalike_service().find(ts, seed, limit=10)
    finally:
        set_search_provider(None)

    names = {lk.domain: lk.name for lk in out}
    assert names == {"revpue.com": "RevPue", "hypergen.io": "Hypergen"}


async def test_icp_discovery_names_candidates_after_the_company():
    """The orchestrator's discovery goes through `SearchBackedCompanySearchProvider`, the second
    caller of the old first-segment rule."""
    from nexus.integrations.company_search import SearchBackedCompanySearchProvider
    from nexus.integrations.search import SearchHit

    provider = SearchBackedCompanySearchProvider(_PlainWebSearch([
        SearchHit(title="AI-native Outbound for B2B Meetings | GenRiver",
                  url="https://genriverai.com", source="plain"),
    ]))
    out = await provider.search({"industries": ["lead generation"]}, limit=5)
    assert [c.name for c in out] == ["GenRiver"]
