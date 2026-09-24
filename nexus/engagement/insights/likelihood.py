"""How likely a prospect is to reply to YOU (spec §18.5, D26). Pure and deterministic, no model.

Computed from the viewing workspace's own evidence: its ICP fit for the account (the relevance
engine's latest composite score) and the account's recent signals, which are the prospect's
interests as this workspace has observed them. The person's general responsiveness across
workspaces joins only when the display rules allow a pattern (three or more workspaces); below that
threshold even a reply RATE would say that others have been writing to them.

Never another workspace's message content: this module does not receive any.

The weights are stated, not learned: fit carries most of it because it is the one thing the SDR
chose, signals say "now", and responsiveness shifts the answer rather than deciding it.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

FIT_WEIGHT = 0.6
SIGNAL_WEIGHT = 0.4
RESPONSIVENESS_SHARE = 0.3
#: Three recent signals is as much "now" as the score can use.
SIGNALS_FOR_FULL = 3
#: A quarter of outreach answered is very responsive for cold email.
PROPENSITY_FOR_FULL = 0.25
HIGH, MEDIUM = 0.66, 0.4


@dataclass(slots=True)
class Likelihood:
    band: str                 # high | medium | low | unknown
    score: float              # 0..1, for ordering only; the screen shows the band
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def likelihood(*, fit: int | None, recent_signals: int, propensity: float | None) -> Likelihood:
    """``fit`` is the 0–100 composite (None = not scored, read as neutral); ``propensity`` only
    when the display rules produced a pattern for this person.

    With none of the three there is nothing to go on, and the answer says so: a neutral fit alone
    scores 0.3, which would label a contact nobody has looked at yet "less likely to reply"."""
    if fit is None and not recent_signals and propensity is None:
        return Likelihood(band="unknown", score=0.0,
                          reasons=["Not scored against your ICP yet, and no recent signals"])
    reasons: list[str] = []
    fit_part = (fit if fit is not None else 50) / 100
    if fit is None:
        reasons.append("Not scored against your ICP yet")
    elif fit >= 70:
        reasons.append(f"Strong fit for your ICP ({fit})")
    elif fit >= 40:
        reasons.append(f"Partial fit for your ICP ({fit})")
    else:
        reasons.append(f"Weak fit for your ICP ({fit})")
    signal_part = min(1.0, recent_signals / SIGNALS_FOR_FULL)
    if recent_signals:
        reasons.append(f"{recent_signals} {'signal' if recent_signals == 1 else 'signals'} in the "
                       "last 30 days")
    score = FIT_WEIGHT * fit_part + SIGNAL_WEIGHT * signal_part
    if propensity is not None:
        responsive = min(1.0, propensity / PROPENSITY_FOR_FULL)
        score = (1 - RESPONSIVENESS_SHARE) * score + RESPONSIVENESS_SHARE * responsive
        if propensity >= 0.15:
            reasons.append("Replies to more outreach than most")
        elif propensity < 0.03:
            reasons.append("Rarely replies to outreach")
    score = round(score, 3)
    band = "high" if score >= HIGH else "medium" if score >= MEDIUM else "low"
    return Likelihood(band=band, score=score, reasons=reasons)
