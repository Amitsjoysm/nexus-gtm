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

from nexus.agents.copy import EMAIL_WORD_CAP, THREADED_TOUCHES, WARM_TOUCHES

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
    # A follow-up that adds nothing new (the 2026-10-01 playbook: every touch must earn its place).
    "just following up",
    "just bumping",
    "bumping this",
    "just checking in",
)

#: Openers a human uses. "Dear" is included because it is formal rather than wrong.
_GREETINGS = ("hi", "hello", "hey", "dear", "good morning", "good afternoon")

#: A sign-off line, which is what separates a message from a memo.
_SIGN_OFFS = (
    "best", "thanks", "thank you", "cheers", "regards", "kind regards", "best regards",
    "speak soon", "all the best",
)

#: What asking looks like. A CTA is either a question or an imperative invitation; the CTA rules in
#: copy.py govern its QUALITY, this only asks that one exists at all.
_ASK_MARKERS = (
    "?", "worth a", "open to", "are you free", "let me know", "happy to", "shall i", "can i send",
    "would you like", "grab 15", "book ", "pick a time",
)

#: Allow the model a little room above the cap before complaining: the rule says "under 90 words"
#: and a 92-word draft is not the failure this check exists for.
_WORD_SLACK = 15

#: A subject longer than this is a sentence, not the 2-4 word label `SUBJECT_RULE` asks for. Two
#: words of slack, so a company name of three words does not cost a retry.
SUBJECT_WORD_LIMIT = 6

#: Below this a body may sit in one paragraph: "Hi Sam, worth a look? Best, Jane" needs no breaks.
_PARAGRAPH_FLOOR = 40

#: What a meeting request looks like in the ask line of an email nobody has answered yet: a
#: duration, a weekday, or the words that name a meeting. Read on the ASK only, never the whole
#: body, so "cut reporting from three hours to 20 minutes" is not mistaken for one.
_MEETING_ASK = re.compile(
    r"\b\d+\s*-?\s*min(?:ute)?s?\b|\b(?:call|meeting|demo|calendar|zoom)\b"
    r"|\b(?:monday|tuesday|wednesday|thursday|friday)\b",
    re.IGNORECASE,
)


def _words(text: str) -> int:
    return len([w for w in re.split(r"\s+", text.strip()) if w])


def _ask_line(lines: list[str]) -> str:
    """The sentence that asks: the last question in the email, else nothing. A sentence, not a
    line, because "We cut it to 20 minutes. Worth a look?" is a proof point and an ask on one line,
    and only the second is the ask."""
    for line in reversed(lines):
        if "?" in line:
            sentences = [s for s in re.split(r"(?<=[.!?])\s+", line) if "?" in s]
            return sentences[-1] if sentences else line
    return ""


def check_draft(*, subject, body, first_name, facts=None, company_name: str = "",
                touch: str = "") -> list[str]:
    """Problems with this draft, in the order a reader would notice them. Empty means it is fine.

    Never raises: it is called on whatever the model returned, including None.

    ``facts`` switches on the personalisation rule (D17): the draft must visibly use one of them.
    Callers that pass none — the composer, the orchestrator — are checked exactly as before.

    ``touch`` (the engagement kind, or "first" for any cold email) switches on the playbook rules
    that depend on where the email sits: the subject's length (not for a threaded touch, whose
    subject is the thread's) and no meeting request until the buyer has replied. Callers that pass
    none are checked as before.
    """
    problems: list[str] = []
    text = str(body or "").strip()
    head = str(subject or "").strip()
    name = str(first_name or "").strip()

    if not head:
        problems.append("The subject line is missing; the first line must read 'Subject: ...'.")
    elif touch and touch not in THREADED_TOUCHES and _words(head) > SUBJECT_WORD_LIMIT:
        problems.append(
            "The subject is a sentence. Use 2 to 4 lowercase words, the way a colleague labels a "
            "note (for example 'new sdr pod')."
        )
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

    normalised = text.replace("\r\n", "\n")
    if _words(text) > _PARAGRAPH_FLOOR and "\n\n" not in normalised:
        problems.append(
            "The email is one block of text. Break it into short paragraphs separated by blank "
            "lines: the greeting, the reason, the problem, the ask, then the sign-off."
        )

    if touch and touch not in WARM_TOUCHES and _MEETING_ASK.search(_ask_line(lines)):
        problems.append(
            "The ask requests a meeting. In an email they have not answered, ask one short "
            "interest question instead (for example 'Worth a look?'); times come after they reply."
        )

    if _words(text) > EMAIL_WORD_CAP + _WORD_SLACK:
        problems.append(
            f"The email is longer than {EMAIL_WORD_CAP} words; cut it to the observation, one "
            "value line and the ask."
        )

    for phrase in BANNED_PHRASES:
        if phrase in lowered:
            problems.append(f"Remove the phrase '{phrase}' — it is the tell that this is automated.")

    if facts:
        from nexus.engagement.drafting.personalisation import PROBLEM, uses_a_fact

        if not uses_a_fact(text, list(facts), company_name=company_name):
            problems.append(PROBLEM)

    return problems
