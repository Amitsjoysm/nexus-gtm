"""Pure prioritization function for the Intelligent Inbox.

Priority blends the triggering signal's strength with the account's composite score and decays
with the signal's age, so reps always see the most valuable, most timely work first.
"""
from __future__ import annotations


def compute_priority(
    *,
    signal_strength: float,
    composite_score: int | None,
    age_days: float = 0.0,
    seniority_boost: float = 0.0,
) -> int:
    """Return a 0..100 priority.

    - signal_strength: 0..1 intrinsic importance of the trigger
    - composite_score: 0..100 account score (None → neutral 50)
    - age_days: how old the signal is (linear decay over 30 days, floor 0.4)
    - seniority_boost: 0..1 bump when a senior contact is involved
    """
    composite = 50 if composite_score is None else composite_score
    recency = max(0.4, 1.0 - age_days / 30.0)
    base = 0.55 * (signal_strength * 100) + 0.45 * composite
    score = base * recency + seniority_boost * 10
    return max(0, min(100, round(score)))
