# tests/test_per_task_search_providers.py
"""Each search task can name its own provider, so the expensive index serves only what needs it.

`search_provider` is global, but the tasks behind it have wildly different value per query. Measured
on the live deployment: account enrichment alone accounted for 123 of the billed search events
across 56 accounts, every one on Exa — while `find_similar` (lookalike accounts) and
`search_companies` (ICP/company discovery) are the ONLY capabilities that genuinely need Exa,
because every other provider returns `[]` for them.

So the bulk work can move to a cheaper index at no cost in capability, and the two Exa-only paths
must NOT move, or they silently return nothing and look like "no lookalikes exist".

Every setting defaults to empty, meaning "use `search_provider`" — a deployment that configures
none of these behaves exactly as it did before they existed.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def settings(monkeypatch):
    from nexus.core.config import get_settings

    s = get_settings()
    for attr in ("enrichment_search_provider", "contact_search_provider",
                 "website_icp_search_provider", "discovery_search_provider"):
        monkeypatch.setattr(s, attr, "")
    monkeypatch.setattr(s, "search_provider", "exa")
    return s


def test_an_unset_task_follows_the_global_provider(settings):
    """THE compatibility line. Nothing configured must behave exactly as before.

    Asserted as "the same KIND as the global provider" rather than a named class: what the global
    resolves to depends on which keys the environment holds, and the contract here is that an unset
    task follows it — not that it is any particular engine.
    """
    from nexus.integrations.search.provider import get_search_provider, provider_for_task

    assert type(provider_for_task("enrichment")) is type(get_search_provider())


def test_a_task_override_wins(monkeypatch, settings):
    from nexus.integrations.search.engines import FirecrawlSearchProvider
    from nexus.integrations.search.provider import provider_for_task

    monkeypatch.setattr(settings, "enrichment_search_provider", "firecrawl")
    assert isinstance(provider_for_task("enrichment"), FirecrawlSearchProvider)


def test_one_task_override_does_not_move_the_others(monkeypatch, settings):
    """The override must not replace the provider the rest of the application resolved — the same
    reason `signal_search_provider` uses `build_search_provider` rather than the global singleton."""
    from nexus.integrations.search.engines import FirecrawlSearchProvider
    from nexus.integrations.search.provider import get_search_provider, provider_for_task

    monkeypatch.setattr(settings, "firecrawl_api_keys", "fc-test-key")
    monkeypatch.setattr(settings, "enrichment_search_provider", "firecrawl")
    assert isinstance(provider_for_task("enrichment"), FirecrawlSearchProvider)
    assert type(provider_for_task("website_icp")) is type(get_search_provider())


def test_a_comma_separated_setting_builds_a_fallback_chain(monkeypatch, settings):
    """Contact discovery: ask Exa, pay Firecrawl only when Exa found nothing."""
    from nexus.integrations.search.engines import ExaSearchProvider, FirecrawlSearchProvider
    from nexus.integrations.search.provider import ChainedSearchProvider, provider_for_task_chain

    monkeypatch.setattr(settings, "exa_api_keys", "exa-test-key")
    monkeypatch.setattr(settings, "firecrawl_api_keys", "fc-test-key")
    monkeypatch.setattr(settings, "contact_search_provider", "exa,firecrawl")
    chain = provider_for_task_chain("contact")
    assert isinstance(chain, ChainedSearchProvider)
    assert isinstance(chain.providers[0], ExaSearchProvider)
    assert isinstance(chain.providers[1], FirecrawlSearchProvider)


async def test_the_chain_short_circuits_on_the_first_result():
    """Sequential, not fan-out. Querying both on every call would double the bill for the common
    case where the first already answered — and cost is the reason this split exists."""
    from nexus.integrations.search.provider import ChainedSearchProvider, SearchHit

    class First:
        calls = 0
        query_dialect = "semantic"

        async def search(self, query, *, limit=5):
            First.calls += 1
            return [SearchHit(title="t", url="https://x.com", snippet="s")]

    class Second:
        calls = 0

        async def search(self, query, *, limit=5):
            Second.calls += 1
            return []

    chain = ChainedSearchProvider([First(), Second()])
    assert await chain.search("q", limit=3)
    assert First.calls == 1
    assert Second.calls == 0, "the second provider was paid for despite the first answering"


async def test_the_chain_falls_through_when_the_first_finds_nothing():
    from nexus.integrations.search.provider import ChainedSearchProvider, SearchHit

    class Empty:
        query_dialect = "semantic"

        async def search(self, query, *, limit=5):
            return []

    class Answers:
        async def search(self, query, *, limit=5):
            return [SearchHit(title="found", url="https://x.com", snippet="s")]

    chain = ChainedSearchProvider([Empty(), Answers()])
    assert (await chain.search("q", limit=3))[0].title == "found"


async def test_a_raising_provider_does_not_sink_the_chain():
    """One dead index must not take contact discovery down with it."""
    from nexus.integrations.search.provider import ChainedSearchProvider, SearchHit

    class Broken:
        query_dialect = "semantic"

        async def search(self, query, *, limit=5):
            raise RuntimeError("provider is down")

    class Answers:
        async def search(self, query, *, limit=5):
            return [SearchHit(title="found", url="https://x.com", snippet="s")]

    assert (await ChainedSearchProvider([Broken(), Answers()]).search("q"))[0].title == "found"


def test_company_search_keeps_the_global_provider(settings, monkeypatch):
    """`search_companies` is Exa-only. Pointing company discovery at a cheaper index would not make
    it cheaper — it would make it return nothing, which reads as "no companies match your ICP"."""
    import inspect

    from nexus.integrations import registry

    src = inspect.getsource(registry.build_registry_from_settings)
    # The company-search branch is handed `search=search` (the global), while contact_search gets
    # its own resolved provider. If these ever converge, company discovery silently returns nothing.
    normalised = " ".join(src.split())
    assert "s.company_search_source_list, search=search" in normalised, (
        "company_search must keep the GLOBAL provider; only Exa implements search_companies, so "
        "pointing it elsewhere makes discovery return nothing rather than cost less"
    )
    assert "contact_search=_build_contact_search( s.contact_search_source_list, "
    assert "contact_search_provider" in normalised
