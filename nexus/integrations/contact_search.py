"""Net-new contact discovery: find a buying-committee person for an account.

The offline default returns one deterministic stub persona so the sourcing path is fully
exercisable with zero network and reproducible test counts. Real providers (Apollo / InfoJoy /
ZoomInfo) slot in behind this same ABC later, wired through ``contact_search_sources``.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field

from nexus.models.account import Account


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
