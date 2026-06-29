"""Deterministic connection-strength (0–100), materialized on the edge at ingest.

No LLM, pure function — mirrors RelevanceEngine.score_icp_fit. Blend of relationship tier, recency
of the last touch, interaction frequency, and reciprocity (two-way contact).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from nexus.core.db import ensure_aware

_TIER = {"linkedin_1st": 40, "contact": 30, "calendar": 25, "email": 20, "follower": 10}
_DEFAULT_TIER = 15  # unknown relation
_RECENCY_BUCKETS = ((30, 30), (90, 20), (365, 10))  # (max_age_days, bonus) — first match wins
_FREQ_PER_EMAIL = 2
_FREQ_PER_MEETING = 5
_FREQ_CAP = 25
_RECIPROCITY_BONUS = 15  # both directions of contact present


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
    score = _TIER.get(stats.relation, _DEFAULT_TIER)

    days = _age_days(stats.last_touch_at, now=now)
    if days is not None:
        for max_age, bonus in _RECENCY_BUCKETS:  # first bucket the age falls into
            if days <= max_age:
                score += bonus
                break

    score += min(_FREQ_CAP, _FREQ_PER_EMAIL * stats.email_count + _FREQ_PER_MEETING * stats.meeting_count)
    if stats.sent_count > 0 and stats.received_count > 0:
        score += _RECIPROCITY_BONUS

    return max(0, min(100, score))
