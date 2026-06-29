"""Deterministic identity resolution: fold raw identities into canonical NetworkPersons.

Resolution order (idempotent, conservative — never bad-merge):
  1. exact normalized email,
  2. else exact normalized name + company (for emailless records),
  3. else create a new person.

NOTE: this is identity matching, not role similarity. ``lookalike/similarity.py`` (used by search)
scores *similar roles* and would wrongly merge two different people who share a title+company.
"""
from __future__ import annotations

import hashlib
import re

from nexus.core.tenancy import TenantSession
from nexus.models.network import NetworkPerson


def normalize_email(email: str | None) -> str:
    return (email or "").strip().lower()


def _norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _domain_of(email: str | None) -> str:
    e = normalize_email(email)
    return e.split("@", 1)[1] if "@" in e else ""


def resolution_key(*, email: str | None, name: str | None, company: str | None) -> str:
    e = normalize_email(email)
    if e:
        return e
    return "h:" + hashlib.sha1(f"{_norm(name)}|{_norm(company)}".encode()).hexdigest()


def search_text_for(name: str, title: str, company: str) -> str:
    # Cap at NetworkPerson.search_text width (600) so real-world provider strings can never
    # overflow the VARCHAR on Postgres (SQLite doesn't enforce it, but prod does).
    return " ".join(p for p in (name.strip(), title.strip(), company.strip()) if p).lower()[:600]


async def resolve_person(
    ts: TenantSession,
    *,
    email: str | None,
    name: str | None,
    title: str | None,
    company: str | None,
) -> NetworkPerson:
    """Find-or-create the canonical person for a raw identity. Idempotent."""
    email_n = normalize_email(email)
    name_n, company_n = _norm(name), _norm(company)

    if email_n:
        hit = await ts.first(NetworkPerson, NetworkPerson.primary_email == email_n)
        if hit is not None:
            return hit
    elif name_n:
        # Emailless dedup — the conservative, uncommon path: reuse a prior emailless person with the
        # same normalized name AND company. Bounded scan: 500 is a generous cap for one tenant's
        # no-email backlog; past it an identity silently falls through to "create new" (under-dedup,
        # never an error). Company-only (no name) is intentionally NOT deduped — matching on company
        # alone would wrongly merge distinct colleagues.
        for cand in await ts.list(
            NetworkPerson, NetworkPerson.primary_email.is_(None), limit=500
        ):
            if _norm(cand.full_name) == name_n and _norm(cand.company) == company_n:
                return cand

    person = NetworkPerson(
        primary_email=email_n or None,
        full_name=name or "",
        title=title or "",
        company=company or "",
        company_domain=_domain_of(email),
        search_text=search_text_for(name or "", title or "", company or ""),
    )
    ts.add(person)
    await ts.flush()
    return person
