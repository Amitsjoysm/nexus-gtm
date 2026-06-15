"""Discovery quality: web company-search must surface real companies, not SERP junk.

Reproduces the live bug where 'logistics companies' discovery persisted aggregator/job/news
pages as Accounts ('100 Top Companies in Pune | F6S' -> f6s.com, etc.)."""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from nexus.integrations.company_search import (
    SearchBackedCompanySearchProvider,
    clean_company_name,
    looks_like_company,
)


@dataclass
class _Hit:
    title: str
    url: str
    snippet: str = ""


class _FakeSearch:
    def __init__(self, hits):
        self._hits = hits

    async def search(self, query, *, limit=25):
        return self._hits[:limit]


def test_looks_like_company_rejects_aggregators_and_listicles():
    # The exact junk seen on the live stack.
    assert looks_like_company("f6s.com", "100 Top Companies in Pune — June 2026 | F6S") is False
    assert looks_like_company("workindia.in", "10000+ Jobs in Pune (June 2026)") is False
    assert looks_like_company("economictimes.indiatimes.com", "Business News Live") is False
    assert looks_like_company("linkedin.com", "Acme Robotics | LinkedIn") is False
    assert looks_like_company("indeed.com", "Logistics jobs") is False
    assert looks_like_company(None, "No domain") is False
    # Novel aggregator domains not on the host denylist, caught by title shape:
    # bare plural list-noun, a year token, and an absurd word count.
    assert looks_like_company("techlist.ai", "Fintech Companies in United States 2026") is False
    assert looks_like_company("startuprank.io", "50 Startups to Watch") is False
    assert looks_like_company("blog.example", "The State of Logistics in 2025") is False
    assert looks_like_company(
        "x.example", "A Very Long Headline About The Best Vendors Around Town"
    ) is False
    # Real companies pass.
    assert looks_like_company("northwind.example", "Northwind Logistics") is True
    assert looks_like_company("acme.io", "Acme Robotics") is True
    assert looks_like_company("cocacola.com", "Coca-Cola") is True  # hyphen survives


def test_clean_company_name_strips_site_branding():
    assert clean_company_name("Acme Robotics - Industrial IoT | acme.io") == "Acme Robotics"
    assert clean_company_name("Northwind Logistics | Home") == "Northwind Logistics"
    assert clean_company_name("Globex: Freight Forwarding") == "Globex"
    assert clean_company_name("Coca-Cola") == "Coca-Cola"  # no spaced separator
    assert clean_company_name(None) == ""


@pytest.mark.asyncio
async def test_search_provider_filters_non_companies():
    hits = [
        _Hit("100 Top Companies in Pune | F6S", "https://f6s.com/pune"),
        _Hit("10000+ Jobs in Pune - Urgent Vacancies", "https://workindia.in/jobs"),
        _Hit("Business News Live - Economic Times", "https://economictimes.indiatimes.com/x"),
        # Novel aggregator domain (not on the host denylist) — must be caught by title shape.
        _Hit("Fintech Companies in United States 2026 | TechList.ai", "https://techlist.ai/us"),
        _Hit("Northwind Logistics", "https://northwind.example"),
        # Real company whose SERP title carries site branding — kept, but name cleaned.
        _Hit("Globex Freight - Global Forwarding | globex.io", "https://globex.io/about"),
    ]
    provider = SearchBackedCompanySearchProvider(_FakeSearch(hits))
    out = await provider.search({"industries": ["Logistics"]}, limit=10)
    domains = sorted(c.domain for c in out)
    assert domains == ["globex.io", "northwind.example"]  # all junk dropped, real kept
    names = {c.domain: c.name for c in out}
    assert names["globex.io"] == "Globex Freight"  # branding stripped from the stored name
    assert all(c.confidence > 0 for c in out)
