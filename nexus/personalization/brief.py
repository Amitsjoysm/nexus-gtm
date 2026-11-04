"""Per-contact personalization brief — the shared person-level context both the email (messaging)
and call-script agents use, so a message is written to the *person*, not just the account.

It combines: the contact's own attributes (title/seniority/role angle), the most relevant signal
(preferring one tied to this person over a generic account one), and any social insights fetched
by a personalization provider (Apify etc.) and stored on ``contact.custom_fields['personalization']``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# ---- the role angle: seniority AND function (content writers, 2026-10-29) ---------------------
#
# It was one keyword list matched by SUBSTRING against the title, first match wins, so "president"
# in the CEO bucket caught every Vice President, "Product Owner" matched "owner", "Lead Generation
# Specialist" matched "lead", and a VP Sales and a VP Engineering got the same sentence. Now titles
# are read as whole words, the exceptions above are named, and the angle has two halves: how senior
# they are (what kind of outcome to speak to) and what they run (which problems are theirs).

#: Seniority, most senior first. Each needle is matched as whole words.
_SENIORITY: list[tuple[str, tuple[str, ...], str]] = [
    ("executive", ("ceo", "chief executive", "founder", "co-founder", "cofounder", "owner",
                   "president", "managing director", "general manager"),
     "executive outcomes — growth, risk, and the board narrative; keep it strategic and brief"),
    ("leader", ("cro", "cfo", "cmo", "cto", "coo", "cio", "ciso", "cpo", "chief", "vp", "svp",
                "evp", "vice president", "head of", "head", "director"),
     "function-level impact + ROI; speak to their team's KPIs, not features"),
    ("manager", ("manager", "lead", "team lead", "supervisor", "principal"),
     "operational quick wins and making their team's day easier"),
]

#: Words that make a seniority needle NOT a seniority: "Vice President" is not "president", a
#: "Product Owner" owns a backlog, a "Lead Generation Specialist" generates leads.
_NOT_SENIORITY = {
    "president": ("vice president", "vice-president", "vp president"),
    "owner": ("product owner", "process owner", "project owner", "data owner", "account owner",
              "business owner", "system owner", "service owner", "content owner",
              "platform owner", "program owner"),
    "lead": ("lead generation", "lead gen", "lead qualification", "lead nurturing",
             "lead management", "lead routing", "lead development"),
}

#: Function: what they run, and the problems that are theirs. Needles are whole words.
_FUNCTIONS: list[tuple[str, tuple[str, ...], str, str]] = [
    ("revops", ("revops", "rev ops", "revenue operations", "sales operations", "sales ops",
                "marketing operations", "marketing ops", "gtm operations"),
     "process efficiency, tooling fit, and data quality",
     "process tooling systems data quality reporting pipeline hygiene"),
    ("sales", ("sales", "revenue", "business development", "bdr", "sdr", "account executive",
               "cro", "commercial", "partnerships"),
     "pipeline, quota attainment and how fast new reps ramp",
     "pipeline quota prospecting outbound reps selling deals revenue"),
    ("marketing", ("marketing", "demand generation", "demand gen", "lead generation", "lead gen",
                   "growth", "brand", "cmo", "content"),
     "qualified pipeline from campaigns and what each channel really returns",
     "marketing campaigns demand leads conversion channel brand"),
    ("engineering", ("engineering", "engineer", "developer", "development", "software",
                     "technology", "technical", "cto", "devops", "platform", "infrastructure",
                     "architect"),
     "delivery speed, reliability and the team's capacity",
     "engineering engineers developers delivery reliability infrastructure hiring onboarding"),
    ("product", ("product", "cpo"),
     "what customers adopt, and shipping the right thing sooner",
     "product roadmap adoption customers features launch"),
    ("finance", ("finance", "financial", "cfo", "controller", "accounting", "fp&a", "treasury"),
     "cost, forecast accuracy and spend that can be justified",
     "finance spend budget forecast costs reporting"),
    ("operations", ("operations", "ops", "coo", "supply chain", "logistics", "procurement",
                    "plant", "manufacturing", "hse", "ehs", "qhse", "safety", "quality"),
     "uptime, safety and running the operation with fewer surprises",
     "operations efficiency safety compliance uptime processes"),
    ("people", ("people", "hr", "human resources", "talent", "recruiting", "recruitment"),
     "hiring speed, onboarding and keeping good people",
     "hiring recruiting onboarding talent retention people"),
    ("customer", ("customer success", "customer experience", "support", "service", "account "
                  "management"),
     "retention, renewals and customers who get value fast",
     "customers retention renewals onboarding support churn"),
    ("security", ("security", "ciso", "compliance", "risk", "privacy", "governance"),
     "audit readiness, risk and less manual evidence work",
     "security compliance audit risk evidence controls"),
    ("data", ("data", "analytics", "bi", "insights"),
     "trustworthy data and less time cleaning it",
     "data analytics quality pipelines reporting"),
]

_DEFAULT_ANGLE = "relevance to their specific role and current priorities"

# An "About" section runs to several paragraphs. Trimmed rather than dropped, for the same reason a
# long post is: the opening sentences carry who the person says they are, and the whole thing would
# dominate a prompt whose other half is the account's signals.
_MAX_SUMMARY_CHARS = 400


def _trim(value, limit: int) -> str:
    """Collapse whitespace and cut at a sentence end where one is near the limit."""
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    stop = cut.rfind(". ")
    # Only honour a sentence break in the last third, or a profile opening with one short line
    # would be trimmed to that line and lose everything after it.
    return (cut[: stop + 1] if stop > limit * 0.6 else cut.rstrip()) + ""


def _normal(text: str) -> str:
    """Lowercase, '&' and '/' and ',' as spaces, single-spaced, padded so whole-word search is a
    plain substring test on ' word '."""
    text = re.sub(r"[,/|()&]+", " ", (text or "").lower())
    return f" {' '.join(text.split())} "


def _has(hay: str, needle: str) -> bool:
    return f" {needle} " in hay


def _seniority(hay: str) -> tuple[str, str] | None:
    for key, needles, angle in _SENIORITY:
        for needle in needles:
            if not _has(hay, needle):
                continue
            if any(_has(hay, exception) for exception in _NOT_SENIORITY.get(needle, ())):
                continue
            return key, angle
    return None


def _function(hay: str) -> tuple[str, str, str] | None:
    for key, needles, angle, words in _FUNCTIONS:
        if any(_has(hay, n) for n in needles):
            return key, angle, words
    return None


def role_profile(title: str | None, seniority: str | None) -> dict:
    """``{"seniority", "function", "angle", "words"}`` for one person. ``words`` describe the
    problems their function owns, for `copy.select_value_prop`'s tie-break."""
    hay = _normal(f"{title or ''} {seniority or ''}")
    level = _seniority(hay)
    function = _function(hay)
    parts = []
    if level:
        parts.append(level[1])
    if function:
        parts.append(f"for their function: {function[1]}")
    return {
        "seniority": level[0] if level else "",
        "function": function[0] if function else "",
        "angle": "; ".join(parts) if parts else _DEFAULT_ANGLE,
        "words": function[2] if function else "",
    }


def _role_angle(title: str | None, seniority: str | None) -> str:
    return role_profile(title, seniority)["angle"]


def role_words(title: str | None, seniority: str | None) -> str:
    return role_profile(title, seniority)["words"]


def _best_signal(contact, signals: list):
    """Prefer a signal tied to THIS person; fall back to the strongest account-level one."""
    from nexus.agents.copy import pick_anchor

    sig = pick_anchor(signals, contact)
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

    @property
    def posts(self) -> list[str]:
        return [str(p) for p in ((self.insights or {}).get("recent_posts") or []) if p]

    def to_prompt(self, *, max_posts: int = 3, channel: str = "email") -> str:
        """A compact instruction block the LLM uses to write to this specific person.

        `channel` picks the verb. The block opened with "Write to Satya Nadella…" for BOTH agents,
        and a call script is not written to anybody — it is spoken at them. Measured: the call
        agent received a full 1,438-character person block carrying his headline, his About section
        and three of his own posts, and used none of it, while the email agent with the same block
        opened on his own words. Email framing in a prompt that has just been told to produce a
        spoken talk track reads as instructions for a different artefact.
        """
        from nexus.agents.copy import ACTIVITY_RULE

        opener = "You are calling" if channel == "call" else "Write to"
        lines = [f"{opener} {self.name}"]
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
        # The "About" section, which was FETCHED, STORED and never read — the built-stored-and-
        # unreachable pattern this codebase keeps finding. It matters more than it looks: measured
        # against the live LinkedIn actor on two profiles, `recent_posts` and `interests` came back
        # empty both times because that actor scrapes a profile, not an activity feed. Headline and
        # About are what it reliably returns, so dropping About left person-level personalization
        # running on a single line of text.
        summary = _trim(ins.get("summary"), _MAX_SUMMARY_CHARS)
        if summary:
            # An About section usually ends in a full stop of its own; appending another produced
            # "…achieve more.." in the live prompt. Cosmetic in isolation, but this block is read by
            # a model that is being told to write carefully.
            lines.append(f"How they describe themselves: {summary.rstrip('.')}.")
        posts = self.posts[:max_posts]
        if posts:
            # On a CALL the personal reference belongs in the opener or hook, and saying so is what
            # makes it survive: the call agent has many structured fields to fill and, told only to
            # "reference it naturally", it spent its attention on the structure and dropped the
            # person. The named slot stays; "in their own words" went (2026-10-29): it contradicted
            # the email rule against quoting a post back, and both channels now share one rule.
            where = ("Use the idea in the opener or hook. " if channel == "call" else "")
            lines.append(f"{where}{ACTIVITY_RULE} Their recent activity: "
                         + " | ".join(posts) + ".")
        interests = ins.get("interests") or []
        if interests:
            lines.append("Interests: " + ", ".join(interests) + ".")
        lines.append(
            "Make it specific to them; never generic."
            if channel != "call"
            else "Make it specific to them; never generic. It is spoken aloud, so keep every "
                 "reference short enough to say in one breath."
        )
        return " ".join(lines)


def build_person_brief(contact, account, signals: list, *, anchor=None) -> PersonBrief:
    """``anchor`` is the one signal the draft is about (`copy.pick_anchor`), when the caller has
    already picked it; the brief then names the same one, so the hook and the brief cannot
    disagree. Without it the brief picks by the same rule."""
    insights = (contact.custom_fields or {}).get("personalization")
    if anchor is not None:
        sig, is_personal = anchor, getattr(anchor, "contact_id", None) == contact.id
    else:
        sig, is_personal = _best_signal(contact, signals)
    return PersonBrief(
        name=contact.full_name,
        title=contact.title,
        seniority=contact.seniority,
        linkedin_url=contact.linkedin_url,
        role_angle=_role_angle(contact.title, contact.seniority),
        signal_title=sig.title if sig is not None else None,
        signal_is_personal=bool(is_personal),
        insights=insights if isinstance(insights, dict) else None,
    )
