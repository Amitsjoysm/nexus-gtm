# nexus/research/provider.py
"""Account-research capability: build a short profile of a company from the open web.

A :class:`ResearchProvider` answers two questions the messaging/QA agents need: "what is this
company about" (:meth:`research`) and "who/what is at this URL" (:meth:`profile_from_url`).
CloakBrowser + Scrapegraph-ai implement the real version; the stub keeps the system runnable
offline. Adapters never raise across the boundary — on failure they return an empty profile.
"""
from __future__ import annotations

import abc
import json
import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger("nexus.research.provider")


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
    """Deterministic offline default: zero-network, but **not** empty.

    The stub returns *templated* facts (analogous to how :class:`StubLLMProvider` returns
    deterministic text) so the grounding pipeline is genuinely exercisable offline: a draft
    composed in tests is "grounded" and the grounded-send gate has something to assert against.
    The facts are obviously synthetic and carry ``source="stub"`` so nothing downstream mistakes
    them for real intelligence. ``profile_from_url`` stays empty — URL seeding is a real-data
    capability with no sensible deterministic fixture.
    """

    name = "stub"

    async def research(self, *, company: str | None = None,
                       domain: str | None = None) -> ResearchProfile:
        label = (company or domain or "this company").strip() or "this company"
        sources = [f"https://{domain}"] if domain else []
        return ResearchProfile(
            found=True,
            summary=f"{label} is an active company evaluating tooling in its category.",
            highlights=[
                f"{label} has been investing in go-to-market initiatives.",
                f"Recent hiring and activity suggest {label} is in a buying window.",
            ],
            sources=sources,
            source=self.name,
        )

    async def profile_from_url(self, url: str) -> ResearchProfile:
        return ResearchProfile(source=self.name)


_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_obj(text: str | None) -> dict:
    if not text:
        return {}
    m = _JSON_OBJ_RE.search(text)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


class SearchBackedResearchProvider(ResearchProvider):
    """Real company research from the open web (Exa) summarized by the LLM.

    Asks the search provider for the company's overview/news, then has the LLM distill a 2-sentence
    summary + a few rep-usable highlights from the real snippets. Robust: if the LLM yields nothing
    usable but search returned hits, it falls back to the *real* hit titles/snippets — so a draft is
    grounded in genuine web data, never a fabricated template. Empty only when search returns nothing
    (then the caller's grounded-send gate correctly withholds the email)."""

    name = "search"

    def __init__(self, search, llm):
        self.search_provider = search
        self.llm = llm

    async def research(self, *, company: str | None = None,
                       domain: str | None = None) -> ResearchProfile:
        label = (company or domain or "").strip()
        if not label:
            return ResearchProfile(source=self.name)
        try:
            hits = await self.search_provider.search(
                f"{label} company overview products customers news", limit=6
            )
        except Exception as exc:  # provider isolation
            logger.warning("research search failed for %r: %r", label, exc)
            return ResearchProfile(source=self.name)
        hits = list(hits or [])
        if not hits:
            return ResearchProfile(found=False, source=self.name)

        sources = [u for u in (getattr(h, "url", "") for h in hits) if u][:5]
        blob = "\n".join(
            f"- {getattr(h, 'title', '')}: {getattr(h, 'snippet', '')} ({getattr(h, 'url', '')})"
            for h in hits
        )
        summary, highlights = "", []
        try:
            from nexus.agents.llm import LLMMessage

            resp = await self.llm.complete(
                [
                    LLMMessage("system", "You summarize a company from web snippets for a sales "
                               "rep. Use only facts present; be specific and concise."),
                    LLMMessage("user", f"Company: {label}.\nResults:\n{blob}\n\n"
                               'Return JSON {"summary": "<=2 sentences", "highlights": '
                               '["<=3 specific recent facts a rep could reference"]}. '
                               'If nothing usable, return {"summary":"","highlights":[]}.'),
                ],
                temperature=0.0, max_tokens=400, purpose="research_brief",
            )
            data = _parse_obj(resp.text)
            summary = (data.get("summary") or "").strip()
            highlights = [h.strip() for h in (data.get("highlights") or [])
                          if isinstance(h, str) and h.strip()][:3]
        except Exception as exc:
            logger.warning("research summarize failed for %r: %r", label, exc)

        # Fall back to the real hit titles when the LLM didn't produce structured facts — still
        # genuine web data, so the draft stays grounded rather than fabricated or skipped.
        if not summary and not highlights:
            titles = [getattr(h, "title", "").strip() for h in hits if getattr(h, "title", "")]
            highlights = titles[:3]
            summary = f"{label}: {titles[0]}" if titles else ""
        return ResearchProfile(
            found=bool(summary or highlights), summary=summary,
            highlights=highlights, sources=sources, source=self.name,
        )

    async def profile_from_url(self, url: str) -> ResearchProfile:
        return ResearchProfile(source=self.name)


_research: ResearchProvider | None = None


def build_research_provider(name: str) -> ResearchProvider:
    key = (name or "").strip().lower()
    if key == "search":
        from nexus.agents.llm import get_llm_provider
        from nexus.integrations.search.provider import get_search_provider

        return SearchBackedResearchProvider(get_search_provider(), get_llm_provider())
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
