"""Which model writes emails and call scripts is a Superadmin choice, Claude first (2026-10-26).

Owner decision: "a separate control in the Superadmin panel for mail and call scripts, with Claude
the default and first priority, then the next best provider available or added". Two gaps stood in
the way: the chain used one order for every purpose, and Anthropic was built only from
``NEXUS_ANTHROPIC_API_KEY``, so a key saved under Provider keys did nothing (CLAUDE.md, "Anthropic
has the same gap and is not fixed yet").
"""
from __future__ import annotations

import json

import httpx
import pytest

from nexus.agents import llm
from nexus.agents.llm import LLMMessage, LLMResponse, LLMProvider


class _Named(LLMProvider):
    """A provider that answers with its own name and the model it was told to use."""

    def __init__(self, name, *, fail=None):
        self.name, self.fail, self.calls = name, fail, []

    async def complete(self, messages, *, temperature=0.2, max_tokens=800, purpose=None,
                       variables=None):
        self.calls.append(purpose)
        if self.fail:
            raise self.fail
        return LLMResponse(text=f"{self.name}:{llm.model_override() or '-'}", tokens=1)


def _router(**fails):
    names = ("anthropic", "groq", "openai_compat")
    providers = {n: _Named(n, fail=fails.get(n)) for n in names}
    router = llm.PurposeRouter([providers[n] for n in names] + [llm.StubLLMProvider()],
                               names=list(names) + ["stub"])
    return router, providers


@pytest.fixture
def settings(monkeypatch):
    from nexus.core.config import get_settings

    s = get_settings()
    for key in ("llm_email_provider", "llm_call_script_provider"):
        monkeypatch.setattr(s, key, "anthropic")
    for key in ("llm_email_model", "llm_call_script_model"):
        monkeypatch.setattr(s, key, "")
    return s


async def _ask(router, purpose):
    return (await router.complete([LLMMessage("user", "hi")], purpose=purpose)).text


async def test_claude_writes_emails_and_call_scripts_by_default(settings):
    router, _ = _router()
    assert await _ask(router, "outreach_message") == "anthropic:-"
    assert await _ask(router, "call_script") == "anthropic:-"


async def test_each_purpose_takes_its_own_provider_and_model(settings, monkeypatch):
    monkeypatch.setattr(settings, "llm_email_provider", "groq")
    monkeypatch.setattr(settings, "llm_email_model", "openai/gpt-oss-120b")
    monkeypatch.setattr(settings, "llm_call_script_model", "claude-opus-5-5")
    router, providers = _router()

    assert await _ask(router, "outreach_message") == "groq:openai/gpt-oss-120b"
    assert await _ask(router, "call_script") == "anthropic:claude-opus-5-5"
    # Everything else keeps the deployment's order, with no override.
    assert await _ask(router, "research_brief") == "anthropic:-"


async def test_the_next_provider_answers_when_the_chosen_one_cannot(settings):
    router, providers = _router(anthropic=llm.NotConfigured("no Anthropic key"))

    out = await router.complete([LLMMessage("user", "hi")], purpose="outreach_message")

    assert out.text == "groq:-", "the override belongs to the chosen provider only"
    assert out.provider == "_Named"


async def test_a_provider_with_no_key_is_skipped_without_a_fallback_alarm(settings, monkeypatch):
    """No key is not an outage: counting it as a fallback fired the alert on every call."""
    from nexus.core import metrics

    fired = []
    monkeypatch.setattr(metrics, "record_llm_fallback", lambda **kw: fired.append(kw))
    router, _ = _router(anthropic=llm.NotConfigured("no Anthropic key"))

    await _ask(router, "outreach_message")

    assert fired == []


# ---- Anthropic reads the panel ------------------------------------------------------------------

async def test_anthropic_uses_the_key_and_model_saved_under_provider_keys(monkeypatch):
    from nexus.providers import resolver

    async def pool(provider):
        return ["sk-ant-panel"] if provider == "anthropic" else []

    async def model(provider):
        return "claude-sonnet-5-5" if provider == "anthropic" else ""

    monkeypatch.setattr(resolver, "managed_pool", pool)
    monkeypatch.setattr(resolver, "model_for", model)
    sent = []

    def handler(request):
        sent.append((request.headers["x-api-key"], json.loads(request.content)["model"]))
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}],
                                         "usage": {"input_tokens": 3, "output_tokens": 2}})

    provider = llm.ManagedAnthropicProvider("", "claude-sonnet-4-6",
                                            transport=httpx.MockTransport(handler))
    out = await provider.complete([LLMMessage("user", "hi")])

    assert out.text == "ok" and sent == [("sk-ant-panel", "claude-sonnet-5-5")]

    with llm.using_model("claude-opus-5-5"):
        await provider.complete([LLMMessage("user", "hi")])
    assert sent[-1][1] == "claude-opus-5-5"


async def test_anthropic_with_no_key_anywhere_sends_nothing(monkeypatch):
    from nexus.providers import resolver

    async def empty(provider):
        return []

    monkeypatch.setattr(resolver, "managed_pool", empty)

    def handler(request):
        raise AssertionError("nothing may be sent without a key")

    provider = llm.ManagedAnthropicProvider("", "claude-sonnet-4-6",
                                            transport=httpx.MockTransport(handler))
    with pytest.raises(llm.NotConfigured):
        await provider.complete([LLMMessage("user", "hi")])


def test_auto_puts_claude_first_even_with_no_key_in_the_environment(monkeypatch):
    from nexus.core.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "llm_provider", "auto")
    monkeypatch.setattr(s, "anthropic_api_key", "")
    chain = llm._build_llm_chain(s)

    assert isinstance(chain, llm.PurposeRouter)
    assert isinstance(chain.providers[0], llm.ManagedAnthropicProvider)
    assert isinstance(chain.providers[-1], llm.StubLLMProvider)


# ---- the Superadmin control ---------------------------------------------------------------------

def test_the_panel_offers_both_purposes_with_claude_as_the_default():
    from nexus.core.config import Settings
    from nexus.runtime_config.catalog import CATALOG

    for purpose in ("email", "call_script"):
        spec = CATALOG[f"llm_{purpose}_provider"]
        assert spec.options == ("anthropic", "groq", "openai_compat") and spec.effect
        assert getattr(Settings(), f"llm_{purpose}_provider") == "anthropic"
        assert CATALOG[f"llm_{purpose}_model"].kind == "str"


@pytest.mark.parametrize("value, ok", [("", True), ("claude-opus-5-5", True),
                                        ("openai/gpt-oss-120b", True), ("bad model\nname", False),
                                        ("x" * 200, False)])
def test_a_model_name_is_checked_before_it_is_stored(value, ok):
    from nexus.runtime_config.service import validate

    if ok:
        validate("llm_email_model", value)
    else:
        with pytest.raises(ValueError):
            validate("llm_email_model", value)
