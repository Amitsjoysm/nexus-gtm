"""A repeated dork is served from `web_cache`, and a FAILED search is never cached.

The second half is the subtle one. A provider whose key pool is condemned returns `[]` and sets
`last_failure` — it does not raise. Caching that empty list would freeze the outage in place for
the whole TTL, and the account would report "no signals" long after the key was fixed.
"""
from __future__ import annotations

from nexus.ingestion.sources import DorkedSearchSource
from nexus.integrations.search.provider import SearchHit
from nexus.models.account import Account


class CountingProvider:
    """Records every query it is asked for, so 'was this bought again?' is a length check."""

    name = "counting"
    query_dialect = "plain"
    last_failure = ""

    def __init__(self, hits=None):
        self.queries: list[str] = []
        self._hits = hits if hits is not None else [
            SearchHit(title="Acme raises $40M Series B", url="https://x.test/a", snippet="",
                      source="counting")
        ]

    async def search_recent(self, query, *, limit=5, days=90, include_domains=(), exclude_domains=()):
        self.queries.append(query)
        return list(self._hits)


def _account() -> Account:
    return Account(id="a1", tenant_id="t1", name="Acme", domain="acme.test", industry="Software")


async def test_a_second_crawl_buys_nothing():
    provider = CountingProvider()
    source = DorkedSearchSource(search=provider, max_queries=2)

    first = await source.fetch(_account())
    bought_first = len(provider.queries)
    second = await source.fetch(_account())

    assert bought_first == 2
    assert len(provider.queries) == 2, "the second crawl re-bought a query it already had"
    assert [s.title for s in second] == [s.title for s in first]


async def test_the_cached_run_is_marked_in_provenance():
    provider = CountingProvider()
    source = DorkedSearchSource(search=provider, max_queries=1)

    await source.fetch(_account())
    await source.fetch(_account())

    assert source.last_provenance["queries"][0]["cached"] is True


async def test_a_failed_search_is_not_cached():
    class FailingProvider(CountingProvider):
        last_failure = "all keys rejected"

        async def search_recent(self, query, *, limit=5, days=90, include_domains=(),
                                exclude_domains=()):
            self.queries.append(query)
            return []

    provider = FailingProvider()
    source = DorkedSearchSource(search=provider, max_queries=1)

    await source.fetch(_account())
    await source.fetch(_account())

    assert len(provider.queries) == 2, "an outage was cached and replayed as 'no results'"


async def test_a_genuine_no_results_answer_is_cached():
    provider = CountingProvider(hits=[])
    source = DorkedSearchSource(search=provider, max_queries=1)

    await source.fetch(_account())
    await source.fetch(_account())

    assert len(provider.queries) == 1


async def test_a_stale_provider_failure_does_not_abort_a_cached_crawl():
    # The answer came from our database, not the provider, so a failure the provider recorded on
    # some EARLIER call says nothing about this one. Reading it would stop a crawl that bought
    # nothing and record an outage that did not happen.
    provider = CountingProvider()
    source = DorkedSearchSource(search=provider, max_queries=1)
    first = await source.fetch(_account())

    provider.last_failure = "all keys rejected"
    second = await source.fetch(_account())

    assert [s.title for s in second] == [s.title for s in first]
    assert "provider_failure" not in source.last_provenance


async def test_the_cache_can_be_switched_off(monkeypatch):
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "web_cache_enabled", False, raising=False)
    provider = CountingProvider()
    source = DorkedSearchSource(search=provider, max_queries=1)

    await source.fetch(_account())
    await source.fetch(_account())

    assert len(provider.queries) == 2
