"""Self-hosted first, paid second, keyless floor last — and never a lie about the dialect.

The chain must declare `plain`, the dialect of its WEAKEST member. A query rendered with `site:`
for Firecrawl returns nothing on DuckDuckGo, and the chain cannot know in advance which member will
answer.
"""
from __future__ import annotations

from nexus.integrations.search.fallback import FallbackSearchProvider
from nexus.integrations.search.provider import SearchHit


class Stub:
    def __init__(self, name, hits=None, failure="", dialect="plain"):
        self.name = name
        self.query_dialect = dialect
        self.last_failure = ""
        self._failure = failure
        self._hits = hits or []
        self.calls = 0

    async def search_recent(self, query, *, limit=5, days=90, include_domains=(), exclude_domains=()):
        self.calls += 1
        self.last_failure = self._failure
        return list(self._hits)

    async def search(self, query, *, limit=5):
        self.calls += 1
        self.last_failure = self._failure
        return list(self._hits)


HIT = SearchHit(title="Acme raises $40M", url="https://x.test/a", snippet="", source="x")


def test_the_chain_declares_the_weakest_dialect():
    chain = FallbackSearchProvider([Stub("nexusfetch"), Stub("firecrawl", dialect="operator")])
    assert chain.query_dialect == "plain"


async def test_the_first_provider_that_answers_wins():
    first, second = Stub("nexusfetch", hits=[HIT]), Stub("firecrawl", hits=[HIT])
    chain = FallbackSearchProvider([first, second])

    await chain.search_recent("acme funding", limit=3, days=90)

    assert (first.calls, second.calls) == (1, 0)
    assert chain.answered_by == "nexusfetch"


async def test_a_blocked_provider_falls_through():
    first = Stub("nexusfetch", failure="blocked")
    second = Stub("firecrawl", hits=[HIT])
    chain = FallbackSearchProvider([first, second])

    hits = await chain.search_recent("acme funding", limit=3, days=90)

    assert [h.url for h in hits] == ["https://x.test/a"]
    assert second.calls == 1
    assert chain.answered_by == "firecrawl"
    assert chain.last_failure == ""


async def test_a_raising_provider_falls_through():
    class Exploding(Stub):
        async def search_recent(self, *a, **k):
            raise RuntimeError("boom")

    chain = FallbackSearchProvider([Exploding("nexusfetch"), Stub("firecrawl", hits=[HIT])])
    assert [h.url for h in await chain.search_recent("q", limit=3, days=90)] == ["https://x.test/a"]


async def test_an_empty_answer_with_no_failure_is_accepted_not_retried():
    # "Nothing was published about this company" is a real answer. Retrying it down the chain would
    # pay a provider for a question we already had an answer to.
    first, second = Stub("nexusfetch"), Stub("firecrawl", hits=[HIT])
    chain = FallbackSearchProvider([first, second])

    assert await chain.search_recent("acme funding", limit=3, days=90) == []
    assert second.calls == 0


async def test_when_every_provider_fails_the_reason_survives():
    chain = FallbackSearchProvider([
        Stub("nexusfetch", failure="blocked"), Stub("firecrawl", failure="no key")
    ])

    assert await chain.search_recent("acme funding", limit=3, days=90) == []
    assert chain.last_failure == "nexusfetch: blocked; firecrawl: no key"
    assert chain.answered_by == ""


def test_without_a_fetcher_the_signal_provider_is_the_paid_one_alone(monkeypatch):
    # A deployment that never provisions the VM must behave exactly as before this existed.
    from nexus.core.config import get_settings
    from nexus.integrations.search.provider import build_signal_search_provider

    monkeypatch.setattr(get_settings(), "fetch_service_url", "", raising=False)
    assert not isinstance(build_signal_search_provider(), FallbackSearchProvider)


def test_with_a_fetcher_the_self_hosted_member_goes_first(monkeypatch):
    from nexus.core.config import get_settings
    from nexus.integrations.search.provider import build_signal_search_provider

    monkeypatch.setattr(get_settings(), "fetch_service_url", "http://fetch.test:8081", raising=False)
    provider = build_signal_search_provider()
    assert isinstance(provider, FallbackSearchProvider)
    assert provider.providers[0].name == "nexusfetch"


async def test_the_dork_source_picks_up_a_fetcher_set_after_it_was_built(monkeypatch):
    # The ingestion service is built once; a fetcher configured later must still be used, without
    # a restart — the same reason the query cap is read on every fetch.
    from nexus.core.config import get_settings
    from nexus.ingestion.sources import DorkedSearchSource

    settings = get_settings()
    monkeypatch.setattr(settings, "fetch_service_url", "", raising=False)
    source = DorkedSearchSource()
    assert not isinstance(source._provider(), FallbackSearchProvider)

    monkeypatch.setattr(settings, "fetch_service_url", "http://fetch.test:8081", raising=False)
    assert isinstance(source._provider(), FallbackSearchProvider)
