# nexus/research/provider.py
"""Account-research capability: build a short profile of a company from the open web.

A :class:`ResearchProvider` answers two questions the messaging/QA agents need: "what is this
company about" (:meth:`research`) and "who/what is at this URL" (:meth:`profile_from_url`).
CloakBrowser + Scrapegraph-ai implement the real version; the stub keeps the system runnable
offline. Adapters never raise across the boundary — on failure they return an empty profile.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field


@dataclass(slots=True)
class ResearchProfile:
    """A compact, citation-bearing summary. ``found`` is False when nothing was gathered."""

    found: bool = False
    summary: str = ""
    highlights: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    source: str = ""

    def as_dict(self) -> dict:
        return {
            "found": self.found,
            "summary": self.summary,
            "highlights": list(self.highlights),
            "sources": list(self.sources),
            "source": self.source,
        }


class ResearchProvider(abc.ABC):
    name: str

    @abc.abstractmethod
    async def research(self, *, company: str | None = None,
                       domain: str | None = None) -> ResearchProfile: ...

    @abc.abstractmethod
    async def profile_from_url(self, url: str) -> ResearchProfile: ...


class StubResearchProvider(ResearchProvider):
    """Deterministic offline default: no network, empty profile."""

    name = "stub"

    async def research(self, *, company: str | None = None,
                       domain: str | None = None) -> ResearchProfile:
        return ResearchProfile(source=self.name)

    async def profile_from_url(self, url: str) -> ResearchProfile:
        return ResearchProfile(source=self.name)


_research: ResearchProvider | None = None


def build_research_provider(name: str) -> ResearchProvider:
    key = (name or "").strip().lower()
    if key in ("stub", "", "none"):
        return StubResearchProvider()
    # Scrapegraph / Cloak adapters land here later; fail safe to the offline stub for now.
    return StubResearchProvider()


def get_research_provider() -> ResearchProvider:
    global _research
    if _research is None:
        from nexus.core.config import get_settings

        _research = build_research_provider(get_settings().research_provider)
    return _research


def set_research_provider(provider: ResearchProvider | None) -> None:
    global _research
    _research = provider
