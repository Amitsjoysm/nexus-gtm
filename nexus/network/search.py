"""NL "who do we know" search (A1).

Two stages, both swappable:
  1. parse — deterministic NL→structured ``NetworkQuery`` (a real LLM adapter can replace
     ``parse_query`` without changing the contract; the StubLLMProvider philosophy).
  2. rank — fetch tenant people who have >=1 *visible* edge, score by keyword match × best visible
     connection strength, return top-N with their broker member ids.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy import or_, select

from nexus.core.tenancy import TenantSession
from nexus.models.network import NetworkEdge, NetworkPerson
from nexus.network.service import visible_edges_where

_TITLE_HINTS = (
    "ceo", "cto", "cfo", "coo", "cmo", "cro", "vp", "head", "director", "founder",
    "manager", "lead", "president", "owner", "engineer", "designer", "recruiter",
    "partner", "investor", "analyst",
)
_STOP = frozenset({
    "at", "in", "the", "a", "an", "of", "who", "find", "people", "person", "someone",
    "know", "me", "we", "our", "is", "are", "and", "or", "to", "for", "with",
})


@dataclass(slots=True)
class NetworkQuery:
    keywords: list[str] = field(default_factory=list)
    titles: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SearchHit:
    person: NetworkPerson
    score: float
    best_strength: int
    broker_member_ids: list[str]


def parse_query(text: str) -> NetworkQuery:
    toks = [t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in _STOP]
    titles = [t for t in toks if t in _TITLE_HINTS]
    keywords = [t for t in toks if len(t) >= 3]
    return NetworkQuery(keywords=keywords, titles=titles)


def _match_score(person: NetworkPerson, q: NetworkQuery) -> float:
    if not q.keywords:
        return 0.5
    hay = (person.search_text or "").lower()
    hits = sum(1 for k in q.keywords if k in hay)
    return hits / len(q.keywords)


async def search_network(
    ts: TenantSession, *, member_id: str, query: str, limit: int = 20
) -> list[SearchHit]:
    q = parse_query(query)
    visible = visible_edges_where(member_id)

    stmt = ts.select(NetworkPerson).where(
        NetworkPerson.id.in_(
            select(NetworkEdge.person_id).where(NetworkEdge.tenant_id == ts.tenant_id, visible)
        )
    )
    if q.keywords:
        stmt = stmt.where(
            or_(*[NetworkPerson.search_text.like(f"%{k}%") for k in q.keywords[:8]])
        )
    people = list((await ts.session.scalars(stmt.limit(500))).all())

    hits: list[SearchHit] = []
    for p in people:
        edges = list(
            (await ts.session.scalars(
                ts.select(NetworkEdge, NetworkEdge.person_id == p.id, visible)
                .order_by(NetworkEdge.strength.desc())
            )).all()
        )
        if not edges:
            continue
        rel = _match_score(p, q)
        if rel <= 0:
            continue
        best = edges[0].strength
        hits.append(
            SearchHit(
                person=p, score=rel * (best / 100.0), best_strength=best,
                broker_member_ids=[e.owner_member_id for e in edges],
            )
        )
    hits.sort(key=lambda h: (h.score, h.best_strength), reverse=True)
    return hits[:limit]
