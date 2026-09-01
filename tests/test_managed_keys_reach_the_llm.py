# tests/test_managed_keys_reach_the_llm.py
"""A key added in the Control plane must actually be used, with nothing in the environment.

Reported from the live deployment: provider keys added through the admin panel "don't get [used],
need to manually add via .env file only".

The resolver was fine — `managed_pool("groq")` returned the stored key. The failure is one line
earlier, in `_build_llm_chain`::

    if s.groq_api_key_list:                     # <-- ENVIRONMENT keys only
        chain.append(GroqLLMProvider(...))

Chain membership was decided from the environment. With an empty env the provider was never added,
so `GroqLLMProvider._refresh_keys` — which DOES read the managed pool, and exists precisely so a key
added in the panel reaches a running process — never got the chance to run. Everything fell through
to the stub, which is silent: fluent output that looks finished and is not what the customer
configured.

`GroqLLMProvider.__init__` compounded it by refusing to be constructed with an empty key list, so
even "add it and let it refresh" was impossible. Keys now decide what a provider CAN DO, not
whether it may exist; the chain still ends in the stub, so a provider that never finds a key fails
over exactly as before.
"""
from __future__ import annotations

import pytest


def test_a_provider_can_exist_before_its_keys_resolve():
    """It self-heals from the managed pool at request time; forbidding the empty construction is
    what made a panel-only deployment impossible."""
    from nexus.agents.llm import GroqLLMProvider

    p = GroqLLMProvider(api_keys=[], model="openai/gpt-oss-120b")
    assert p is not None


async def test_a_provider_with_no_key_anywhere_still_fails_over():
    """The compatibility line. An empty provider must not raise past the chain — the stub tail is
    what guarantees completion never hard-fails."""
    from nexus.agents.llm import (
        FallbackLLMProvider,
        GroqLLMProvider,
        LLMMessage,
        StubLLMProvider,
    )

    chain = FallbackLLMProvider(
        [GroqLLMProvider(api_keys=[], model="m"), StubLLMProvider()]
    )
    out = await chain.complete([LLMMessage("user", "hi")], max_tokens=10)
    assert out.text, "the stub tail must still answer when no key resolves"


def test_the_chain_includes_groq_when_only_managed_keys_exist(monkeypatch):
    """THE bug. No env keys, a managed key in the panel — Groq must be in the chain."""
    from nexus.agents.llm import FallbackLLMProvider, GroqLLMProvider, _build_llm_chain
    from nexus.core.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "groq_api_keys", "")
    monkeypatch.setattr(s, "groq_api_key", "")
    monkeypatch.setattr(s, "anthropic_api_key", "")
    monkeypatch.setattr(s, "llm_api_key", "")
    monkeypatch.setattr(s, "llm_provider", "auto")

    chain = _build_llm_chain(s)
    members = chain.providers if isinstance(chain, FallbackLLMProvider) else [chain]
    assert any(isinstance(m, GroqLLMProvider) for m in members), (
        "Groq is absent from the chain with an empty environment, so a key added in the Control "
        "plane can never be picked up — the reported failure"
    )


def test_env_keys_still_build_the_chain(monkeypatch):
    """Regression guard: a deployment configured entirely through .env is unchanged."""
    from nexus.agents.llm import FallbackLLMProvider, GroqLLMProvider, _build_llm_chain
    from nexus.core.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "groq_api_keys", "gsk-env-one,gsk-env-two")
    monkeypatch.setattr(s, "anthropic_api_key", "")
    monkeypatch.setattr(s, "llm_api_key", "")
    monkeypatch.setattr(s, "llm_provider", "auto")

    chain = _build_llm_chain(s)
    members = chain.providers if isinstance(chain, FallbackLLMProvider) else [chain]
    groq = next(m for m in members if isinstance(m, GroqLLMProvider))
    assert len(groq._keys) == 2, "explicitly configured env keys must still be used"


def test_the_stub_tail_is_always_present(monkeypatch):
    """Nothing above may remove the guarantee that a completion never hard-fails."""
    from nexus.agents.llm import FallbackLLMProvider, StubLLMProvider, _build_llm_chain
    from nexus.core.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "groq_api_keys", "")
    monkeypatch.setattr(s, "anthropic_api_key", "")
    monkeypatch.setattr(s, "llm_api_key", "")
    chain = _build_llm_chain(s)
    members = chain.providers if isinstance(chain, FallbackLLMProvider) else [chain]
    assert isinstance(members[-1], StubLLMProvider)


async def test_apify_accepts_an_empty_key_list():
    """Apify already refreshes from the managed pool, and — unlike Groq did — allows the empty
    construction that makes the refresh reachable. Pinned so it stays that way."""
    from nexus.integrations.apify import ApifyClient

    assert ApifyClient([]) is not None


def test_a_selected_engine_is_built_when_only_managed_keys_exist(monkeypatch):
    """The reported failure in the search layer, and it failed more quietly than the LLM one: a
    provider whose keys lived only in the admin panel was replaced by DuckDuckGo, so searches ran
    and returned plausible results from an engine nobody chose."""
    import time

    from nexus.core.config import get_settings
    from nexus.integrations.search.engines import BraveSearchProvider, build_engine
    from nexus.providers import resolver

    s = get_settings()
    for attr in ("brave_api_key", "exa_api_key", "exa_api_keys"):
        if hasattr(s, attr):
            monkeypatch.setattr(s, attr, "")

    # A running process that has refreshed at least once — which is every process serving traffic.
    monkeypatch.setitem(resolver._CACHE._pools, "brave", (time.monotonic(), ["brave-managed-key"]))
    assert isinstance(build_engine("brave", s), BraveSearchProvider), (
        "Brave was swapped for DuckDuckGo despite a managed key, so the key could never be used"
    )


def test_no_key_anywhere_still_degrades_to_duckduckgo(monkeypatch):
    """THE compatibility line. A selected engine with nothing behind it must degrade to a keyless
    index, not to a dark provider that silently returns nothing -- the property
    test_build_engine_falls_back_to_duckduckgo_without_key has always asserted."""
    from nexus.core.config import get_settings
    from nexus.integrations.search.engines import DuckDuckGoSearchProvider, build_engine
    from nexus.providers import resolver

    s = get_settings()
    for attr in ("brave_api_key", "exa_api_key", "exa_api_keys"):
        if hasattr(s, attr):
            monkeypatch.setattr(s, attr, "")
    resolver.invalidate("brave")
    assert isinstance(build_engine("brave", s), DuckDuckGoSearchProvider)


def test_brave_and_serper_can_refresh_from_the_managed_pool():
    """Exa and Firecrawl already could; these two held a single key captured at construction, so a
    panel key was invisible to them forever."""
    from nexus.integrations.search.engines import BraveSearchProvider, SerperSearchProvider

    for cls in (BraveSearchProvider, SerperSearchProvider):
        assert hasattr(cls, "_refresh_keys"), f"{cls.__name__} cannot pick up a managed key"


async def test_a_search_provider_with_no_key_returns_empty_rather_than_raising():
    """The compatibility line for the gate change: building the provider instead of substituting
    DuckDuckGo must not turn a missing key into an exception on the caller."""
    from nexus.integrations.search.engines import BraveSearchProvider

    assert await BraveSearchProvider("").search("anything", limit=3) == []
