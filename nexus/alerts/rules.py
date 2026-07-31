# nexus/alerts/rules.py
"""Signal → alert: which signals are worth interrupting someone for, and what to say.

**The gap this closes.** `signal.created` was published on every ingested signal and *nothing
subscribed to it* — the only subscriber on the bus was CRM sync, listening for `account.scored`.
Alerts were created in exactly three places, none of them from an incoming signal. So the entire
collection pipeline — funding rounds, hiring surges, pricing changes — landed in a table nobody was
notified about, and a rep learned about a customer's Series F by opening the account page and
scrolling.

**Two decisions worth stating.**

*Not every signal is an alert.* An alert costs attention, and attention spent on a weak press
mention is attention not spent on a funding round. Signals below a strength floor are recorded and
visible on the timeline but never interrupt anyone — which is what the existing 0.4 "weak mention"
tier was always for.

*Every alert carries the next action.* "Acme raised a Series B" is information. "Acme raised a
Series B — reach out about scaling before their headcount doubles" is a prompt. The suggested action
is derived from the signal kind, deterministically and without an LLM, so it costs nothing and never
fabricates.
"""
from __future__ import annotations

from dataclasses import dataclass

# Strength below which a signal is recorded but never alerts. 0.4 is the classifier's "weak
# mention" tier — a headline that named the company but described no event.
DEFAULT_ALERT_FLOOR = 0.5

# What a rep should do about each kind of signal, and how loud it is. Deterministic: no LLM call on
# the ingestion hot path, and nothing invented.
#
# `category` is what a user subscribes to in their notification preferences, so these names are
# stable strings — renaming one silently unsubscribes everybody who chose it.
_RULES: dict[str, tuple[str, str, str]] = {
    # kind -> (category, severity, suggested action)
    "funding": (
        "funding", "critical",
        "Reach out now — budget is unlocked and priorities are being set for the next 12 months.",
    ),
    "hiring": (
        "hiring", "warning",
        "Hiring signals budget. Ask which team is growing and what problem the headcount solves.",
    ),
    "job_posting": (
        "hiring", "info",
        "Open roles name the team with budget. Reference the specific role in your opener.",
    ),
    "website_change": (
        "product", "warning",
        "A pricing or positioning change is a live strategy shift — ask what drove it.",
    ),
    "tech_install": (
        "technographic", "info",
        "They adopted a technology in your stack's orbit. Lead with the integration story.",
    ),
    "g2_intent": (
        "intent", "critical",
        "They are actively comparing vendors. Move now — this is a short window.",
    ),
    "web_visit": (
        "intent", "warning",
        "They visited your site. Follow up while you are still top of mind.",
    ),
    "job_switch": (
        "champion", "warning",
        "Your champion moved. Congratulate them, and re-open at the new company.",
    ),
    "product_usage": ("usage", "info", "Usage changed — check whether they are hitting a limit."),
    "news": ("news", "info", "Reference the news in your next touch to show you are paying attention."),
    "call": ("activity", "info", "Log the outcome and set the next step."),
}

# Categories a user can subscribe to. Derived from the rules so the two cannot drift.
ALERT_CATEGORIES: tuple[str, ...] = tuple(sorted({c for c, _s, _a in _RULES.values()}))


@dataclass(slots=True)
class AlertDecision:
    """Whether a signal alerts, and the alert it becomes."""

    should_alert: bool
    category: str = "news"
    severity: str = "info"
    suggested_action: str = ""
    reason: str = ""


def decide(kind: str, strength: float, *, floor: float = DEFAULT_ALERT_FLOOR) -> AlertDecision:
    """Turn a signal into an alert decision.

    An unknown kind is **not** an error and **not** silently dropped: it alerts as `news` at
    whatever strength it carries. A new signal kind that nobody remembered to add a rule for should
    degrade to "tell someone quietly", never to "vanish" — the same bias that makes an unknown
    billing capability resolve to allow.
    """
    category, severity, action = _RULES.get(kind, ("news", "info", ""))
    if strength < floor:
        return AlertDecision(
            should_alert=False, category=category, severity=severity,
            reason=f"strength {strength:.2f} below alert floor {floor:.2f}",
        )
    # A strong signal in a normally-quiet category still deserves to be louder: a 0.9 news item is
    # an acquisition, not a press mention.
    if strength >= 0.85 and severity == "info":
        severity = "warning"
    return AlertDecision(
        should_alert=True, category=category, severity=severity,
        suggested_action=action, reason=f"{kind} at strength {strength:.2f}",
    )


def alert_dedupe_key(category: str, account_id: str, period: str) -> str:
    """Alert-level dedupe, deliberately separate from signal dedupe.

    Signals dedupe on the *event* — one funding round is one signal. Alerts dedupe on **attention**:
    a rep should not be interrupted twice in a day about the same category of thing on the same
    account, even when two genuinely distinct signals arrive. Two different job postings are two
    real signals and one notification.
    """
    return f"alert:{category}:{account_id}:{period}"
