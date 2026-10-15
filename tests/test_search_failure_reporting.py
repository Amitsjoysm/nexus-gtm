"""A search that failed is never remembered or reported as "nothing found" (2026-10-15).

Measured on the local deploy. Firecrawl's two panel keys answered 402 and the pool 429 for days.
Two gaps hid it:

- A rate-limited pool, or one whose request kept failing, returned ``[]`` WITHOUT setting
  ``last_failure``. The dork source caches any answer that carries no failure, so the outage was
  stored as "no results": ``web_cache`` held 1,016 Firecrawl answers, every one empty, replayed
  2,202 times, and an account stayed empty for up to a day after the provider recovered.
- A run whose provider failed was recorded ``empty``, the outcome of a quiet market, because the
  run recorder only asked whether anything was found. 74 runs in one day carried "every key in
  the 2-key pool was rejected (402)" and still read ``empty``.

The worker runs jobs concurrently, and the dork source and its provider are shared by every
account, so each call's failure is read from that call alone: the last test runs two accounts at
once, one failing and one healthy, and neither may be reported with the other's outcome.
"""
from __future__ import annotations

import asyncio

import pytest

from tests.test_search_engines import _FakeResp, _patch


# ---- the engines say when they failed ----------------------------------------------------------

async def test_exa_rate_limit_is_reported(monkeypatch):
    from nexus.integrations.search.engines import ExaSearchProvider

    _patch(monkeypatch, [_FakeResp(429)])
    provider = ExaSearchProvider("k")
    assert await provider.search("logistics", limit=5) == []
    assert "rate-limited" in provider.last_failure


async def test_firecrawl_rate_limit_is_reported(monkeypatch):
    from nexus.integrations.search.engines import FirecrawlSearchProvider

    _patch(monkeypatch, [_FakeResp(429)])
    provider = FirecrawlSearchProvider("fc-k")
    assert await provider.search("acme funding", limit=5) == []
    assert "rate-limited" in provider.last_failure


async def test_firecrawl_request_failure_is_reported(monkeypatch):
    from nexus.integrations.search import engines

    class _Down:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            raise ConnectionError("connection refused")

    _patch(monkeypatch, [_FakeResp(200)])
    monkeypatch.setattr(engines.httpx, "AsyncClient", lambda *a, **k: _Down())
    provider = engines.FirecrawlSearchProvider("fc-k")
    assert await provider.search("acme funding", limit=5) == []
    assert "failed" in provider.last_failure and "connection refused" in provider.last_failure


async def test_a_success_after_a_failure_reports_no_failure(monkeypatch):
    from nexus.integrations.search.engines import FirecrawlSearchProvider

    provider = FirecrawlSearchProvider("fc-k")
    _patch(monkeypatch, [_FakeResp(429)])
    await provider.search("acme funding", limit=5)
    assert provider.last_failure
    _patch(monkeypatch, [_FakeResp(200, {"data": {"web": [
        {"url": "https://acme.com/news", "title": "Acme raises $5M", "description": ""}]}})])
    assert len(await provider.search("acme funding", limit=5)) == 1
    assert provider.last_failure == ""


async def test_duckduckgo_says_when_it_was_refused(monkeypatch):
    from nexus.enrichment import browser

    class _Refused:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            raise RuntimeError("Client error '403 Forbidden'")

        async def get(self, *a, **k):
            return await self.post()

    monkeypatch.setattr(browser.httpx, "AsyncClient", lambda *a, **k: _Refused())
    ddg = browser.DuckDuckGoSearch()
    assert await ddg.search("acme funding", limit=5) == []
    assert "403" in ddg.last_failure


# ---- the dork source neither caches nor hides a failure ----------------------------------------

class _RateLimited:
    """Behaves like the engines above: clears its flag, waits on the network, then states it."""

    name = "firecrawl"
    query_dialect = "operator"

    def __init__(self):
        self.last_failure = ""

    async def search(self, query, *, limit=5):
        return await self.search_recent(query, limit=limit)

    async def search_recent(self, query, *, limit=5, days=90, include_domains=None,
                            exclude_domains=None):
        self.last_failure = ""
        await asyncio.sleep(0)
        self.last_failure = "2-key pool rate-limited after retries"
        return []


def _no_cache(monkeypatch):
    from nexus.fetching import cache

    stored: list = []

    async def get(*a, **k):
        return None

    async def put(*a, **k):
        stored.append(k.get("payload"))

    monkeypatch.setattr(cache, "get", get)
    monkeypatch.setattr(cache, "put", put)
    return stored


async def test_a_rate_limited_answer_is_not_cached(monkeypatch):
    from nexus.ingestion.sources import DorkedSearchSource, SourceUnavailable
    from nexus.models.account import Account

    stored = _no_cache(monkeypatch)
    src = DorkedSearchSource(search=_RateLimited(), max_queries=4)
    with pytest.raises(SourceUnavailable):
        await src.fetch(Account(name="Vanta", domain="vanta.com"))
    assert stored == [], "an outage was cached as 'no results'"


async def test_a_failed_provider_is_recorded_as_an_error_run(monkeypatch):
    from nexus.ingestion.service import IngestionService
    from nexus.ingestion.sources import DorkedSearchSource
    from nexus.models.account import Account

    _no_cache(monkeypatch)
    src = DorkedSearchSource(search=_RateLimited(), max_queries=4)
    items, run = await IngestionService([src])._run_one(
        Account(name="Vanta", domain="vanta.com"), src, 8.0)
    assert items == []
    assert run["outcome"] == "error"
    assert "rate-limited" in run["error"]
    assert "rate-limited" in run["provenance"].get("provider_failure", "")


async def test_web_news_records_a_refused_search_as_an_error(monkeypatch):
    from nexus.ingestion.service import IngestionService
    from nexus.ingestion.sources import WebNewsSource
    from nexus.models.account import Account

    class _Refused:
        last_failure = ""

        async def search(self, query, *, limit=5):
            self.last_failure = "DuckDuckGo answered 403 Forbidden"
            return []

    src = WebNewsSource(_Refused())
    _, run = await IngestionService([src])._run_one(Account(name="Vanta", domain="vanta.com"),
                                                    src, 8.0)
    assert run["outcome"] == "error" and "403" in run["error"]


async def test_a_quiet_account_on_a_healthy_provider_is_still_empty(monkeypatch):
    from nexus.ingestion.service import IngestionService
    from nexus.ingestion.sources import DorkedSearchSource
    from nexus.models.account import Account

    class _Healthy(_RateLimited):
        async def search_recent(self, query, **kw):
            self.last_failure = ""
            return []

    stored = _no_cache(monkeypatch)
    src = DorkedSearchSource(search=_Healthy(), max_queries=4)
    _, run = await IngestionService([src])._run_one(Account(name="Vanta", domain="vanta.com"),
                                                    src, 8.0)
    assert run["outcome"] == "empty" and run["error"] == ""
    assert len(stored) == 4, "a real empty answer is still cached: that IS the cadence"


async def test_two_accounts_at_once_each_get_their_own_outcome(monkeypatch):
    """The worker runs jobs concurrently and the source and its provider are shared. Account A's
    search fails while account B's succeeds; B finishes first and then waits on the cache write,
    which is exactly when A's failure lands on the shared provider."""
    from nexus.fetching import cache
    from nexus.ingestion.sources import DorkedSearchSource, SourceUnavailable
    from nexus.models.account import Account

    class _Shared:
        name = "firecrawl"
        query_dialect = "operator"
        last_failure = ""

        async def search_recent(self, query, *, limit=5, days=90, include_domains=None,
                                exclude_domains=None):
            self.last_failure = ""
            if "Alpha" in query:
                await asyncio.sleep(0.05)
                self.last_failure = "every key in the 2-key pool was rejected (#0:402, #1:402)"
                return []
            await asyncio.sleep(0.01)
            self.last_failure = ""
            return [{"title": "Beta raises $20M Series A", "url": "https://beta.io/news/a",
                     "snippet": "Beta raised a $20M Series A."}]

    async def get(*a, **k):
        return None

    async def slow_put(*a, **k):
        await asyncio.sleep(0.1)

    monkeypatch.setattr(cache, "get", get)
    monkeypatch.setattr(cache, "put", slow_put)
    src = DorkedSearchSource(search=_Shared(), max_queries=1)
    alpha, beta = await asyncio.gather(
        src.fetch(Account(name="Alpha", domain="alpha.io")),
        src.fetch(Account(name="Beta", domain="beta.io")),
        return_exceptions=True,
    )
    assert isinstance(alpha, SourceUnavailable), f"Alpha's dead pool was not reported: {alpha!r}"
    assert isinstance(beta, list), f"Beta was given Alpha's failure: {beta!r}"
