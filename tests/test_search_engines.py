# tests/test_search_engines.py
"""Hosted search engines (Exa / Brave / Serper): parsing, key-gating, routing.

Zero-network: response parsing is tested against captured sample payloads via the pure ``_parse``
helpers, and the key-gated ``search`` path short-circuits to ``[]`` before any HTTP call. No test
here makes a live request, so the suite stays offline and deterministic.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from nexus.integrations.search import (
    BraveSearchProvider,
    DuckDuckGoSearchProvider,
    ExaSearchProvider,
    SerperSearchProvider,
    StubSearchProvider,
    build_search_provider,
)
from nexus.integrations.search.engines import build_engine


class _FakeResp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeClient:
    """Async-context httpx stand-in that replays scripted responses (last one repeats)."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, *a, **k):
        r = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        return r


def _patch(monkeypatch, responses):
    from nexus.integrations.search import engines

    client = _FakeClient(responses)
    monkeypatch.setattr(engines.httpx, "AsyncClient", lambda *a, **k: client)

    async def _nosleep(*a, **k):
        return None

    monkeypatch.setattr(engines.asyncio, "sleep", _nosleep)
    return client


def test_exa_num_results_clamps_to_100():
    # Exa 400s on numResults > 100; the helper must clamp so a big pool never fails the whole call.
    from nexus.integrations.search.engines import _exa_num_results

    assert _exa_num_results(5) == 5
    assert _exa_num_results(100) == 100
    assert _exa_num_results(160) == 100  # the bug: 160 used to reach Exa and 400
    assert _exa_num_results(0) == 1


async def test_exa_request_clamps_numresults_on_the_wire(monkeypatch):
    """Over-eager limit (e.g. a 160-deep discovery pool) must go out as numResults=100, not 160."""
    captured: dict = {}

    class _CapturingClient(_FakeClient):
        async def post(self, *a, **k):
            captured.update(k.get("json") or {})
            return await super().post(*a, **k)

    from nexus.integrations.search import engines

    monkeypatch.setattr(engines.httpx, "AsyncClient",
                        lambda *a, **k: _CapturingClient([_FakeResp(200, {"results": []})]))
    await ExaSearchProvider("k").search_companies("b2b saas", limit=160, exclude_domains=["x.com"])
    assert captured["numResults"] == 100
    assert captured["category"] == "company"
    assert captured["excludeDomains"] == ["x.com"]


async def test_exa_retries_on_429_then_succeeds(monkeypatch):
    ok = {"results": [{"title": "Acme", "url": "https://acme.com", "text": "logistics"}]}
    client = _patch(monkeypatch, [_FakeResp(429), _FakeResp(200, ok)])
    hits = await ExaSearchProvider("k").search("logistics", limit=5)
    assert client.calls == 2  # one 429, then the retry succeeded
    assert len(hits) == 1 and hits[0].url == "https://acme.com"


async def test_exa_degrades_to_empty_after_persistent_429(monkeypatch):
    client = _patch(monkeypatch, [_FakeResp(429)])
    hits = await ExaSearchProvider("k").search("logistics", limit=5)
    assert hits == []
    assert client.calls == 3  # initial + 2 retries, then give up


class _KeyAwareClient:
    """Replays a response per x-api-key, recording which keys were tried (in order)."""

    def __init__(self, by_key):
        self.by_key = by_key
        self.used_keys: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, endpoint, json, headers):
        k = headers["x-api-key"]
        self.used_keys.append(k)
        return self.by_key.get(k, _FakeResp(429))


async def test_exa_pool_rotates_off_rate_limited_key(monkeypatch):
    from nexus.integrations.search import engines

    ok = {"results": [{"title": "Acme", "url": "https://acme.com", "text": "logistics"}]}
    client = _KeyAwareClient({"k1": _FakeResp(429), "k2": _FakeResp(200, ok)})
    monkeypatch.setattr(engines.httpx, "AsyncClient", lambda *a, **k: client)

    async def _nosleep(*a, **k):
        return None

    monkeypatch.setattr(engines.asyncio, "sleep", _nosleep)
    provider = ExaSearchProvider(api_keys=["k1", "k2"])
    hits = await provider.search("logistics", limit=5)
    assert len(hits) == 1 and hits[0].url == "https://acme.com"
    assert client.used_keys == ["k1", "k2"]  # tried k1 (429), rotated to k2 (200)
    assert provider.api_key == "k2"          # sticky on the working key


def test_build_engine_exa_uses_key_pool():
    s = _settings(exa_api_key="primary")
    s.exa_api_key_list = ["primary", "k2", "k3"]
    p = build_engine("exa", s)
    assert isinstance(p, ExaSearchProvider) and p.api_keys == ["primary", "k2", "k3"]


# --------------------------------------------------------------- parsing (pure)


def test_exa_parse_maps_results_and_text_snippet():
    data = {
        "results": [
            {"title": "Acme Inc", "url": "https://acme.com", "text": "Acme builds widgets."},
            {"title": "Beta", "url": "https://beta.io", "highlights": ["fast", "cheap"]},
        ]
    }
    hits = ExaSearchProvider("k")._parse(data, limit=10)
    assert [h.url for h in hits] == ["https://acme.com", "https://beta.io"]
    assert hits[0].snippet == "Acme builds widgets."
    assert hits[1].snippet == "fast cheap"          # falls back to highlights
    assert all(h.source == "exa" for h in hits)


def test_brave_parse_reads_web_results_and_strips_tags():
    data = {
        "web": {
            "results": [
                {"title": "<strong>Acme</strong> Inc", "url": "https://acme.com",
                 "description": "A <strong>widget</strong> maker"},
            ]
        }
    }
    hits = BraveSearchProvider("k")._parse(data, limit=10)
    assert hits[0].title == "Acme Inc"              # markup stripped
    assert hits[0].snippet == "A widget maker"
    assert hits[0].source == "brave"


def test_brave_parse_handles_missing_web_key():
    assert BraveSearchProvider("k")._parse({}, limit=5) == []


def test_serper_parse_maps_organic_link_to_url():
    data = {"organic": [{"title": "Acme", "link": "https://acme.com", "snippet": "widgets"}]}
    hits = SerperSearchProvider("k")._parse(data, limit=10)
    assert hits[0].url == "https://acme.com"
    assert hits[0].snippet == "widgets"
    assert hits[0].source == "serper"


def test_parse_respects_limit():
    data = {"results": [{"title": str(i), "url": f"https://{i}.com"} for i in range(10)]}
    assert len(ExaSearchProvider("k")._parse(data, limit=3)) == 3


# --------------------------------------------------------- key-gating (offline)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", [ExaSearchProvider(""), BraveSearchProvider(""),
                                      SerperSearchProvider("")])
async def test_missing_key_returns_empty_without_network(provider):
    # No key -> short-circuit to [] before any httpx call, so this never touches the network.
    assert await provider.search("anything", limit=5) == []


# ----------------------------------------------------------------- routing


def _settings(**keys):
    base = {"exa_api_key": "", "brave_api_key": "", "serper_api_key": ""}
    base.update(keys)
    return SimpleNamespace(**base)


def test_build_engine_returns_keyed_provider():
    p = build_engine("exa", _settings(exa_api_key="secret"))
    assert isinstance(p, ExaSearchProvider) and p.api_key == "secret"
    assert isinstance(build_engine("brave", _settings(brave_api_key="b")), BraveSearchProvider)
    assert isinstance(build_engine("serper", _settings(serper_api_key="s")), SerperSearchProvider)


def test_build_engine_falls_back_to_duckduckgo_without_key():
    # A selected engine with no key degrades to keyless DuckDuckGo, not a dark/empty provider.
    assert isinstance(build_engine("exa", _settings()), DuckDuckGoSearchProvider)


def test_build_engine_unknown_token_is_stub():
    assert isinstance(build_engine("nope", _settings()), StubSearchProvider)


def test_build_search_provider_routes_known_tokens():
    assert isinstance(build_search_provider("duckduckgo"), DuckDuckGoSearchProvider)
    assert isinstance(build_search_provider("stub"), StubSearchProvider)
    # exa with no configured key (test env) -> DuckDuckGo fallback.
    assert isinstance(build_search_provider("exa"), DuckDuckGoSearchProvider)


# ---- a condemned key must be rotated past, not retried ------------------------------------------
#
# Rotation used to fire on 429 alone. A revoked key (401), a forbidden one (403), or one whose
# credits ran out (402) fell through to `raise_for_status()`, was caught by the broad handler,
# retried with backoff against the SAME dead key, and finally returned `[]` — which reads as
# "no results". Because the index never advanced, one dead key at position 0 disabled the entire
# pool while every other key sat unused. `integrations/apify.py` already handled this; these did
# not.


@pytest.mark.parametrize("status", [401, 402, 403])
async def test_exa_rotates_past_a_condemned_key(monkeypatch, status):
    from nexus.integrations.search import engines

    ok = {"results": [{"title": "Acme", "url": "https://acme.com", "text": "logistics"}]}
    client = _KeyAwareClient({"dead": _FakeResp(status), "live": _FakeResp(200, ok)})
    monkeypatch.setattr(engines.httpx, "AsyncClient", lambda *a, **k: client)

    provider = ExaSearchProvider(api_keys=["dead", "live"])
    hits = await provider.search("logistics", limit=5)

    assert len(hits) == 1, f"a {status} on key 0 still empties the pool"
    # Tried the dead key exactly once — never retried — then moved on and stayed there.
    assert client.used_keys == ["dead", "live"]
    assert provider.api_key == "live"


async def test_exa_stops_once_every_key_is_condemned(monkeypatch):
    """Distinct from exhaustion-by-rate-limit: there is nothing to wait for, so do not burn the
    backoff budget pretending otherwise."""
    from nexus.integrations.search import engines

    client = _KeyAwareClient({"a": _FakeResp(401), "b": _FakeResp(401)})
    monkeypatch.setattr(engines.httpx, "AsyncClient", lambda *a, **k: client)

    async def _nosleep(*a, **k):
        return None

    monkeypatch.setattr(engines.asyncio, "sleep", _nosleep)
    provider = ExaSearchProvider(api_keys=["a", "b"])
    assert await provider.search("logistics", limit=5) == []
    # Each key tried once and only once — no retry against a key we know is dead.
    assert client.used_keys == ["a", "b"]


async def test_a_rate_limited_key_is_still_retried_not_condemned(monkeypatch):
    """The guard must not swallow the 429 path: 429 is temporary and the key stays in rotation."""
    from nexus.integrations.search import engines

    ok = {"results": [{"title": "Acme", "url": "https://acme.com", "text": "x"}]}
    client = _KeyAwareClient({"k1": _FakeResp(429), "k2": _FakeResp(200, ok)})
    monkeypatch.setattr(engines.httpx, "AsyncClient", lambda *a, **k: client)

    async def _nosleep(*a, **k):
        return None

    monkeypatch.setattr(engines.asyncio, "sleep", _nosleep)
    provider = ExaSearchProvider(api_keys=["k1", "k2"])
    assert len(await provider.search("x", limit=5)) == 1
