# nexus/agents/email_quality.py
"""Check a drafted email before a rep ever sees it.

Reported 2026-09-16: drafts arrived with no salutation and did not read like a real SDR wrote them.
The prompt asked for a subject and a body and nothing else, so a model that opened with the
observation produced an email starting mid-thought.

**A rule in a prompt is a request; a check is a guarantee.** The same instruction is followed on one
generation and dropped on the next — measured on this deployment's own reasoning model, which
returns an empty body outright when its budget runs out. So the draft is inspected, and
`agents/messaging.py` regenerates once with the specific complaints attached.

Deliberately NOT a quality score. Every rule here is something a person can point at in the text:
missing greeting, wrong name, no ask, no sign-off, too long, a banned phrase. Anything softer would
reject good drafts for reasons nobody could act on.
"""
from __future__ import annotations

import re

from nexus.agents.copy import EMAIL_WORD_CAP

#: The tells that say "this was automated" louder than anything else in the message. Kept in step
#: with the banned list in `EMAIL_RULES`; a phrase is here only if a reader would notice it.
BANNED_PHRASES = (
    "hope this finds you well",
    "i wanted to reach out",
    "circling back",
    "touching base",
    "synergy",
    "game-changer",
    "revolutionary",
)

#: Openers a human uses. "Dear" is included because it is formal rather than wrong.
_GREETINGS = ("hi", "hello", "hey", "dear", "good morning", "good afternoon")

#: A sign-off line, which is what separates a message from a memo.
_SIGN_OFFS = (
    "best", "thanks", "thank you", "cheers", "regards", "kind regards", "best regards",
    "speak soon", "all the best",
)

#: What asking looks like. A CTA is either a question or an imperative invitation; `CTA_RULE` in
#: copy.py governs its QUALITY, this only asks that one exists at all.
_ASK_MARKERS = (
    "?", "worth a", "open to", "are you free", "let me know", "happy to", "shall i", "can i send",
    "would you like", "grab 15", "book ", "pick a time",
)

#: Allow the model a little room above the cap before complaining: the rule says "under 90 words"
#: and a 92-word draft is not the failure this check exists for.
_WORD_SLACK = 15


def _words(text: str) -> int:
    return len([w for w in re.split(r"\s+", text.strip()) if w])


def check_draft(*, subject, body, first_name) -> list[str]:
    """Problems with this draft, in the order a reader would notice them. Empty means it is fine.

    Never raises: it is called on whatever the model returned, including None.
    """
    problems: list[str] = []
    text = str(body or "").strip()
    head = str(subject or "").strip()
    name = str(first_name or "").strip()

    if not head:
        problems.append("The subject line is missing; the first line must read 'Subject: ...'.")
    if not text:
        problems.append("The body is empty.")
        return problems

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    first_line = lines[0].lower() if lines else ""
    greeted = any(first_line.startswith(g) for g in _GREETINGS)
    if not greeted:
        problems.append(
            "There is no greeting. Open with a greeting line addressing the person by first name, "
            "for example 'Hi Sam,'."
        )
    elif name and name.lower() not in first_line:
        # The right email to the wrong name is mail-merge's most expensive failure.
        problems.append(
            f"The greeting does not use the recipient's first name ({name}); address them by it."
        )

    lowered = text.lower()
    if not any(marker in lowered for marker in _ASK_MARKERS):
        problems.append(
            "There is no ask. Close with one specific, low-friction next step they can accept or "
            "decline in a word."
        )

    tail = " ".join(lines[-2:]).lower() if lines else ""
    if not any(tail.startswith(s) or f"\n{s}" in lowered[-80:] or s in tail for s in _SIGN_OFFS):
        problems.append("There is no sign-off. End with a short sign-off line such as 'Best,'.")

    if _words(text) > EMAIL_WORD_CAP + _WORD_SLACK:
        problems.append(
            f"The email is longer than {EMAIL_WORD_CAP} words; cut it to the observation, one "
            "value line and the ask."
        )

    for phrase in BANNED_PHRASES:
        if phrase in lowered:
            problems.append(f"Remove the phrase '{phrase}' — it is the tell that this is automated.")

    return problems
