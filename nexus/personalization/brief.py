"""Per-contact personalization brief — the shared person-level context both the email (messaging)
and call-script agents use, so a message is written to the *person*, not just the account.

It combines: the contact's own attributes (title/seniority/role angle), the most relevant signal
(preferring one tied to this person over a generic account one), and any social insights fetched
by a personalization provider (Apify etc.) and stored on ``contact.custom_fields['personalization']``.
"""
from __future__ import annotations

from dataclasses import dataclass

# Role-aware angle: shape the message to what this persona actually cares about. First keyword
# match wins (checked against title + seniority, lowercased).
# Most specific persona first (a RevOps lead gets the ops angle, not the generic leadership one).
_ROLE_ANGLES: list[tuple[tuple[str, ...], str]] = [
    (("revops", "rev ops", "sales ops", "revenue operations", "operations"),
     "process efficiency, tooling fit, and data quality"),
    (("ceo", "founder", "co-founder", "owner", "president", "chief executive"),
     "executive outcomes — growth, risk, and the board narrative; keep it strategic and brief"),
    (("cro", "cfo", "cmo", "cto", "chief", "vp", "vice president", "svp", "head of", "director"),
     "function-level impact + ROI; speak to their team's KPIs, not features"),
    (("manager", "lead", "team lead"),
     "operational quick wins and making their team's day easier"),
]
_DEFAULT_ANGLE = "relevance to their specific role and current priorities"


def _role_angle(title: str | None, seniority: str | None) -> str:
    hay = f"{title or ''} {seniority or ''}".lower()
    for needles, angle in _ROLE_ANGLES:
        if any(n in hay for n in needles):
            return angle
    return _DEFAULT_ANGLE


def _best_signal(contact, signals: list):
    """Prefer a signal tied to THIS person; fall back to the strongest account-level one."""
    personal = [s for s in signals if getattr(s, "contact_id", None) == contact.id]
    pool = personal or signals
    sig = max(pool, key=lambda s: s.strength, default=None)
    return sig, bool(sig is not None and getattr(sig, "contact_id", None) == contact.id)


@dataclass(slots=True)
class PersonBrief:
    name: str
    title: str | None
    seniority: str | None
    linkedin_url: str | None
    role_angle: str
    signal_title: str | None
    signal_is_personal: bool
    insights: dict | None  # contact.custom_fields['personalization'] (Apify-fetched), if any

    def to_prompt(self, *, max_posts: int = 3) -> str:
        """A compact instruction block the LLM uses to write to this specific person."""
        lines = [f"Write to {self.name}"]
        if self.title:
            lines[0] += f", {self.title}"
        if self.seniority:
            lines[0] += f" ({self.seniority})"
        lines[0] += "."
        lines.append(f"Angle for their role: {self.role_angle}.")
        if self.signal_title:
            whose = "they personally" if self.signal_is_personal else "their company"
            lines.append(f"Open on what's timely: {whose} — {self.signal_title}.")
        ins = self.insights or {}
        if ins.get("headline"):
            lines.append(f"Their headline: {ins['headline']}.")
        posts = (ins.get("recent_posts") or [])[:max_posts]
        if posts:
            lines.append("Reference, naturally, their recent activity: " + " | ".join(posts) + ".")
        interests = ins.get("interests") or []
        if interests:
            lines.append("Interests: " + ", ".join(interests) + ".")
        lines.append("Make it specific to them; never generic.")
        return " ".join(lines)


def build_person_brief(contact, account, signals: list) -> PersonBrief:
    insights = (contact.custom_fields or {}).get("personalization")
    sig, is_personal = _best_signal(contact, signals)
    return PersonBrief(
        name=contact.full_name,
        title=contact.title,
        seniority=contact.seniority,
        linkedin_url=contact.linkedin_url,
        role_angle=_role_angle(contact.title, contact.seniority),
        signal_title=sig.title if sig is not None else None,
        signal_is_personal=is_personal,
        insights=insights if isinstance(insights, dict) else None,
    )
