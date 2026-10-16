"""The InfoJoy Search client: total, rotating, and honest about why it has nothing.

It never raises. Every way a call can go wrong is a status the router turns into one sentence:
not configured (no key), invalid (the provider refused the request, with its own words), rejected
(every key refused), busy (rate limited), unavailable (timeout, transport, 5xx), bad response (a 200
it could not read). "Not configured" and "nothing matched" must never look the same.
"""
from __future__ import annotations

import json

import httpx
import pytest

from tests.prospector_fixtures import CONTACTS, FakeVendor, page


def _client(handler, keys=("k1",), **kw):
    from nexus.prospector.client import InfoJoyClient

    transport = handler if isinstance(handler, httpx.MockTransport) else httpx.MockTransport(handler)
    return InfoJoyClient(keys=list(keys), transport=transport, backoff_s=0, **kw)


async def test_the_body_is_exactly_the_documented_shape():
    vendor = FakeVendor()
    out = await _client(vendor.transport()).search(
        "contacts", {"Titles": ["VP Sales"]}, page=2, size=3, count=True)

    sent = vendor.requests[0]
    assert sent["path"] == "/api/search/contacts" and sent["token"] == "k1"
    assert sent["body"] == {"PageNumber": 2, "PageSize": 3, "IncludeTotalCount": True,
                            "Filters": {"Titles": ["VP Sales"]}}
    assert out.ok and out.page.total == len(CONTACTS) and out.vendor_rows == 1


async def test_companies_go_to_their_own_endpoint():
    vendor = FakeVendor()
    await _client(vendor.transport()).search("companies", {}, page=1, size=25)
    assert vendor.requests[0]["path"] == "/api/search/companies"


async def test_a_page_over_500_is_refused_without_sending():
    vendor = FakeVendor()
    out = await _client(vendor.transport()).search("contacts", {}, page=1, size=501)
    assert out.status == "invalid" and "500" in out.detail and vendor.requests == []


async def test_no_key_is_not_configured_and_sends_nothing():
    vendor = FakeVendor()
    out = await _client(vendor.transport(), keys=()).search("contacts", {}, page=1, size=25)
    assert out.status == "not_configured" and vendor.requests == []


async def test_without_explicit_keys_the_managed_pool_is_used(monkeypatch):
    from nexus.prospector.client import InfoJoyClient

    async def pool(provider):
        assert provider == "infojoy_search"
        return ["from-pool"]

    monkeypatch.setattr("nexus.providers.resolver.key_pool", pool)
    vendor = FakeVendor()
    out = await InfoJoyClient(transport=vendor.transport(), backoff_s=0).search(
        "contacts", {}, page=1, size=2)
    assert out.ok and vendor.requests[0]["token"] == "from-pool"


async def test_a_rejected_key_rotates_and_is_recorded(monkeypatch):
    recorded = []

    async def record(provider, key, resp):
        recorded.append((provider, key, resp.status_code))

    monkeypatch.setattr("nexus.providers.service.record_rejection_from_response", record)

    def handler(request):
        if request.headers["X-Api-Token"] == "dead":
            return httpx.Response(401, json={"error": "invalid token"})
        return httpx.Response(200, json=page(CONTACTS[:1]))

    out = await _client(handler, keys=("dead", "live")).search("contacts", {}, page=1, size=1)

    assert out.ok and recorded == [("infojoy_search", "dead", 401)]


async def test_every_key_rejected_is_rejected(monkeypatch):
    async def record(provider, key, resp):
        return None

    monkeypatch.setattr("nexus.providers.service.record_rejection_from_response", record)
    out = await _client(lambda r: httpx.Response(403, json={}), keys=("a", "b")).search(
        "contacts", {}, page=1, size=1)
    assert out.status == "rejected"


async def test_rate_limited_everywhere_is_busy():
    calls = []

    def handler(request):
        calls.append(request.headers["X-Api-Token"])
        return httpx.Response(429, json={})

    out = await _client(handler, keys=("a", "b")).search("contacts", {}, page=1, size=1)
    assert out.status == "busy" and set(calls) == {"a", "b"}


async def test_the_providers_validation_sentence_is_kept():
    body = {"Errors": ["PageSize cannot be greater than 500."]}
    out = await _client(lambda r: httpx.Response(400, json=body)).search(
        "contacts", {}, page=1, size=10)
    assert out.status == "invalid" and out.detail == "PageSize cannot be greater than 500."


async def test_a_timeout_is_unavailable_not_an_exception():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    out = await _client(handler).search("contacts", {}, page=1, size=10)
    assert out.status == "unavailable" and "did not answer" in out.detail


async def test_a_server_error_is_unavailable():
    out = await _client(lambda r: httpx.Response(502, text="bad gateway")).search(
        "contacts", {}, page=1, size=10)
    assert out.status == "unavailable"


@pytest.mark.parametrize("body", [[1, 2], {"Items": "nope"}, "not json"])
async def test_an_unreadable_200_is_a_bad_response(body):
    def handler(request):
        if isinstance(body, str):
            return httpx.Response(200, text=body)
        return httpx.Response(200, json=body)

    out = await _client(handler).search("contacts", {}, page=1, size=10)
    assert out.status == "bad_response" and out.page is None


async def test_the_key_never_appears_in_a_detail():
    def handler(request):
        return httpx.Response(400, json={"Errors": [f"bad token {request.headers['X-Api-Token']}"]})

    out = await _client(handler, keys=("sekret-token-9",)).search("contacts", {}, page=1, size=1)
    assert "sekret-token-9" not in out.detail


async def test_an_unknown_view_is_invalid():
    out = await _client(FakeVendor().transport()).search("people", {}, page=1, size=1)
    assert out.status == "invalid"


async def test_the_token_is_never_in_the_url():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json=page([]))

    await _client(handler, keys=("tok-xyz",)).search("contacts", {"Titles": ["a"]}, page=1, size=1)
    assert "tok-xyz" not in seen[0]
    assert json.loads(json.dumps({"ok": True}))
