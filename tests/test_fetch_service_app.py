"""The service refuses what it must, and never returns a fabricated answer.

Scrapling is not installed in the test environment, so the fetchers are injected. That is
deliberate: the offline suite must never reach the network.
"""
from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from nexus.fetching.parse import BlockedByEngine
from nexus.fetching.service import build_app

TOKEN = "test-token"
AUTH = {"X-Fetch-Token": TOKEN}


def _client(search=None, fetch=None) -> AsyncClient:
    app = build_app(token=TOKEN, search_fn=search, fetch_fn=fetch)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://fetch.test")


async def test_a_call_without_the_token_is_refused():
    async with _client() as client:
        response = await client.post("/search", json={"query": "acme funding", "limit": 3})
    assert response.status_code == 401


async def test_a_wrong_token_is_refused():
    async with _client() as client:
        response = await client.post("/search", json={"query": "acme funding"},
                                     headers={"X-Fetch-Token": "guess"})
    assert response.status_code == 401


async def test_a_service_with_no_token_configured_refuses_everything():
    # An empty secret must not mean "open": a misconfigured deployment fails closed.
    app = build_app(token="", search_fn=None, fetch_fn=None)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://fetch.test") as client:
        response = await client.post("/search", json={"query": "acme"}, headers={"X-Fetch-Token": ""})
    assert response.status_code == 401


async def test_a_private_target_is_refused_even_with_the_token():
    async def never(url, *, mode, timeout_s):
        raise AssertionError("a refused URL must never reach the fetcher")

    async with _client(fetch=never) as client:
        response = await client.post(
            "/fetch", json={"url": "http://169.254.169.254/latest/meta-data/"}, headers=AUTH,
        )
    assert response.status_code == 400
    assert "not fetchable" in response.json()["detail"]


async def test_search_returns_hits_and_names_the_engine():
    async def search(query, *, limit, recency_days):
        return [{"title": "Acme raises $40M", "url": "https://x.test/a", "snippet": "",
                 "source": "nexusfetch:ddg"}]

    async with _client(search=search) as client:
        response = await client.post("/search", json={"query": "acme funding", "limit": 3},
                                     headers=AUTH)
    body = response.json()
    assert response.status_code == 200
    assert body["engine"] == "nexusfetch:ddg"
    assert body["hits"][0]["url"] == "https://x.test/a"


async def test_a_blocked_engine_answers_503_not_an_empty_list():
    async def search(query, *, limit, recency_days):
        raise BlockedByEngine("duckduckgo served an anti-bot page")

    async with _client(search=search) as client:
        response = await client.post("/search", json={"query": "acme funding"}, headers=AUTH)
    assert response.status_code == 503
    assert response.json()["detail"] == "blocked"


async def test_a_crashing_fetcher_is_a_502_not_a_500_with_a_traceback():
    async def fetch(url, *, mode, timeout_s):
        raise RuntimeError("browser died")

    async with _client(fetch=fetch) as client:
        response = await client.post("/fetch", json={"url": "https://93.184.216.34/pricing"},
                                     headers=AUTH)
    assert response.status_code == 502
    assert "browser died" not in response.text


async def test_health_answers_the_token_holder():
    async with _client() as client:
        response = await client.get("/health", headers=AUTH)
    assert response.status_code == 200
    assert response.json()["ok"] is True


async def test_health_tells_a_stranger_nothing():
    async with _client() as client:
        assert (await client.get("/health")).status_code == 401


async def test_an_oversized_page_is_capped_and_says_so():
    async def fetch(url, *, mode, timeout_s):
        return {"status": 200, "final_url": url, "html": "x" * 50, "text": "y" * 50, "title": ""}

    app = build_app(token=TOKEN, fetch_fn=fetch, max_chars=10)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://fetch.test") as client:
        body = (await client.post("/fetch", json={"url": "https://93.184.216.34/p"},
                                  headers=AUTH)).json()
    assert len(body["html"]) == 10 and len(body["text"]) == 10
    assert body["truncated"] is True


async def test_a_redirect_to_a_private_address_returns_nothing_from_it():
    # A browser follows redirects the guard never saw. The content of wherever it landed must not
    # come back if that place is one the guard would have refused.
    async def fetch(url, *, mode, timeout_s):
        return {"status": 200, "final_url": "http://169.254.169.254/latest/meta-data/iam",
                "html": "SECRET-ROLE-CREDENTIALS", "text": "", "title": ""}

    async with _client(fetch=fetch) as client:
        response = await client.post("/fetch", json={"url": "https://93.184.216.34/p",
                                                     "mode": "stealth"}, headers=AUTH)
    assert response.status_code == 400
    assert "SECRET" not in response.text


async def test_the_api_documentation_is_not_published():
    # An interactive schema on an internal fetcher is a map for whoever reaches it.
    async with _client() as client:
        assert (await client.get("/docs")).status_code == 404
        assert (await client.get("/openapi.json")).status_code == 404
