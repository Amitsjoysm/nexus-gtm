# nexus/providers/testing.py
"""Testing a provider key at two depths.

:func:`probe` is the cheapest call that proves the credential authenticates. It runs on save and on
"test all", and costs nothing meaningful.

:func:`verify` makes a real request of the kind the product actually issues. It costs credits, so
it is opt-in per key and never swept.

The second exists because the first is not sufficient, and that is not a hypothetical. On
2026-08-21 every configured Groq key returned 200 from ``GET /models`` and 404 from every chat
completion, because the configured model had been withdrawn. A panel with one test depth would
have shown five healthy keys while the stub wrote every outbound email.

A transport failure never condemns a key: "unreachable" is not evidence about a credential, and
reporting it as a bad key sends an operator to rotate something that was never wrong.
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx

_TIMEOUT = 20.0


@dataclass(slots=True)
class TestResult:
    ok: bool
    status: str                      # probe_ok | verified | failed
    detail: str = ""
    http_status: int | None = None   # None for a transport failure — there was no response


def _detail(resp: httpx.Response) -> str:
    """The provider's own words.

    "Invalid API Key" and "the model does not exist" need opposite fixes, and the status code alone
    does not distinguish them — a 404 could be either a withdrawn model or a wrong base URL.
    """
    try:
        body = resp.json()
    except Exception:
        return (resp.text or "")[:300]
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        return str(err.get("message") or err)[:300]
    if isinstance(err, str):
        return err[:300]
    return str(body)[:300]


async def _call(method: str, url: str, *, headers: dict, json_body: dict | None = None,
                transport=None) -> httpx.Response:
    async with httpx.AsyncClient(timeout=_TIMEOUT, transport=transport) as client:
        return await client.request(method, url, headers=headers, json=json_body)


async def _resolved_model(provider: str) -> str:
    """The model the product would actually send, falling back to the environment default.

    Kept local so `testing` does not hard-depend on the resolver: if the settings table is
    unreadable, verifying against the environment value is still a useful test.
    """
    from nexus.core.config import get_settings

    s = get_settings()
    env = {"groq": s.groq_model, "openai_compat": s.llm_model,
           "anthropic": s.anthropic_model}.get(provider, "")
    try:
        from nexus.providers.resolver import model_for

        return await model_for(provider) or env
    except Exception:
        return env


def _unreachable(exc: Exception) -> TestResult:
    return TestResult(False, "failed", f"could not reach the provider: {exc!r}"[:300], None)


async def probe(provider: str, key: str, *, transport=None) -> TestResult:
    """Cheapest call that proves this credential authenticates."""
    from nexus.core.config import get_settings
    from nexus.providers.catalog import PROVIDERS

    if provider not in PROVIDERS:
        return TestResult(False, "failed", f"unknown provider {provider!r}")
    s = get_settings()
    try:
        if provider == "groq":
            resp = await _call("GET", f"{s.groq_base_url}/models",
                               headers={"Authorization": f"Bearer {key}"}, transport=transport)
        elif provider == "anthropic":
            resp = await _call("GET", "https://api.anthropic.com/v1/models",
                               headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                               transport=transport)
        elif provider == "openai_compat":
            resp = await _call("GET", f"{s.llm_base_url}/models",
                               headers={"Authorization": f"Bearer {key}"}, transport=transport)
        elif provider == "exa":
            resp = await _call("POST", "https://api.exa.ai/search",
                               headers={"x-api-key": key},
                               json_body={"query": "test", "numResults": 1}, transport=transport)
        elif provider == "firecrawl":
            resp = await _call("POST", "https://api.firecrawl.dev/v1/search",
                               headers={"Authorization": f"Bearer {key}"},
                               json_body={"query": "test", "limit": 1}, transport=transport)
        elif provider == "brave":
            resp = await _call("GET",
                               "https://api.search.brave.com/res/v1/web/search?q=test&count=1",
                               headers={"X-Subscription-Token": key}, transport=transport)
        elif provider == "serper":
            resp = await _call("POST", "https://google.serper.dev/search",
                               headers={"X-API-KEY": key},
                               json_body={"q": "test", "num": 1}, transport=transport)
        elif provider == "apify":
            resp = await _call("GET", f"https://api.apify.com/v2/users/me?token={key}",
                               headers={}, transport=transport)
        else:  # github
            resp = await _call("GET", "https://api.github.com/rate_limit",
                               headers={"Authorization": f"Bearer {key}"}, transport=transport)
    except Exception as exc:
        return _unreachable(exc)

    if resp.status_code == 200:
        return TestResult(True, "probe_ok", "authenticated", 200)
    return TestResult(False, "failed", _detail(resp), resp.status_code)


async def verify(provider: str, key: str, *, transport=None) -> TestResult:
    """A real request of the kind the product makes. Costs credits, so never automatic."""
    from nexus.core.config import get_settings
    from nexus.providers.catalog import PROVIDERS

    if provider not in PROVIDERS:
        return TestResult(False, "failed", f"unknown provider {provider!r}")
    s = get_settings()
    try:
        if provider in ("groq", "openai_compat"):
            base = s.groq_base_url if provider == "groq" else s.llm_base_url
            # The RESOLVED model, not the environment one. Verifying against a model the app is
            # not going to use would report a green key while real calls fail — precisely the
            # failure this whole feature exists to surface.
            model = await _resolved_model(provider)
            resp = await _call(
                "POST", f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json_body={"model": model,
                           "messages": [{"role": "user", "content": "Reply with OK"}],
                           "max_tokens": 5},
                transport=transport,
            )
        elif provider == "anthropic":
            resp = await _call(
                "POST", "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                json_body={"model": await _resolved_model(provider), "max_tokens": 5,
                           "messages": [{"role": "user", "content": "Reply with OK"}]},
                transport=transport,
            )
        elif provider == "apify":
            # Listing actors exercises the token against a real, authorised resource without
            # starting a billed actor run.
            resp = await _call("GET", f"https://api.apify.com/v2/acts?token={key}&limit=1",
                               headers={}, transport=transport)
        else:
            # For the search providers the probe already issues a real query against the real
            # endpoint, so there is no deeper call to make. Inventing a second request that proves
            # less would not be honest.
            #
            # But the RESULT is upgraded to `verified`, because for these providers the probe *is*
            # the real call. Returning `probe_ok` would leave every search key permanently amber in
            # the UI with no way to clear it — the status would stop meaning "auth works, real
            # calls untested" and start meaning "this provider does not support verification",
            # which is a different fact wearing the same badge.
            result = await probe(provider, key, transport=transport)
            if result.ok:
                return TestResult(True, "verified", "a real query succeeded", result.http_status)
            return result
    except Exception as exc:
        return _unreachable(exc)

    if resp.status_code == 200:
        return TestResult(True, "verified", "a real call succeeded", 200)
    return TestResult(False, "failed", _detail(resp), resp.status_code)


async def list_models(provider: str, key: str, *, transport=None) -> tuple[list[str], str]:
    """The models this provider currently offers for this key.

    Returns ``(models, detail)``. An unreachable provider or one with no model concept gives an
    empty list and a reason — "we could not ask" and "there are none" are different facts and a
    bare ``[]`` conflates them, which is the mistake this whole subsystem keeps correcting.
    """
    from nexus.core.config import get_settings

    s = get_settings()
    urls = {
        "groq": (f"{s.groq_base_url}/models", {"Authorization": f"Bearer {key}"}),
        "openai_compat": (f"{s.llm_base_url}/models", {"Authorization": f"Bearer {key}"}),
        "anthropic": ("https://api.anthropic.com/v1/models",
                      {"x-api-key": key, "anthropic-version": "2023-06-01"}),
    }
    if provider not in urls:
        return [], "this provider has no model to choose"
    url, headers = urls[provider]
    try:
        resp = await _call("GET", url, headers=headers, transport=transport)
    except Exception as exc:
        return [], f"could not reach the provider: {exc!r}"[:200]
    if resp.status_code != 200:
        return [], _detail(resp)
    try:
        data = resp.json().get("data") or []
        return sorted(str(m.get("id")) for m in data if m.get("id")), ""
    except Exception:
        return [], "the provider's model list could not be parsed"
