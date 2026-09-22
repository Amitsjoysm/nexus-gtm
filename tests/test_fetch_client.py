"""The client never raises across the boundary, and says WHY it could not answer.

`last_failure` is what stops a dead service from looking like a quiet market — the same contract
`SearchProvider` already defines.
"""
from __future__ import annotations

import httpx
import pytest

from nexus.fetching.client import FetchClient, get_fetch_client, set_fetch_client


def _client(handler) -> FetchClient:
    transport = httpx.MockTransport(handler)
    return FetchClient(base_url="http://fetch.test", token="t",
                       http=httpx.AsyncClient(transport=transport, base_url="http://fetch.test"))


async def test_search_returns_hits_and_sends_the_token():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["token"] = request.headers.get("X-Fetch-Token")
        return httpx.Response(200, json={"engine": "nexusfetch:ddg", "hits": [
            {"title": "Acme raises $40M", "url": "https://x.test/a", "snippet": "",
             "source": "nexusfetch:ddg"}
        ]})

    client = _client(handler)
    hits = await client.search("acme funding", limit=3)

    assert seen["token"] == "t"
    assert hits[0]["url"] == "https://x.test/a"
    assert client.last_failure == ""


async def test_a_blocked_engine_is_reported_not_raised():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "blocked"})

    client = _client(handler)
    assert await client.search("acme funding", limit=3) == []
    assert client.last_failure == "blocked"


async def test_an_unreachable_service_is_reported_not_raised():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    client = _client(handler)
    assert await client.search("acme funding", limit=3) == []
    assert "unreachable" in client.last_failure


async def test_a_garbled_body_is_reported_not_raised():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not json</html>")

    client = _client(handler)
    assert await client.search("acme funding", limit=3) == []
    assert client.last_failure


async def test_a_success_clears_the_previous_failure():
    responses = iter([httpx.Response(503, json={"detail": "blocked"}),
                      httpx.Response(200, json={"engine": "e", "hits": []})])
    client = _client(lambda request: next(responses))

    await client.search("q", limit=3)
    assert client.last_failure == "blocked"
    await client.search("q", limit=3)
    assert client.last_failure == ""


async def test_an_unconfigured_client_does_not_call_anything():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("an unconfigured client must not send a request")

    client = FetchClient(base_url="", token="",
                         http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await client.search("acme funding", limit=3) == []
    assert client.last_failure == "not configured"


async def test_fetch_page_returns_the_document():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": 200, "final_url": "https://acme.test/pricing",
                                         "html": "<html></html>", "text": "Pricing",
                                         "title": "Pricing"})

    page = await _client(handler).fetch_page("https://acme.test/pricing")
    assert page["text"] == "Pricing"


async def test_health_reads_the_service_payload():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(200, json={"ok": True, "browser": False})

    assert await _client(handler).health() == {"ok": True, "browser": False}


def test_the_shared_client_follows_the_settings(monkeypatch):
    from nexus.core.config import get_settings

    set_fetch_client(None)
    settings = get_settings()
    monkeypatch.setattr(settings, "fetch_service_url", "http://a.test:8081", raising=False)
    monkeypatch.setattr(settings, "fetch_service_token", "one", raising=False)
    first = get_fetch_client()
    assert first.configured and first.base_url == "http://a.test:8081"

    # A change made in the Control plane reaches the next call without a restart.
    monkeypatch.setattr(settings, "fetch_service_url", "http://b.test:8081", raising=False)
    assert get_fetch_client().base_url == "http://b.test:8081"
    set_fetch_client(None)


def test_the_token_can_never_be_set_from_the_control_plane():
    # The panel returns every value in plaintext, so a credential there leaks through a screenshot.
    from nexus.runtime_config.catalog import CATALOG, FORBIDDEN

    assert "fetch_service_token" in FORBIDDEN
    assert "fetch_service_token" not in CATALOG


@pytest.mark.parametrize("value", ["ftp://fetch.test", "http://", "http://metadata.google.internal:8081"])
def test_a_bad_fetch_service_url_is_refused_before_it_is_stored(value):
    from nexus.runtime_config.service import _VALIDATORS

    with pytest.raises(ValueError):
        _VALIDATORS["fetch_service_url"](value)


@pytest.mark.parametrize("value", ["http://169.254.169.254:8081", "http://10.0.0.5:8081",
                                   "http://127.0.0.1:8081"])
def test_a_private_fetch_service_url_is_refused_outside_a_local_stack(monkeypatch, value):
    # A local stack runs its services on private addresses, so `local`/`test` allow them; anywhere
    # else a private target from a web form is a port scanner. Metadata names are refused always.
    from nexus.core.config import get_settings
    from nexus.runtime_config.service import _VALIDATORS

    monkeypatch.setattr(get_settings(), "env", "production")
    with pytest.raises(ValueError):
        _VALIDATORS["fetch_service_url"](value)


def test_an_empty_fetch_service_url_is_allowed_because_it_means_off():
    from nexus.runtime_config.service import _VALIDATORS

    _VALIDATORS["fetch_service_url"]("")
