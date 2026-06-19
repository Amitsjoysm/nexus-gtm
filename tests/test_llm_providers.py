"""LLM provider chain: Groq/Anthropic adapters + the Anthropic->Groq->stub fallback.
Fully offline — no provider is ever actually called over the network here."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from nexus.agents.llm import (
    AnthropicLLMProvider,
    FallbackLLMProvider,
    GroqLLMProvider,
    LLMMessage,
    LLMResponse,
    StubLLMProvider,
    _build_llm_chain,
)


class _Boom(StubLLMProvider):
    async def complete(self, *a, **k):
        raise RuntimeError("provider down")


class _Tagged(StubLLMProvider):
    def __init__(self, tag):
        self.tag = tag

    async def complete(self, *a, **k):
        return LLMResponse(text=self.tag)


@pytest.mark.asyncio
async def test_fallback_returns_first_success_skipping_failures():
    p = FallbackLLMProvider([_Boom(), _Tagged("groq"), _Tagged("stub")])
    out = await p.complete([LLMMessage("user", "hi")])
    assert out.text == "groq"  # skipped the failing primary, used the next


@pytest.mark.asyncio
async def test_fallback_raises_only_if_everything_fails():
    p = FallbackLLMProvider([_Boom(), _Boom()])
    with pytest.raises(RuntimeError):
        await p.complete([LLMMessage("user", "hi")])


def _settings(**over):
    base = dict(
        anthropic_api_key="", anthropic_model="claude-sonnet-4-6",
        groq_api_key="", groq_api_keys="", groq_model="llama-3.3-70b-versatile",
        groq_base_url="https://api.groq.com/openai/v1",
        llm_api_key="", llm_base_url="https://api.openai.com/v1", llm_model="gpt-4o-mini",
    )
    base.update(over)
    # Mirror Settings.groq_api_key_list (primary + pool, deduped, blanks dropped).
    pool = [base["groq_api_key"].strip()] + [k.strip() for k in base["groq_api_keys"].split(",")]
    seen: list[str] = []
    for k in pool:
        if k and k not in seen:
            seen.append(k)
    base["groq_api_key_list"] = seen
    return SimpleNamespace(**base)


def test_auto_chain_prefers_anthropic_then_groq_then_stub():
    chain = _build_llm_chain(_settings(anthropic_api_key="a", groq_api_key="g"))
    assert isinstance(chain, FallbackLLMProvider)
    kinds = [type(p).__name__ for p in chain.providers]
    assert kinds == ["AnthropicLLMProvider", "GroqLLMProvider", "StubLLMProvider"]


def test_auto_chain_groq_only():
    chain = _build_llm_chain(_settings(groq_api_key="g"))
    kinds = [type(p).__name__ for p in chain.providers]
    assert kinds == ["GroqLLMProvider", "StubLLMProvider"]


def test_auto_chain_no_keys_is_bare_stub():
    chain = _build_llm_chain(_settings())
    assert isinstance(chain, StubLLMProvider)  # single provider -> not wrapped


def test_groq_provider_targets_groq_endpoint():
    g = GroqLLMProvider("key", "llama-3.3-70b-versatile")
    assert g.base_url == "https://api.groq.com/openai/v1"
    assert g.model == "llama-3.3-70b-versatile"


def test_groq_key_pool_dedup_and_order():
    from nexus.core.config import Settings

    s = Settings(groq_api_key="a", groq_api_keys="b, c, a")  # 'a' repeated -> deduped, order kept
    assert s.groq_api_key_list == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_groq_rotates_to_next_key_on_429():
    """A rate-limited key (429) makes the provider rotate to the next key and retry."""
    import httpx

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("authorization", "")
        seen.append(auth)
        if auth == "Bearer k1":  # first key is rate-limited
            return httpx.Response(429, json={"error": "rate_limit_exceeded"})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "hi"}}], "usage": {"total_tokens": 5}},
        )

    g = GroqLLMProvider(["k1", "k2"], "m", transport=httpx.MockTransport(handler))
    out = await g.complete([LLMMessage("user", "yo")])
    assert out.text == "hi"
    assert seen == ["Bearer k1", "Bearer k2"]  # rotated after the 429


@pytest.mark.asyncio
async def test_groq_raises_when_all_keys_rate_limited():
    """If every key is 429, the request raises so FallbackLLMProvider can degrade to the stub."""
    import httpx

    g = GroqLLMProvider(
        ["k1", "k2"], "m",
        transport=httpx.MockTransport(lambda r: httpx.Response(429, json={})),
    )
    with pytest.raises(httpx.HTTPStatusError):
        await g.complete([LLMMessage("user", "yo")])


@pytest.mark.asyncio
async def test_anthropic_splits_system_from_turns(monkeypatch):
    """System messages become the top-level `system` field; only user/assistant go in messages."""
    captured = {}

    class _FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"content": [{"type": "text", "text": "ok"}],
                    "usage": {"input_tokens": 3, "output_tokens": 2}}

    class _FakeClient:
        async def post(self, url, json, headers):
            captured["json"] = json
            captured["headers"] = headers
            return _FakeResp()

    prov = AnthropicLLMProvider("secret", "claude-sonnet-4-6")
    monkeypatch.setattr(prov, "_http", lambda: _FakeClient())
    out = await prov.complete(
        [LLMMessage("system", "You are X"), LLMMessage("user", "hello")]
    )
    assert out.text == "ok" and out.tokens == 5
    assert captured["json"]["system"] == "You are X"
    assert captured["json"]["messages"] == [{"role": "user", "content": "hello"}]
    assert captured["headers"]["x-api-key"] == "secret"
