# tests/test_provider_key_testing.py
"""Testing a provider key, at two depths.

A cheap auth probe answers "does this credential authenticate". A full round-trip answers "does the
thing we actually do with it work". Measured 2026-08-21, those separated cleanly:

    GET  /models            -> 200 for all five Groq keys   (the credential is fine)
    POST /chat/completions  -> 404 for all five Groq keys   (the model had been withdrawn)

A panel showing five green ticks while every draft came from the stub would be worse than no panel.
So `probe_ok` and `verified` are distinct states and `verify` is a real call, opt-in because it
costs credits.
"""
from __future__ import annotations

import httpx
import pytest


async def test_a_probe_reports_ok_on_200():
    from nexus.providers.testing import probe

    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"data": []}))
    result = await probe("groq", "sk-good", transport=transport)
    assert result.ok is True
    assert result.status == "probe_ok"


async def test_a_probe_carries_the_providers_own_error_text():
    """"revoked" and "model withdrawn" arrive behind similar statuses and need opposite fixes, so
    the provider's own words are kept rather than a status code alone."""
    from nexus.providers.testing import probe

    transport = httpx.MockTransport(
        lambda r: httpx.Response(401, json={"error": {"message": "Invalid API Key"}})
    )
    result = await probe("groq", "sk-dead", transport=transport)
    assert result.ok is False
    assert result.status == "failed"
    assert result.http_status == 401
    assert "Invalid API Key" in result.detail


async def test_a_key_that_probes_ok_but_fails_verify_is_not_marked_verified():
    """The Groq shape, exactly: auth fine, real call broken. This is the whole reason for two
    depths — a single test would have reported this key healthy."""
    from nexus.providers.testing import probe, verify

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "m"}]})
        return httpx.Response(404, json={
            "error": {"message": "The model `x` does not exist or you do not have access to it."}
        })

    transport = httpx.MockTransport(handler)
    assert (await probe("groq", "sk-ok", transport=transport)).status == "probe_ok"

    deep = await verify("groq", "sk-ok", transport=transport)
    assert deep.ok is False
    assert deep.status == "failed"
    assert "does not exist" in deep.detail


async def test_verify_succeeds_on_a_real_completion():
    from nexus.providers.testing import verify

    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={
        "choices": [{"message": {"content": "ok"}}], "usage": {"total_tokens": 3},
    }))
    result = await verify("groq", "sk-ok", transport=transport)
    assert result.ok is True and result.status == "verified"


async def test_an_unknown_provider_is_refused_not_silently_ok():
    from nexus.providers.testing import probe

    result = await probe("nope", "k")
    assert result.ok is False and "unknown provider" in result.detail.lower()


async def test_a_network_failure_does_not_condemn_the_key():
    """Unreachable is not evidence against the credential. Reporting it as a bad key sends an
    operator to rotate something that was never wrong — the mistake the Apify client used to make
    by reporting an approval problem as a rate limit."""
    from nexus.providers.testing import probe

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns failure")

    result = await probe("exa", "sk-x", transport=httpx.MockTransport(boom))
    assert result.ok is False
    assert "could not reach" in result.detail.lower()
    assert result.http_status is None, "a transport failure has no HTTP status to report"


@pytest.mark.parametrize("provider", ["exa", "firecrawl", "brave", "serper", "github", "apify"])
async def test_every_provider_has_a_working_probe(provider):
    """A provider whose probe was never written would silently report failure forever."""
    from nexus.providers.testing import probe

    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True}))
    result = await probe(provider, "some-key", transport=transport)
    assert result.ok is True, f"{provider} has no working probe"


@pytest.mark.parametrize("provider", ["exa", "firecrawl", "brave", "serper"])
async def test_a_search_key_can_actually_reach_verified(provider):
    """For the search providers the probe IS the real call, so verify upgrades the status.

    Returning `probe_ok` here would leave every search key permanently amber with no way to clear
    it — the badge would stop meaning "auth works, real calls untested" and start meaning "this
    provider cannot be verified", which is a different fact.
    """
    from nexus.providers.testing import verify

    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"results": []}))
    result = await verify(provider, "k", transport=transport)
    assert result.status == "verified", f"{provider} can never reach verified"


async def test_a_failing_search_key_still_reports_failed_not_verified():
    from nexus.providers.testing import verify

    transport = httpx.MockTransport(
        lambda r: httpx.Response(401, json={"error": "bad key"})
    )
    result = await verify("exa", "k", transport=transport)
    assert result.ok is False and result.status == "failed"
