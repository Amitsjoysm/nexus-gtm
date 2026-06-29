"""Warm-intro mapping (A4): for a target person, who on the team can broker an intro, ranked.

Effectively 1-hop in the team-pool model — the brokers are the *members* who hold a visible edge
to the person. One index seek on (tenant_id, person_id, pooling_enabled, strength).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from nexus.core.tenancy import TenantSession
from nexus.models.network import NetworkEdge
from nexus.network.service import visible_edges_where


@dataclass(slots=True)
class IntroPath:
    broker_member_id: str
    broker_user_id: str
    relation: str
    strength: int
    last_touch_at: datetime | None
    provider: str


async def intro_paths(
    ts: TenantSession, *, person_id: str, member_id: str
) -> list[IntroPath]:
    edges = list(
        (await ts.session.scalars(
            ts.select(NetworkEdge, NetworkEdge.person_id == person_id, visible_edges_where(member_id))
            .order_by(NetworkEdge.strength.desc(), NetworkEdge.last_touch_at.desc())
        )).all()
    )
    return [
        IntroPath(
            broker_member_id=e.owner_member_id, broker_user_id=e.owner_user_id,
            relation=e.relation, strength=e.strength, last_touch_at=e.last_touch_at,
            provider=e.provider,
        )
        for e in edges
    ]
