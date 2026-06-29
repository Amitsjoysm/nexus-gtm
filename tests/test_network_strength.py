from __future__ import annotations

from datetime import datetime, timedelta, timezone


def test_score_edge_blends_tier_recency_frequency_reciprocity():
    from nexus.network.strength import EdgeStats, score_edge

    now = datetime(2026, 6, 29, tzinfo=timezone.utc)

    # cold contact, no touchpoints → tier only (contact=30)
    assert score_edge(EdgeStats(relation="contact"), now=now) == 30

    # recent two-way email thread: email tier 20 + recency(<=30d) 30 + freq(min(25, 2*4)) 8
    #   + reciprocity 15 = 73
    recent = EdgeStats(
        relation="email", email_count=4, sent_count=2, received_count=2,
        last_touch_at=now - timedelta(days=5),
    )
    assert score_edge(recent, now=now) == 73

    # strong linkedin + heavy frequency clamps the frequency boost at 25 and total at 100
    strong = EdgeStats(
        relation="linkedin_1st", email_count=100, sent_count=50, received_count=50,
        meeting_count=20, last_touch_at=now - timedelta(days=1),
    )
    assert score_edge(strong, now=now) == 100

    # stale relationship (>1y) gets no recency boost
    stale = EdgeStats(relation="contact", last_touch_at=now - timedelta(days=800))
    assert score_edge(stale, now=now) == 30
