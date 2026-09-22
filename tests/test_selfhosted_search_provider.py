"""The self-hosted backend is a `plain` engine, like DuckDuckGo.

Declaring `operator` would make every dork render `site:` terms, which these endpoints match
literally — measured: zero results, silently, forever.
"""
from __future__ import annotations

from nexus.fetching.client import FetchClient
from nexus.integrations.search.engines import SelfHostedSearchProvider
from nexus.integrations.search.provider import build_search_provider


class FakeClient(FetchClient):
    def __init__(self, hits=None, failure=""):
        super().__init__(base_url="http://fetch.test", token="t")
        self._hits = hits or []
        self._failure = failure
        self.calls: list[dict] = []

    async def search(self, query, *, limit=5, recency_days=0):
        self.calls.append({"query": query, "limit": limit, "recency_days": recency_days})
        self.last_failure = self._failure
        return list(self._hits)


def test_it_declares_the_plain_dialect():
    assert SelfHostedSearchProvider(client=FakeClient()).query_dialect == "plain"


async def test_hits_are_normalised_to_search_hits():
    client = FakeClient(hits=[{"title": "Acme raises $40M", "url": "https://x.test/a",
                               "snippet": "s", "source": "nexusfetch:ddg"}])

    hits = await SelfHostedSearchProvider(client=client).search("acme funding", limit=3)

    assert hits[0].title == "Acme raises $40M"
    assert hits[0].url == "https://x.test/a"
    assert hits[0].source == "nexusfetch:ddg"


async def test_a_hit_without_a_url_is_dropped():
    client = FakeClient(hits=[{"title": "no link"}, {"title": "linked", "url": "https://x.test/b"}])
    hits = await SelfHostedSearchProvider(client=client).search("q", limit=3)
    assert [h.url for h in hits] == ["https://x.test/b"]


async def test_recency_is_passed_through():
    client = FakeClient()
    await SelfHostedSearchProvider(client=client).search_recent("acme funding", limit=3, days=120)
    assert client.calls[0]["recency_days"] == 120


async def test_a_failure_is_reported_on_last_failure_and_cleared_by_a_success():
    client = FakeClient(failure="blocked")
    provider = SelfHostedSearchProvider(client=client)

    assert await provider.search("acme funding", limit=3) == []
    assert provider.last_failure == "blocked"

    client._failure = ""
    await provider.search("acme funding", limit=3)
    assert provider.last_failure == ""


def test_the_settings_token_builds_it():
    assert isinstance(build_search_provider("nexusfetch"), SelfHostedSearchProvider)
