# tests/test_web_cache_store.py
"""The cache must be invisible when it works and harmless when it does not.

A cache that raises takes signal collection down with it, so every entry point here is total: a
failure is a miss. The expiry is the product decision (how often we re-ask), so it is tested
explicitly rather than assumed.
"""
from __future__ import annotations



from nexus.core.db import get_platform_sessionmaker
from nexus.fetching import cache
from nexus.models.web_cache import WebCache

HITS = [{"title": "Acme raises $40M Series B", "url": "https://x.test/a", "snippet": "", "source": "t"}]


async def test_a_miss_returns_none():
    assert await cache.get("search", "acme funding", engine="t", limit=4) is None


async def test_a_stored_answer_is_served_and_counted():
    await cache.put("search", "acme funding", engine="t", limit=4, payload=HITS, ttl_s=3600)

    assert await cache.get("search", "acme funding", engine="t", limit=4) == HITS

    async with get_platform_sessionmaker()() as s:
        row = await s.get(WebCache, cache.cache_key("search", "acme funding", engine="t", limit=4))
    assert row.hit_count == 1
    assert row.bytes > 0


async def test_an_expired_answer_is_a_miss():
    await cache.put("search", "old", engine="t", limit=4, payload=HITS, ttl_s=-1)
    assert await cache.get("search", "old", engine="t", limit=4) is None


async def test_the_key_separates_engine_and_limit():
    await cache.put("search", "same", engine="a", limit=4, payload=HITS, ttl_s=3600)
    assert await cache.get("search", "same", engine="b", limit=4) is None
    assert await cache.get("search", "same", engine="a", limit=6) is None


async def test_a_second_put_replaces_rather_than_duplicating():
    await cache.put("search", "q", engine="t", limit=4, payload=HITS, ttl_s=3600)
    await cache.put("search", "q", engine="t", limit=4, payload=[], ttl_s=3600)
    assert await cache.get("search", "q", engine="t", limit=4) == []


async def test_a_broken_database_is_a_miss_not_an_exception(monkeypatch):
    def boom():
        raise RuntimeError("database is on fire")

    monkeypatch.setattr(cache, "get_platform_sessionmaker", boom)
    assert await cache.get("search", "q", engine="t", limit=4) is None
    # put must not raise either: nothing is cached, collection continues.
    await cache.put("search", "q", engine="t", limit=4, payload=HITS, ttl_s=3600)


async def test_prune_deletes_only_expired_rows():
    await cache.put("search", "fresh", engine="t", limit=4, payload=HITS, ttl_s=3600)
    await cache.put("search", "stale", engine="t", limit=4, payload=HITS, ttl_s=-1)

    deleted = await cache.prune()

    assert deleted == 1
    assert await cache.get("search", "fresh", engine="t", limit=4) == HITS


async def test_a_bad_argument_is_a_miss_not_an_exception():
    # The whole contract: a caller that hands us something odd must still get a signal crawl, not
    # a traceback. `cache_key` normalises strings, so a non-string subject would raise.
    assert await cache.get("search", ["not", "a", "string"], engine="t", limit=4) is None
    await cache.put("search", ["not", "a", "string"], engine="t", limit=4, payload=[], ttl_s=60)


def test_a_query_is_case_insensitive_but_a_page_url_is_not():
    assert cache.cache_key("search", "Acme Funding") == cache.cache_key("search", "acme funding")
    assert (cache.cache_key("page", "https://acme.test/Pricing")
            != cache.cache_key("page", "https://acme.test/pricing"))


async def test_the_switch_off_means_every_lookup_misses(monkeypatch):
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "web_cache_enabled", False, raising=False)
    await cache.put("search", "q", engine="t", limit=4, payload=HITS, ttl_s=3600)
    assert await cache.get("search", "q", engine="t", limit=4) is None
