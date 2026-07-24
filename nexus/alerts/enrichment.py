"""Alert intelligence — turn a bare signal into an actionable, categorized, scored alert.

Today a play-fired alert carries only ``{"play": name}`` in ``meta``. This module computes the
structured intelligence an SDR needs to act — category, importance, confidence, matched-ICP,
reason, summary, source URL, suggested action, and next best action — and returns it as a plain
dict to MERGE into ``Alert.meta``. Because ``AlertOut`` already exposes ``meta``, the UI and
webhooks get these fields with no schema change and no API break.

It is deterministic (no LLM, no network) so it is fast, free, and fully testable; an LLM insight
step can layer on later behind a flag. Never raises — a missing field degrades to a sane default.
"""
from __future__ import annotations

from nexus.models.account import Account
from nexus.models.signal import SignalEvent

# Per-signal-kind playbook: category + the two actions an SDR cares about. First-party kinds
# (web_visit/product_usage/call) are the hottest; 3rd-party events are grouped by intent.
_PLAYBOOK: dict[str, dict[str, str]] = {
    "funding": {
        "category": "Funding",
        "suggested_action": "Congratulate on the raise and tie your value to their growth plans.",
        "next_best_action": "Draft a funding-triggered email and enroll in the New-Funding cadence.",
    },
    "job_posting": {
        "category": "Hiring",
        "suggested_action": "Reference the specific role as proof of an active initiative.",
        "next_best_action": "Reach the hiring manager's leader with a 15-minute intro offer.",
    },
    "hiring": {
        "category": "Hiring",
        "suggested_action": "Note the headcount growth and the pain it creates for your buyer.",
        "next_best_action": "Enroll the department head in a growth-pain cadence.",
    },
    "news": {
        "category": "News",
        "suggested_action": "Open with the announcement to show you're paying attention.",
        "next_best_action": "Personalize the first touch around the news and send today.",
    },
    "g2_intent": {
        "category": "Buying Intent",
        "suggested_action": "They're researching your category — reach out while intent is hot.",
        "next_best_action": "Move to the top of today's queue; lead with the comparison angle.",
    },
    "tech_install": {
        "category": "Technographic",
        "suggested_action": "They adopted a complementary technology — position the integration.",
        "next_best_action": "Send the integration one-pager to the technical champion.",
    },
    "job_switch": {
        "category": "Champion Move",
        "suggested_action": "Your champion changed companies — re-engage them at the new account.",
        "next_best_action": "Open the new account and request a warm intro.",
    },
    "web_visit": {
        "category": "First-Party Visit",
        "suggested_action": "They visited your site — strike while you're top of mind.",
        "next_best_action": "Trigger a same-day outreach referencing the pages viewed.",
    },
    "product_usage": {
        "category": "Product Usage",
        "suggested_action": "Active product usage signals expansion or a champion — engage.",
        "next_best_action": "Loop in the account owner for an expansion conversation.",
    },
    "call": {
        "category": "Conversation",
        "suggested_action": "Follow up on the recent call while context is fresh.",
        "next_best_action": "Log the outcome and schedule the next step.",
    },
}
_DEFAULT = {
    "category": "Signal",
    "suggested_action": "Review the signal and decide whether to reach out.",
    "next_best_action": "Add to the account's activity and prioritize by fit.",
}


def _importance(strength: float, fit: int) -> int:
    """0..100 blend: signal strength (how big the event) + ICP fit (how much we care)."""
    return max(0, min(100, round(strength * 50 + fit * 0.4)))


def _confidence(source: str, strength: float) -> float:
    """Higher for a real, citable source (web_news/rss) than a synthetic one (demo/stub)."""
    base = 0.3 if source in ("demo", "stub") else 0.6
    return round(min(1.0, base + strength * 0.3), 2)


def build_alert_intelligence(
    account: Account, signal: SignalEvent, composite: int | None
) -> dict:
    """Structured, actionable fields to merge into ``Alert.meta``. Deterministic; never raises."""
    strength = float(signal.strength if signal.strength is not None else 0.5)
    fit = int(composite) if composite is not None else 50
    play = _PLAYBOOK.get(signal.kind, _DEFAULT)
    category = play["category"]
    importance = _importance(strength, fit)

    industry = account.industry or "Account"
    size = f"{account.employee_count} emp" if account.employee_count else "size n/a"
    matched_icp = f"{industry} · {size} · ICP fit {fit}/100"
    reason = (
        f"{category} signal on {account.name} at strength {round(strength * 100)}% "
        f"with ICP fit {fit}/100."
    )
    summary = (signal.body or signal.title or "").strip()[:500]
    insight = (
        f"{account.name}'s {category.lower()} signal scores {importance}/100 "
        f"(strength {round(strength * 100)}%, fit {fit}). {play['suggested_action']}"
    )
    return {
        "category": category,
        "importance": importance,
        "confidence": _confidence(signal.source or "", strength),
        "matched_icp": matched_icp,
        "reason": reason,
        "summary": summary,
        "source": signal.source or "",
        "source_url": signal.url or "",
        "signal_kind": signal.kind,
        "suggested_action": play["suggested_action"],
        "next_best_action": play["next_best_action"],
        "ai_insight": insight,
        "occurred_at": signal.occurred_at.isoformat() if signal.occurred_at else None,
    }
