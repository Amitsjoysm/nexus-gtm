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


class StubContactSearchProvider(ContactSearchProvider):
    """Deterministic offline persona: one candidate, titled from the ICP's first buyer title."""

    name = "stub"

    async def search(
        self, account: Account, icp: dict, *, limit: int = 3
    ) -> list[ContactCandidate]:
        buyer_titles = (icp or {}).get("buyer_titles") or []
        title = buyer_titles[0] if buyer_titles else "Decision Maker"
        return [
            ContactCandidate(
                full_name=f"{account.name} Lead",
                title=title,
                email=None,
                source=self.name,
                confidence=0.3,
            )
        ]
