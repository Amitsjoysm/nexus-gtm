"""Net-new contact discovery: find a buying-committee person for an account.

The offline default returns one deterministic stub persona so the sourcing path is fully
exercisable with zero network and reproducible test counts. Real providers (Apollo / InfoJoy /
ZoomInfo) slot in behind this same ABC later, wired through ``contact_search_sources``.
"""
from __future__ import annotations

import abc
import json
import logging
import re
from dataclasses import dataclass, field

from nexus.models.account import Account

logger = logging.getLogger("nexus.integrations.contact_search")


@dataclass(slots=True)
class ContactCandidate:
    full_name: str
    title: str | None = None
    seniority: str | None = None
    email: str | None = None
    linkedin_url: str | None = None
    source: str = ""
    confidence: float = 0.0
    provenance: dict = field(default_factory=dict)


class ContactSearchProvider(abc.ABC):
    name: str

    @abc.abstractmethod
    async def search(
        self, account: Account, icp: dict, *, limit: int = 3
    ) -> list[ContactCandidate]: ...


# When the ICP names no buyer titles, fall back to a typical B2B buying committee so contact
# discovery still returns a multi-role committee rather than a single generic "Lead".
_DEFAULT_COMMITTEE: tuple[str, ...] = (
    "VP Sales", "Head of Operations", "Chief Technology Officer",
    "Chief Financial Officer", "Chief Executive Officer",
)


class StubContactSearchProvider(ContactSearchProvider):
    """Deterministic offline buying committee: one persona per ICP buyer title (capped at
    ``limit``). Names are placeholders (a real provider — Exa people-search / Apollo — supplies
    real identities); titles are real so the email finder can pattern-guess a role address and
    Reacher can verify it. Returns up to ``limit`` distinct personas, not just one."""

    name = "stub"

    async def search(
        self, account: Account, icp: dict, *, limit: int = 3
    ) -> list[ContactCandidate]:
        titles = list((icp or {}).get("buyer_titles") or ()) or list(_DEFAULT_COMMITTEE)
        out: list[ContactCandidate] = []
        for title in titles[: max(1, limit)]:
            out.append(
                ContactCandidate(
                    full_name=f"{account.name} {title}",
                    title=title,
                    email=None,
                    source=self.name,
                    confidence=0.3,
                )
            )
        return out


_JSON_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)


def _parse_people(text: str | None) -> list[dict]:
    """Pull a JSON array of people out of an LLM reply, tolerating prose around it.
    Returns [] for the stub/non-JSON case, so the offline path yields no synthetic names."""
    if not text:
        return []
    m = _JSON_ARRAY_RE.search(text)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except (json.JSONDecodeError, ValueError):
        return []
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []


class SearchBackedContactSearchProvider(ContactSearchProvider):
    """Find *real* people for an account via web search (Exa) + LLM extraction.

    Queries the company's leadership/team and LinkedIn presence, then asks the LLM to pull the
    real names + titles that match the ICP's buyer titles out of the result snippets. Needs a real
    LLM to do anything: offline (stub LLM) extraction returns nothing, so the caller falls back to
    the role-persona stub. Never raises across the boundary — a failure yields ``[]``."""

    name = "search"

    def __init__(self, search, llm, *, confidence: float = 0.5):
        self.search_provider = search
        self.llm = llm
        self.confidence = confidence

    async def search(
        self, account: Account, icp: dict, *, limit: int = 3
    ) -> list[ContactCandidate]:
        titles = list((icp or {}).get("buyer_titles") or (icp or {}).get("titles") or [])

        # A level/keyword spec expands into the phrasings a web index actually holds. Querying only
        # the one literal title a rep typed is why a search for "Facilities Director" returned
        # nothing while the index held "Director of Facilities" and "Head of Facilities".
        from nexus.relevance.job_levels import expand_titles, matches_title, spec_from_icp

        title_spec = spec_from_icp(icp)
        expanded = expand_titles(title_spec)
        if expanded:
            titles = list(dict.fromkeys([*titles, *expanded]))

        try:
            hits = await self._gather_hits(account, titles, limit)
            if not hits:
                return []
            people = await self._extract_people(account, titles, hits, limit)
        except Exception as exc:  # provider isolation
            logger.warning("search contact extraction failed for %s: %r",
                           getattr(account, "domain", None), exc)
            return []

        out: list[ContactCandidate] = []
        seen: set[str] = set()
        for p in people:
            name = (p.get("full_name") or "").strip()
            if not name or name.lower() in seen:
                continue
            # Re-filter against the spec. The expanded queries deliberately over-match — a broad
            # phrase costs a little recall noise, which is the right way round — but a person who
            # does not satisfy the spec must not reach the rep. Only applied when a spec exists, so
            # a workspace using plain buyer_titles is untouched.
            if any(title_spec.values()) and not matches_title(p.get("title") or "", title_spec):
                continue
            seen.add(name.lower())
            out.append(
                ContactCandidate(
                    full_name=name,
                    title=(p.get("title") or "").strip() or None,
                    seniority=(p.get("seniority") or "").strip() or None,
                    linkedin_url=p.get("linkedin_url") or None,
                    source=self.name,
                    confidence=self.confidence,
                )
            )
            if len(out) >= limit:
                break
        return out

    async def _gather_hits(self, account: Account, titles: list[str], limit: int) -> list[dict]:
        company = account.name or account.domain or ""
        if not company:
            return []
        # Ground the people search in what the company is (industry) before chasing titles, so the
        # results are that company's actual leadership rather than namesakes elsewhere.
        industry = (account.industry or "").strip()
        ctx = f" {industry}" if industry else ""
        queries = [f"{company}{ctx} leadership team executives"]
        if titles:
            queries.append(f'{" OR ".join(titles[:3])} at {company}{ctx} linkedin')
        hits: list[dict] = []
        for q in queries:
            try:
                res = await self.search_provider.search(q, limit=max(5, limit))
            except Exception:  # one bad query shouldn't sink the others
                res = []
            for h in res:
                hits.append({
                    "title": getattr(h, "title", "") or "",
                    "snippet": getattr(h, "snippet", "") or "",
                    "url": getattr(h, "url", "") or "",
                })
        return hits[:12]

    async def _extract_people(
        self, account: Account, titles: list[str], hits: list[dict], limit: int
    ) -> list[dict]:
        from nexus.agents.llm import LLMMessage

        blob = "\n".join(f"- {h['title']} :: {h['snippet']} ({h['url']})" for h in hits)
        want = (
            ", ".join(titles)
            if titles
            else "senior decision-makers (VP / C-level across Sales, Operations, Technology, Finance)"
        )
        system = (
            "You extract real, named people from web-search results about one company. "
            "Output ONLY a JSON array — no prose, no code fences."
        )
        user = (
            f"Company: {account.name} ({account.domain or 'unknown domain'}).\n"
            f"Find up to {limit} real people who plausibly hold these roles: {want}.\n"
            f"Search results:\n{blob}\n\n"
            'Return a JSON array of objects with keys "full_name", "title", "seniority", '
            '"linkedin_url". Only include a person whose actual name appears in the results. '
            "If you cannot find any real names, return []."
        )
        resp = await self.llm.complete(
            [LLMMessage("system", system), LLMMessage("user", user)],
            # A JSON ARRAY of up to `limit` people, four keys each (a LinkedIn URL alone
            # runs ~20 tokens). 700 closed the array only for the smallest results; a
            # truncated array parses to [] and reads as "this company has no contacts".
            temperature=0.0, max_tokens=1600, purpose="contact_extract",
        )
        return _parse_people(resp.text)
