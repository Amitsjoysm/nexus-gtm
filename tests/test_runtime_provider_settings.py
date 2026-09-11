"""The provider settings a superadmin needs to fix a degraded deployment — changeable on the go.

Asked for by the product owner 2026-09-11, after staging ran on shipped defaults that silently
degrade the product: `contact_search_sources=stub` ("Find contacts finds nobody"), `llm_provider`
reaching the stub (canned text sent to real prospects), `research_provider=stub`,
`email_verify_provider=stub`. None of them was in the Control plane, so fixing one meant a redeploy.

Adding a catalog entry was never going to be enough on its own. The panel applies an override with
`setattr`, and every one of these is read ONCE into a cached singleton — the registry, the LLM chain,
the email verifier, the research provider, the agent runtime. A bare entry would save, report "in
effect" and change nothing until a restart: the exact trap the catalog's rules exist to prevent. So
each gets an on-change hook that drops the caches it feeds, and these tests assert the CHANGE
happens, not merely that the value was stored.

Two settings are deliberately NOT offered, and named in `FORBIDDEN` so the refusal says why:
`search_provider` (discovery is strictly Exa in staging/prod and reads nothing else — a picker
would be inert) and `payment_provider` (switching to "noop" would silently stop collecting money;
the Payment credentials screen, with its verification-gated activation, governs that).
"""
from __future__ import annotations

import pytest

from nexus.core.config import get_settings

NEW_KEYS = ("llm_provider", "contact_search_sources", "research_provider",
            "email_verify_provider", "signal_search_provider")


@pytest.fixture(autouse=True)
def _restore_settings_and_caches():
    """Same guard as test_runtime_config.py — monkeypatch cannot undo a mutation made BEFORE it
    snapshots — plus the caches the on-change hooks drop, so nothing leaks into the next test."""
    from nexus.runtime_config import service
    from nexus.runtime_config.catalog import CATALOG

    settings = get_settings()
    before = {k: getattr(settings, k) for k in CATALOG if hasattr(settings, k)}
    service._overridden_here.clear()
    try:
        yield
    finally:
        for key, value in before.items():
            setattr(settings, key, value)
        service._overridden_here.clear()
        service._reset_providers()


# ---- what the panel offers ----------------------------------------------------------------------

@pytest.mark.parametrize("key", NEW_KEYS)
def test_each_provider_setting_is_in_the_panel_with_an_effect_and_a_warning(key):
    from nexus.runtime_config.catalog import CATALOG

    spec = CATALOG[key]
    assert spec.effect.strip(), f"{key} does not say what changing it does"
    assert spec.warning.strip(), f"{key} does not say what it costs or breaks"
    assert spec.options, f"{key} is free text; a typo would silently degrade the product"
    assert not spec.requires_restart, f"{key} must take effect without a restart"


def test_the_llm_picker_never_offers_the_stub():
    """The stub's fluent, canned text is what reaches prospects when the LLM is down. Offering it
    as a CHOICE would let one click send it on purpose."""
    from nexus.runtime_config.catalog import CATALOG

    assert "stub" not in CATALOG["llm_provider"].options


def test_the_signal_picker_never_offers_exa():
    """Signals and the daily scan do not use Exa (decided 2026-09-10)."""
    from nexus.runtime_config.catalog import CATALOG

    assert "exa" not in CATALOG["signal_search_provider"].options


@pytest.mark.parametrize("key", ["search_provider", "payment_provider"])
def test_withheld_settings_are_refused_with_a_reason(key):
    from nexus.runtime_config.catalog import FORBIDDEN
    from nexus.runtime_config.service import UnknownSetting, _spec

    assert key in FORBIDDEN
    with pytest.raises(UnknownSetting, match="deliberately not changeable"):
        _spec(key)


# ---- a change actually takes effect, in this process and in the others ----------------------------

async def test_changing_the_llm_provider_rebuilds_the_llm_without_a_restart(monkeypatch):
    from nexus.agents.llm import get_llm_provider
    from nexus.runtime_config.service import set_override

    monkeypatch.setattr(get_settings(), "llm_provider", "stub")
    before = get_llm_provider()
    await set_override("llm_provider", "auto")
    assert get_settings().llm_provider == "auto"
    assert get_llm_provider() is not before, "the cached LLM survived the change"


async def test_changing_contact_sources_rebuilds_the_registry(monkeypatch):
    """The fix for a deployment stuck on the shipped `stub` default, applied live."""
    from nexus.integrations.registry import get_registry
    from nexus.runtime_config.service import set_override

    monkeypatch.setattr(get_settings(), "contact_search_sources", "stub")
    before = get_registry()
    assert [p.name for p in before.contact_search_providers] == ["stub"]

    await set_override("contact_search_sources", "search")
    after = get_registry()
    assert after is not before
    assert [p.name for p in after.contact_search_providers] == ["search"]


async def test_saving_the_same_value_again_does_not_rebuild_anything(monkeypatch):
    """The sweep re-applies every override every 30s. Rebuilding on each pass would throw away the
    registry's cache and the LLM chain's sticky key rotation for nothing."""
    from nexus.integrations.registry import get_registry
    from nexus.runtime_config.service import apply_overrides, set_override

    await set_override("contact_search_sources", "search")
    registry = get_registry()
    await apply_overrides()
    assert get_registry() is registry


async def test_an_override_cleared_by_another_process_reverts_here_too(monkeypatch):
    """`clear_override` in the API used to leave every OTHER process — the worker, the second API
    worker — on the old value until a restart, though a comment claimed the sweep converged it."""
    from sqlalchemy import delete

    from nexus.core.db import get_platform_sessionmaker
    from nexus.integrations.registry import get_registry
    from nexus.models.runtime_setting import RuntimeSetting
    from nexus.runtime_config.service import apply_overrides, set_override

    env_value = get_settings().contact_search_sources
    await set_override("contact_search_sources", "search" if env_value != "search" else "stub")
    changed = get_registry()

    # Another process clears it: the row goes, and this process only finds out on its sweep.
    async with get_platform_sessionmaker()() as s:
        await s.execute(delete(RuntimeSetting).where(RuntimeSetting.key == "contact_search_sources"))
        await s.commit()
    await apply_overrides()

    assert get_settings().contact_search_sources == env_value
    assert get_registry() is not changed, "the cached registry kept the cleared value"


async def test_a_signal_provider_change_reaches_an_existing_source(monkeypatch):
    """The ingestion service is built once and holds its sources; a source that cached its provider
    forever would ignore the panel until a restart."""
    from nexus.ingestion.sources import DorkedSearchSource
    from nexus.integrations.search.provider import set_search_provider

    set_search_provider(None)
    monkeypatch.setattr(get_settings(), "firecrawl_api_keys", "fc-test-key")
    monkeypatch.setattr(get_settings(), "signal_search_provider", "firecrawl")
    source = DorkedSearchSource()
    assert source._provider().name == "firecrawl"

    monkeypatch.setattr(get_settings(), "signal_search_provider", "duckduckgo")
    assert source._provider().name == "duckduckgo"


def test_an_injected_signal_provider_is_never_replaced():
    """The dork tests inject their fake by constructor; re-resolving must not override that."""
    from nexus.ingestion.sources import DorkedSearchSource

    class _Fake:
        name = "fake"

    source = DorkedSearchSource(search=_Fake())
    assert source._provider().name == "fake"
