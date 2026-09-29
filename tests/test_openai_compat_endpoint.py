"""An OpenAI-compatible LLM configured entirely from the Superadmin panel: endpoint, key and model.

Reported 2026-09-29: the panel took a key and a model for "OpenAI-compatible" but offered nowhere to
say WHICH endpoint, and the base URL could only be set with ``NEXUS_LLM_BASE_URL`` and a redeploy.
Worse, measured while fixing it: nothing read the panel for this provider at all. The chain built
``OpenAICompatProvider`` only when the environment held a key, from the environment's model and URL,
so a key and model saved in the panel changed nothing. These pin both halves.
"""
from __future__ import annotations

import json

import httpx
import pytest

from nexus.core.config import get_settings
from tests.conftest import auth, signup


async def _superadmin(client, monkeypatch, *, slug: str, email: str) -> str:
    monkeypatch.setattr(get_settings(), "platform_admin_emails", email)
    return await signup(client, slug=slug, email=email, company=slug.upper())


@pytest.fixture(autouse=True)
def _fresh_caches():
    from nexus.providers import resolver

    resolver.invalidate()
    resolver.invalidate_models()
    yield
    resolver.invalidate()
    resolver.invalidate_models()


# ---- the endpoint is chosen in the panel ----------------------------------------------------------

async def test_the_endpoint_can_be_set_shown_and_cleared(client, monkeypatch):
    token = await _superadmin(client, monkeypatch, slug="oc1", email="boss@oc1.com")
    monkeypatch.setattr(get_settings(), "llm_base_url", "https://api.openai.com/v1")

    before = (await client.get("/api/admin/provider-keys/openai_compat/models",
                               headers=auth(token))).json()
    assert before["base_url"] == "https://api.openai.com/v1"
    assert before["base_url_overridden"] is False and before["base_url_editable"] is True

    r = await client.put("/api/admin/provider-keys/openai_compat/base-url", headers=auth(token),
                         json={"base_url": "https://openrouter.ai/api/v1/"})
    assert r.status_code == 200, r.text
    # Stored without the trailing slash, because every path is appended to it.
    assert r.json()["base_url"] == "https://openrouter.ai/api/v1"
    after = (await client.get("/api/admin/provider-keys/openai_compat/models",
                              headers=auth(token))).json()
    assert after["base_url"] == "https://openrouter.ai/api/v1" and after["base_url_overridden"]

    cleared = await client.put("/api/admin/provider-keys/openai_compat/base-url",
                               headers=auth(token), json={"base_url": ""})
    assert cleared.status_code == 200
    back = (await client.get("/api/admin/provider-keys/openai_compat/models",
                             headers=auth(token))).json()
    assert back["base_url"] == "https://api.openai.com/v1" and not back["base_url_overridden"]


@pytest.mark.parametrize("bad, words", [
    ("ftp://llm.example.com/v1", "http"),
    ("https:///v1", "no host"),
    ("http://169.254.169.254/latest", "refusing"),
    ("https://openrouter.ai/api/v1/chat/completions", "base URL"),
])
async def test_an_unusable_endpoint_is_refused_before_it_is_saved(client, monkeypatch, bad, words):
    """The panel's Test and model list call this URL and report how it answered, which is a port
    scanner if it can point anywhere. Same guard as the email verifier URL."""
    token = await _superadmin(client, monkeypatch, slug="oc2", email="boss@oc2.com")
    monkeypatch.setattr(get_settings(), "env", "production")
    r = await client.put("/api/admin/provider-keys/openai_compat/base-url", headers=auth(token),
                         json={"base_url": bad})
    assert r.status_code == 422, r.text
    assert words in r.json()["detail"]


async def test_only_openai_compatible_takes_an_endpoint(client, monkeypatch):
    token = await _superadmin(client, monkeypatch, slug="oc3", email="boss@oc3.com")
    r = await client.put("/api/admin/provider-keys/groq/base-url", headers=auth(token),
                         json={"base_url": "https://example.com/v1"})
    assert r.status_code == 400
    groq = (await client.get("/api/admin/provider-keys/groq/models", headers=auth(token))).json()
    assert groq["base_url_editable"] is False


async def test_a_tenant_owner_cannot_set_it(client):
    token = await signup(client, slug="oc4", email="o@oc4.com", company="OC4")
    r = await client.put("/api/admin/provider-keys/openai_compat/base-url", headers=auth(token),
                         json={"base_url": "https://example.com/v1"})
    assert r.status_code == 404


# ---- and what the panel says is what a completion uses -------------------------------------------

async def test_a_completion_uses_the_panels_endpoint_key_and_model(monkeypatch):
    """No environment key at all: everything comes from the panel, with no restart."""
    from nexus.agents.llm import LLMMessage, ManagedOpenAICompatProvider
    from nexus.providers import resolver, service

    monkeypatch.setattr(get_settings(), "llm_api_key", "")
    monkeypatch.setattr(get_settings(), "llm_model", "env-model")
    monkeypatch.setattr(get_settings(), "llm_base_url", "https://api.openai.com/v1")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}],
                                         "usage": {"total_tokens": 7}})

    provider = ManagedOpenAICompatProvider(transport=httpx.MockTransport(handler))
    await service.add_key("openai_compat", "router", "sk-panel-1234")
    await service.set_model("openai_compat", "meta-llama/llama-3.1-70b")
    await service.set_base_url("openai_compat", "https://openrouter.ai/api/v1")
    resolver.invalidate()

    out = await provider.complete([LLMMessage(role="user", content="hello")])
    assert out.text == "hi"
    request = seen[-1]
    assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer sk-panel-1234"
    assert json.loads(request.content)["model"] == "meta-llama/llama-3.1-70b"


async def test_with_no_key_anywhere_it_fails_fast_so_the_chain_moves_on(monkeypatch):
    from nexus.agents.llm import LLMMessage, ManagedOpenAICompatProvider

    monkeypatch.setattr(get_settings(), "llm_api_key", "")
    calls: list[httpx.Request] = []
    provider = ManagedOpenAICompatProvider(
        transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(500)))
    with pytest.raises(RuntimeError, match="no OpenAI-compatible key"):
        await provider.complete([LLMMessage(role="user", content="hello")])
    assert calls == []  # nothing was sent anywhere


def test_choosing_openai_compatible_builds_it_without_an_environment_key(monkeypatch):
    """It used to fall back to the stub unless NEXUS_LLM_API_KEY was set, so a deployment keyed
    only through the panel wrote every draft with the stub."""
    from nexus.agents import llm

    monkeypatch.setattr(get_settings(), "llm_provider", "openai_compat")
    monkeypatch.setattr(get_settings(), "llm_api_key", "")
    llm.set_llm_provider(None)
    try:
        built = llm.get_llm_provider()
        inner = getattr(built, "inner", built)
        assert isinstance(inner, llm.FallbackLLMProvider)
        assert isinstance(inner.providers[0], llm.ManagedOpenAICompatProvider)
        assert isinstance(inner.providers[-1], llm.StubLLMProvider)
    finally:
        llm.set_llm_provider(None)


def test_auto_includes_it_after_groq(monkeypatch):
    from nexus.agents import llm

    s = get_settings()
    monkeypatch.setattr(s, "llm_provider", "auto")
    monkeypatch.setattr(s, "llm_api_key", "")
    monkeypatch.setattr(s, "anthropic_api_key", "")
    chain = llm._build_llm_chain(s)
    kinds = [type(p) for p in chain.providers]
    assert kinds.index(llm.GroqLLMProvider) < kinds.index(llm.ManagedOpenAICompatProvider)
    assert kinds[-1] is llm.StubLLMProvider


async def test_the_models_list_and_the_probe_ask_the_chosen_endpoint(monkeypatch):
    from nexus.providers import service, testing

    await service.set_base_url("openai_compat", "https://llm.example.com/v1")
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"data": [{"id": "m-1"}]})

    models, _detail = await testing.list_models(
        "openai_compat", "sk-x", transport=httpx.MockTransport(handler))
    assert models == ["m-1"]
    result = await testing.probe("openai_compat", "sk-x", transport=httpx.MockTransport(handler))
    assert result.ok
    assert seen == ["https://llm.example.com/v1/models"] * 2
