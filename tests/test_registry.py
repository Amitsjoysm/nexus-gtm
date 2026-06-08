# tests/test_registry.py
"""DataSourceRegistry + capability providers: waterfall, merge, provenance, budgets, breakers.

All offline: providers are in-process fakes, so the registry's behavior is fully deterministic.
"""
from __future__ import annotations

import pytest

from nexus.integrations.company_search import (
    CompanyCandidate,
    CompanySearchProvider,
    SearchBackedCompanySearchProvider,
    StubCompanySearchProvider,
    domain_from_url,
    icp_to_query,
)
from nexus.integrations.provenance import Sourced, merge_sourced
from nexus.integrations.registry import DataSourceRegistry, build_registry_from_settings
from nexus.integrations.search import SearchHit, SearchProvider, StubSearchProvider
from nexus.research import StubResearchProvider
from nexus.verification import (
    STATUS_INVALID,
    STATUS_UNKNOWN,
    StubEmailVerificationProvider,
)

# --------------------------------------------------------------------------- fakes


class FakeSearch(SearchProvider):
    def __init__(self, name, hits):
        self.name = name
        self._hits = hits

    async def search(self, query, *, limit=5):
        return list(self._hits)[:limit]


class FakeCompanySearch(CompanySearchProvider):
    def __init__(self, name, candidates):
        self.name = name
        self._candidates = candidates

    async def search(self, icp, *, limit=25):
        return list(self._candidates)[:limit]


class BoomCompanySearch(CompanySearchProvider):
    """Always raises — exercises provider isolation + the circuit breaker."""

    def __init__(self, name="boom"):
        self.name = name
        self.calls = 0

    async def search(self, icp, *, limit=25):
        self.calls += 1
        raise RuntimeError("nope")


# ----------------------------------------------------------------- provenance unit


def test_merge_sourced_highest_confidence_wins():
    a = Sourced("InfoJoy", source="infojoy", confidence=0.9)
    b = Sourced("Apify", source="apify", confidence=0.6)
    assert merge_sourced(a, b) is a          # incoming lower -> keep current
    assert merge_sourced(b, a) is a          # incoming higher -> take it
    assert merge_sourced(None, b) is b
    assert merge_sourced(a, None) is a


def test_merge_sourced_tie_keeps_current():
    a = Sourced("x", source="first", confidence=0.5)
    b = Sourced("y", source="second", confidence=0.5)
    assert merge_sourced(a, b) is a


# ------------------------------------------------------------------ helpers unit


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.acme.com/about", "acme.com"),
        ("http://Acme.com", "acme.com"),
        ("acme.com/x", "acme.com"),
        ("https://user:pw@acme.com:8443/p", "acme.com"),
        ("", None),
        (None, None),
    ],
)
def test_domain_from_url(url, expected):
    assert domain_from_url(url) == expected


def test_icp_to_query_is_stable():
    icp = {"industries": ["Fintech"], "geo": ["United States"], "intent_signals": ["hiring"]}
    q = icp_to_query(icp)
    assert q == icp_to_query(icp)            # stable for caching
    assert "Fintech" in q and "companies" in q and "United States" in q


# ----------------------------------------------------------- company_search merge


@pytest.mark.asyncio
async def test_company_search_waterfall_merges_by_domain_with_provenance():
    # High-priority source supplies the name; low-priority fills the missing employee_count.
    infojoy = FakeCompanySearch(
        "infojoy",
        [CompanyCandidate(name="Acme", domain="acme.com", source="infojoy", confidence=0.95)],
    )
    apify = FakeCompanySearch(
        "apify",
        [CompanyCandidate(name="Acme Corp", domain="acme.com", employee_count=500,
                          source="apify", confidence=0.6)],
    )
    reg = DataSourceRegistry(company_search=[infojoy, apify])

    out = await reg.company_search({"industries": ["x"]}, limit=10)
    assert len(out) == 1
    cand = out[0]
    assert cand.name == "Acme"                 # anchor (higher priority) wins the conflict
    assert cand.employee_count == 500          # gap filled from the lower-priority source
    assert cand.provenance["name"] == "infojoy"
    assert cand.provenance["employee_count"] == "apify"
    assert cand.confidence == 0.95             # best confidence across sources


@pytest.mark.asyncio
async def test_company_search_skips_candidates_without_domain():
    src = FakeCompanySearch("s", [CompanyCandidate(name="NoDomain", source="s")])
    reg = DataSourceRegistry(company_search=[src])
    assert await reg.company_search({}, limit=5) == []


@pytest.mark.asyncio
async def test_company_search_caches_by_normalized_query():
    src = FakeCompanySearch(
        "s", [CompanyCandidate(name="A", domain="a.com", source="s")]
    )
    reg = DataSourceRegistry(company_search=[src])
    icp = {"industries": ["Fintech"]}
    first = await reg.company_search(icp, limit=5)
    second = await reg.company_search(icp, limit=5)
    assert [c.domain for c in first] == [c.domain for c in second]
    # One underlying call per source despite two registry calls (budget proves the cache hit).
    assert reg.source_health()["s"]["calls"] == 1


# --------------------------------------------------------- isolation + breaker


@pytest.mark.asyncio
async def test_failing_provider_is_isolated_and_trips_breaker():
    boom = BoomCompanySearch()
    good = FakeCompanySearch(
        "good", [CompanyCandidate(name="G", domain="g.com", source="good")]
    )
    reg = DataSourceRegistry(company_search=[boom, good], breaker_threshold=3,
                             cache_enabled=False)

    # A raising source never breaks the waterfall — the good source still yields.
    for _ in range(3):
        out = await reg.company_search({"n": 1}, limit=5)
        assert [c.domain for c in out] == ["g.com"]

    health = reg.source_health()
    assert health["boom"]["open"] is True      # breaker tripped after threshold failures
    calls_at_open = boom.calls
    # Once open, the breaker short-circuits the source: no further calls.
    await reg.company_search({"n": 2}, limit=5)
    assert boom.calls == calls_at_open


@pytest.mark.asyncio
async def test_per_source_budget_caps_calls():
    src = FakeCompanySearch("s", [CompanyCandidate(name="A", domain="a.com", source="s")])
    reg = DataSourceRegistry(company_search=[src], per_source_budget=2, cache_enabled=False)
    for _ in range(5):
        await reg.company_search({"q": 1}, limit=5)
    assert reg.source_health()["s"]["calls"] == 2   # capped at the budget


# ---------------------------------------------------------------- search seam


@pytest.mark.asyncio
async def test_registry_search_returns_hits_and_caches():
    hit = SearchHit(title="T", url="https://t.com", snippet="s", source="fake")
    reg = DataSourceRegistry(search=FakeSearch("fake", [hit]))
    out = await reg.search("q", limit=3)
    assert out[0].url == "https://t.com"
    await reg.search("q", limit=3)
    assert reg.source_health()["fake"]["calls"] == 1


@pytest.mark.asyncio
async def test_search_backed_company_search_derives_domains():
    search = FakeSearch(
        "web",
        [
            SearchHit(title="NewBank", url="https://newbank.com/about", snippet="fintech"),
            SearchHit(title="No URL", url="", snippet="skip me"),
        ],
    )
    provider = SearchBackedCompanySearchProvider(search)
    out = await provider.search({"industries": ["Fintech"], "geo": ["US"]}, limit=10)
    assert [c.domain for c in out] == ["newbank.com"]
    assert out[0].industry == "Fintech" and out[0].country == "US"


# --------------------------------------------------------- stubs stay offline


@pytest.mark.asyncio
async def test_stubs_are_zero_network_noops():
    assert await StubSearchProvider().search("x") == []
    assert await StubCompanySearchProvider().search({}) == []
    # Research stub is deterministic-but-grounded: it returns templated facts (zero-network)
    # so the grounding pipeline is exercisable offline. The facts carry source="stub".
    prof = await StubResearchProvider().research(company="Acme")
    assert prof.found is True and prof.highlights and prof.source == "stub"
    valid = await StubEmailVerificationProvider().verify_one("a@b.com")
    assert valid.status == STATUS_UNKNOWN
    bad = await StubEmailVerificationProvider().verify_one("not-an-email")
    assert bad.status == STATUS_INVALID


# -------------------------------------------------- settings-built registry


@pytest.mark.asyncio
async def test_build_registry_from_settings_defaults_are_offline_with_fake_browser():
    from tests.conftest import FakeBrowser

    # Default sources: company_search="search" (web-backed), search="duckduckgo".
    # With an empty browser the whole thing yields nothing — net-new stays 0 offline.
    reg = build_registry_from_settings(browser=FakeBrowser([]))
    assert await reg.company_search({"industries": ["Fintech"]}, limit=5) == []
    assert await reg.search("anything", limit=3) == []


@pytest.mark.asyncio
async def test_build_registry_from_settings_routes_browser_hits():
    from tests.conftest import FakeBrowser

    hits = [{"title": "NewBank", "url": "https://newbank.com/about", "snippet": "fintech"}]
    reg = build_registry_from_settings(browser=FakeBrowser(hits))
    out = await reg.company_search({"industries": ["Fintech"], "geo": ["US"]}, limit=5)
    assert [c.domain for c in out] == ["newbank.com"]
    assert out[0].provenance["name"] == "search"
