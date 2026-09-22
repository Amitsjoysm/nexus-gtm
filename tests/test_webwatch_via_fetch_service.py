"""First-party pages go through nexus-fetch when it is configured, and through httpx when it is not.

The fallback is not optional: a deployment with no VM must keep watching pages exactly as it does
today. And a page fetched for one tenant is reused for the next within the page TTL — which is also
what makes `page_cache_ttl_s` a setting that does something, rather than one the panel shows while
nothing reads it.
"""
from __future__ import annotations

import pytest

from nexus.fetching.client import FetchClient, set_fetch_client
from nexus.ingestion import webwatch

PAGE = {"status": 200, "final_url": "https://acme.test/pricing", "html": "<html>Plans</html>",
        "text": "Plans", "title": "Plans"}


class FakeClient(FetchClient):
    def __init__(self, page=None):
        super().__init__(base_url="http://fetch.test", token="t")
        self._page = page
        self.urls: list[str] = []

    async def fetch_page(self, url, *, mode="http"):
        self.urls.append(url)
        return self._page


@pytest.fixture(autouse=True)
def _reset_client():
    yield
    set_fetch_client(None)


@pytest.fixture
def direct(monkeypatch):
    """Records direct httpx fetches instead of touching the network."""
    calls: list[str] = []

    async def fake_direct(url):
        calls.append(url)
        return 200, "<html>direct</html>"

    monkeypatch.setattr(webwatch, "_direct_get", fake_direct)
    return calls


async def test_a_configured_service_serves_the_page(direct):
    set_fetch_client(FakeClient(page=PAGE))

    assert await webwatch._get("https://acme.test/pricing") == (200, "<html>Plans</html>")
    assert direct == []


async def test_an_unconfigured_service_falls_back_to_httpx(direct):
    set_fetch_client(FetchClient(base_url="", token=""))

    assert await webwatch._get("https://acme.test/pricing") == (200, "<html>direct</html>")
    assert direct == ["https://acme.test/pricing"]


async def test_a_service_failure_falls_back_rather_than_losing_the_page(direct):
    set_fetch_client(FakeClient(page=None))

    assert await webwatch._get("https://acme.test/pricing") == (200, "<html>direct</html>")
    assert direct == ["https://acme.test/pricing"]


async def test_a_second_fetch_within_the_ttl_is_served_from_the_cache(direct):
    client = FakeClient(page=PAGE)
    set_fetch_client(client)

    await webwatch._get("https://acme.test/pricing")
    again = await webwatch._get("https://acme.test/pricing")

    assert again == (200, "<html>Plans</html>")
    assert client.urls == ["https://acme.test/pricing"], "the page was fetched twice"


async def test_a_failed_page_is_not_cached(monkeypatch):
    set_fetch_client(FetchClient(base_url="", token=""))
    calls: list[str] = []

    async def failing(url):
        calls.append(url)
        return 0, ""

    monkeypatch.setattr(webwatch, "_direct_get", failing)
    await webwatch._get("https://acme.test/pricing")
    await webwatch._get("https://acme.test/pricing")

    assert len(calls) == 2, "a failed fetch was cached and replayed"


async def test_a_very_large_page_is_used_but_not_cached(monkeypatch):
    set_fetch_client(FetchClient(base_url="", token=""))
    monkeypatch.setattr(webwatch, "_MAX_CACHED_PAGE_CHARS", 10)
    calls: list[str] = []

    async def big(url):
        calls.append(url)
        return 200, "<html>" + "x" * 50 + "</html>"

    monkeypatch.setattr(webwatch, "_direct_get", big)
    status, body = await webwatch._get("https://acme.test/careers")
    await webwatch._get("https://acme.test/careers")

    assert status == 200 and len(body) > 10
    assert len(calls) == 2, "an oversized page was stored in the cache"


async def test_an_injected_fetcher_bypasses_the_service_and_the_cache(direct):
    # The seam existing webwatch tests use; they must keep seeing exactly what they inject.
    client = FakeClient(page=PAGE)
    set_fetch_client(client)
    seen: list[str] = []

    async def injected(url):
        seen.append(url)
        return 200, "<html>injected</html>"

    assert await webwatch._get("https://acme.test/pricing", injected) == (200, "<html>injected</html>")
    assert await webwatch._get("https://acme.test/pricing", injected) == (200, "<html>injected</html>")
    assert len(seen) == 2 and client.urls == [] and direct == []
