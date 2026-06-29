"""Deterministic connection-strength (0–100), materialized on the edge at ingest.

No LLM, pure function — mirrors RelevanceEngine.score_icp_fit. Blend of relationship tier, recency
of the last touch, interaction frequency, and reciprocity (two-way contact).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from nexus.core.db import ensure_aware

_TIER = {"linkedin_1st": 40, "contact": 30, "calendar": 25, "email": 20, "follower": 10}


@dataclass(slots=True)
class EdgeStats:
    relation: str
    email_count: int = 0
    sent_count: int = 0
    received_count: int = 0
    meeting_count: int = 0
    last_touch_at: datetime | None = None


def _age_days(at: datetime | None, *, now: datetime) -> int | None:
    at = ensure_aware(at)
    if at is None:
        return None
    return max(0, (now - at).days)


def score_edge(stats: EdgeStats, *, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    score = _TIER.get(stats.relation, 15)

    days = _age_days(stats.last_touch_at, now=now)
    if days is not None:
        if days <= 30:
            score += 30
        elif days <= 90:
            score += 20
        elif days <= 365:
            score += 10

    score += min(25, 2 * stats.email_count + 5 * stats.meeting_count)  # frequency
    if stats.sent_count > 0 and stats.received_count > 0:
        score += 15  # reciprocity

    return max(0, min(100, score))
