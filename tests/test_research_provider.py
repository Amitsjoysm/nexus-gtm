"""SearchBackedResearchProvider: real web research (Exa) summarized by the LLM, with a real-snippet
fallback so a draft is never grounded on fabricated facts. Offline/deterministic."""
from __future__ import annotations

from nexus.agents.llm import LLMResponse, StubLLMProvider
from nexus.research.provider import (
    SearchBackedResearchProvider,
    StubResearchProvider,
    _parse_obj,
    build_research_provider,
)


class _Hit:
    def __init__(self, title, snippet="", url=""):
        self.title, self.snippet, self.url = title, snippet, url


class _FakeSearch:
    def __init__(self, hits):
        self._hits = hits

    async def search(self, q, *, limit=6):
        return self._hits


class _FakeLLM:
    def __init__(self, text):
        self.text = text

    async def complete(self, messages, **k):
        return LLMResponse(text=self.text)


def test_parse_obj_tolerates_prose():
    assert _parse_obj('ok: {"summary":"x","highlights":[]} done') == {"summary": "x", "highlights": []}
    assert _parse_obj("no json") == {}
    assert _parse_obj(None) == {}


async def test_research_summarizes_real_hits_via_llm():
    hits = [_Hit("Acme raises $50M", "Series C funding", "https://news/acme")]
    llm = _FakeLLM('{"summary":"Acme is a logistics SaaS.","highlights":["Raised $50M Series C"]}')
    prov = SearchBackedResearchProvider(_FakeSearch(hits), llm)
    prof = await prov.research(company="Acme", domain="acme.com")
    assert prof.found is True and prof.source == "search"
    assert "logistics" in prof.summary.lower()
    assert prof.highlights == ["Raised $50M Series C"]
    assert prof.sources == ["https://news/acme"]


async def test_research_falls_back_to_real_hit_titles_when_llm_unusable():
    """Stub LLM returns non-JSON -> fall back to the real hit titles (still genuine web data)."""
    hits = [_Hit("Acme launches new freight product", "...", "https://acme.com/news")]
    prov = SearchBackedResearchProvider(_FakeSearch(hits), StubLLMProvider())
    prof = await prov.research(company="Acme")
    assert prof.found is True
    assert prof.highlights == ["Acme launches new freight product"]  # the real title, not a template


async def test_research_empty_when_no_hits():
    prov = SearchBackedResearchProvider(_FakeSearch([]), _FakeLLM("{}"))
    prof = await prov.research(company="Nowhere")
    assert prof.found is False


async def test_research_empty_when_no_label():
    prov = SearchBackedResearchProvider(_FakeSearch([_Hit("x")]), _FakeLLM("{}"))
    prof = await prov.research()
    assert prof.found is False


def test_build_research_provider_selects_search_or_stub():
    assert isinstance(build_research_provider("search"), SearchBackedResearchProvider)
    assert isinstance(build_research_provider("stub"), StubResearchProvider)
    assert isinstance(build_research_provider(""), StubResearchProvider)
