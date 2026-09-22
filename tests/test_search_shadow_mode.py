"""Shadow: ask both, use the PAID answer, record what the free one would have returned.

The fallback chain alone cannot catch the failure this exists for. A self-hosted engine that answers
with WORSE results never falls through — it just quietly degrades what reps see. Shadow is how we
find that out before promoting it, with nothing about what a rep sees changing while it is on.
"""
from __future__ import annotations

from nexus.integrations.search.fallback import FallbackSearchProvider
from nexus.integrations.search.provider import SearchHit


class Stub:
    def __init__(self, name, hits, failure=""):
        self.name = name
        self.query_dialect = "plain"
        self.last_failure = ""
        self._failure = failure
        self._hits = hits
        self.calls = 0

    async def search_recent(self, query, *, limit=5, days=90, include_domains=(), exclude_domains=()):
        self.calls += 1
        self.last_failure = self._failure
        return list(self._hits)


FREE = [SearchHit(title="A directory profile", url="https://dir.test/acme", snippet="", source="f")]
PAID = [SearchHit(title="Acme raises $40M", url="https://news.test/a", snippet="", source="p"),
        SearchHit(title="Acme hires CFO", url="https://news.test/b", snippet="", source="p")]


async def test_shadow_uses_the_paid_answer_and_asks_both():
    free, paid = Stub("nexusfetch", FREE), Stub("firecrawl", PAID)
    chain = FallbackSearchProvider([free, paid], shadow=True)

    hits = await chain.search_recent("acme funding", limit=3, days=90)

    assert [h.url for h in hits] == ["https://news.test/a", "https://news.test/b"]
    assert (free.calls, paid.calls) == (1, 1)
    assert chain.answered_by == "firecrawl"
    assert chain.shadow_report == {"nexusfetch": 1, "firecrawl": 2}


async def test_a_failing_free_member_is_recorded_and_changes_nothing():
    chain = FallbackSearchProvider([Stub("nexusfetch", [], failure="blocked"),
                                    Stub("firecrawl", PAID)], shadow=True)

    hits = await chain.search_recent("acme funding", limit=3, days=90)

    assert len(hits) == 2
    assert chain.shadow_report == {"nexusfetch": None, "firecrawl": 2}


async def test_if_the_paid_member_fails_the_free_answer_is_still_served():
    # Shadow must never be worse than the chain it shadows: reps get the free answer rather than
    # nothing, exactly as the ordinary fallback would give them.
    chain = FallbackSearchProvider([Stub("nexusfetch", FREE),
                                    Stub("firecrawl", [], failure="no key")], shadow=True)

    hits = await chain.search_recent("acme funding", limit=3, days=90)

    assert [h.url for h in hits] == ["https://dir.test/acme"]
    assert chain.answered_by == "nexusfetch"
    assert chain.last_failure == ""


def test_the_setting_turns_it_on(monkeypatch):
    from nexus.core.config import get_settings
    from nexus.integrations.search.provider import build_signal_search_provider

    settings = get_settings()
    monkeypatch.setattr(settings, "fetch_service_url", "http://fetch.test:8081", raising=False)
    monkeypatch.setattr(settings, "signal_fetch_shadow", True, raising=False)
    assert build_signal_search_provider().shadow is True
