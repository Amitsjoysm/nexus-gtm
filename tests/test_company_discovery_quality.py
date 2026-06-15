"""Discovery quality: web company-search must surface real companies, not SERP junk.

Reproduces the live bug where 'logistics companies' discovery persisted aggregator/job/news
pages as Accounts ('100 Top Companies in Pune | F6S' -> f6s.com, etc.)."""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from nexus.integrations.company_search import (
    SearchBackedCompanySearchProvider,
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
    # Real companies pass.
    assert looks_like_company("northwind.example", "Northwind Logistics") is True
    assert looks_like_company("acme.io", "Acme Robotics") is True


@pytest.mark.asyncio
async def test_search_provider_filters_non_companies():
    hits = [
        _Hit("100 Top Companies in Pune | F6S", "https://f6s.com/pune"),
        _Hit("10000+ Jobs in Pune - Urgent Vacancies", "https://workindia.in/jobs"),
        _Hit("Business News Live - Economic Times", "https://economictimes.indiatimes.com/x"),
        _Hit("Northwind Logistics", "https://northwind.example"),
        _Hit("Globex Freight", "https://globex.io/about"),
    ]
    provider = SearchBackedCompanySearchProvider(_FakeSearch(hits))
    out = await provider.search({"industries": ["Logistics"]}, limit=10)
    domains = sorted(c.domain for c in out)
    assert domains == ["globex.io", "northwind.example"]  # junk dropped, real kept
    assert all(c.confidence > 0 for c in out)
