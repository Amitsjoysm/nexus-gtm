# tests/test_apify_client.py
"""The Apify seam: key rotation, inert-until-keyed, and no fake successes.

Apify is where the lookups with no compliant public API live (a phone behind a LinkedIn profile, a
Crunchbase page). The rules here are the ones the search-provider seam learned the expensive way:
an unkeyed integration must raise rather than return an empty list, a revoked key must not take the
whole pool down, and rotation state must be shared so a fresh caller does not restart on an already
rate-limited key.
"""
from __future__ import annotations

import httpx
import pytest

from nexus.integrations.apify import ACTORS, ApifyClient, ApifyError, ApifyNotConfigured


class _Transport:
    """Scripted responses, recording the token used for each call."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.tokens: list[str] = []
        self.urls: list[str] = []
        self.bodies: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.tokens.append(request.url.params.get("token", ""))
        self.urls.append(str(request.url.path))
        import json as _json

        self.bodies.append(_json.loads(request.content or b"{}"))
        status, payload = self.responses.pop(0) if self.responses else (200, [])
        return httpx.Response(status, json=payload)


def _patch(monkeypatch, transport: _Transport):
    """Route the client's httpx through a scripted transport."""
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(transport)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


async def test_an_unkeyed_client_raises_rather_than_returning_nothing():
    """An empty list and a missing key look identical to a caller. This codebase has already
    shipped one source that silently found nothing forever."""
    with pytest.raises(ApifyNotConfigured):
        await ApifyClient([]).run_actor("phone_finder", {})


async def test_a_logical_actor_name_resolves_to_its_id(monkeypatch):
    t = _Transport((200, [{"phone": "+14155552671"}]))
    _patch(monkeypatch, t)
    items = await ApifyClient(["k1"]).run_actor("phone_finder", {"linkedin_url": ["u"]})

    assert items == [{"phone": "+14155552671"}]
    assert ACTORS["phone_finder"] in t.urls[0]
    assert t.bodies[0] == {"linkedin_url": ["u"]}


async def test_a_raw_actor_id_is_passed_through(monkeypatch):
    """So trying a new actor does not require editing the registry first."""
    t = _Transport((200, []))
    _patch(monkeypatch, t)
    await ApifyClient(["k1"]).run_actor("someRawActorId", {})
    assert "someRawActorId" in t.urls[0]


async def test_a_rate_limited_key_rotates_to_the_next(monkeypatch):
    t = _Transport((429, {}), (200, [{"ok": True}]))
    _patch(monkeypatch, t)
    client = ApifyClient(["k1", "k2"])
    items = await client.run_actor("phone_finder", {})

    assert items == [{"ok": True}]
    assert t.tokens == ["k1", "k2"], "the second attempt must use the second key"


async def test_rotation_is_sticky_across_calls(monkeypatch):
    """A fresh client per call would restart at key 0 and hammer an already-limited key, which is
    why the module keeps one process-wide instance."""
    t = _Transport((429, {}), (200, [{"a": 1}]), (200, [{"b": 2}]))
    _patch(monkeypatch, t)
    client = ApifyClient(["k1", "k2"])
    await client.run_actor("phone_finder", {})
    await client.run_actor("phone_finder", {})

    assert t.tokens == ["k1", "k2", "k2"], "must stay on the working key, not reset to k1"


async def test_a_revoked_key_is_skipped_not_retried(monkeypatch):
    """One dead key in a pool must not take the integration down, and must not be retried either."""
    t = _Transport((401, {}), (200, [{"ok": True}]))
    _patch(monkeypatch, t)
    client = ApifyClient(["dead", "live"])
    assert await client.run_actor("phone_finder", {}) == [{"ok": True}]
    assert t.tokens == ["dead", "live"]


async def test_a_single_revoked_key_raises(monkeypatch):
    """With nothing to rotate to, this is a configuration error and must surface as one."""
    t = _Transport((401, {}))
    _patch(monkeypatch, t)
    with pytest.raises(ApifyError):
        await ApifyClient(["dead"]).run_actor("phone_finder", {})


async def test_a_failing_actor_raises_rather_than_reporting_no_results(monkeypatch):
    t = _Transport((500, {}), (500, {}), (500, {}), (500, {}), (500, {}))
    _patch(monkeypatch, t)
    with pytest.raises(ApifyError):
        await ApifyClient(["k1"]).run_actor("phone_finder", {})


async def test_non_dict_dataset_rows_are_dropped(monkeypatch):
    """Actors are third-party code; a stray string in the dataset must not crash the caller."""
    t = _Transport((200, [{"good": 1}, "junk", None, {"also_good": 2}]))
    _patch(monkeypatch, t)
    items = await ApifyClient(["k1"]).run_actor("phone_finder", {})
    assert items == [{"good": 1}, {"also_good": 2}]


async def test_the_key_pool_reads_primary_then_rotation_list():
    from nexus.core.config import get_settings

    s = get_settings()
    object.__setattr__(s, "apify_api_key", "primary")
    object.__setattr__(s, "apify_api_keys", "primary, second ,third")
    try:
        assert s.apify_api_key_list == ["primary", "second", "third"], "deduped, primary first"
    finally:
        object.__setattr__(s, "apify_api_key", "")
        object.__setattr__(s, "apify_api_keys", "")
