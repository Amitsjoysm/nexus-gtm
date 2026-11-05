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

The complaints describe what to write and never quote an example (2026-10-29): they are sent back
to the model on the retry, and a quoted example there is copied as readily as one in the rules.
"""
from __future__ import annotations

import re

from nexus.agents.copy import (
    BANNED_PHRASES,  # noqa: F401 - re-exported: the prompt and this check read one list
    CALL_BANNED_PHRASES,
    CALL_OPENER_WORDS,
    CALL_VOICEMAIL_WORDS,
    EMAIL_WORD_CAP,
    FIRST_REACTIONS,
    THREADED_TOUCHES,
    WARM_TOUCHES,
)

#: A greeting is a WORD at the start of the first line, never a prefix: "startswith('hi')" accepted
#: "Hiring 12 engineers..." and "Historically..." as greetings (content writers, 2026-10-29).
_GREETING = re.compile(r"^(?:hi|hello|hey|dear|good (?:morning|afternoon|evening))\b", re.I)

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

#: Below this a body may sit in one paragraph: a two-line note needs no breaks.
_PARAGRAPH_FLOOR = 40

#: What a meeting request looks like in the ask line of an email nobody has answered yet: a
#: duration, a weekday, or the words that name a meeting. Read on the ASK only, never the whole
#: body, so "cut reporting from three hours to 20 minutes" is not mistaken for one.
_MEETING_ASK = re.compile(
    r"\b\d+\s*-?\s*min(?:ute)?s?\b|\b(?:call|meeting|demo|calendar|zoom)\b"
    r"|\b(?:monday|tuesday|wednesday|thursday|friday)\b",
    re.IGNORECASE,
)

#: A problem stated as a fact about THEM (content writers, 2026-10-29): nothing in the context can
#: support "your team is struggling with X", and the pressure to write one is what invents a fact.
_CLAIM_ABOUT_THEM = re.compile(
    r"\b(?:you(?:'re|’re| are)|your (?:team|teams|reps|company|people|process|org|"
    r"organisation|organization) (?:is|are))\s+(?:probably\s+|likely\s+|clearly\s+|"
    r"currently\s+|already\s+|still\s+)?(?:struggling|having trouble|wasting|losing|drowning|"
    r"stuck|behind|overwhelmed|frustrated|missing out)\b",
    re.IGNORECASE,
)

#: Their size or revenue said out loud (`FIRMOGRAPHICS_RULE`): a headcount or headcount band, an
#: "N-person" company, a revenue band or "revenue of $...". A number of engineers being hired is a
#: signal and is not matched; neither is a funding amount.
_FIRMOGRAPHIC = re.compile(
    # "employees", never "staff": "hiring 40 support staff" is a signal, not their size.
    r"\b\d[\d,]*\+?(?:\s*(?:-|–|to)\s*\d[\d,]*\+?)?[\s-]*employees?\b"
    r"|\b\d[\d,]*-person\b|\bheadcount of\b"
    r"|\$[\d.,]+\s*(?:k|m|mm|b|bn|million|billion)?\s*(?:-|–|to)\s*\$?[\d.,]+\s*"
    r"(?:k|m|mm|b|bn|million|billion)?\s+(?:in\s+)?(?:revenue|turnover|arr)\b"
    r"|\b(?:revenue|turnover) of\s+(?:about\s+|around\s+|over\s+|under\s+)?\$"
    r"|\b(?:under|over)\s+\$[\d.,]+\s*(?:m|b|bn|million|billion)\s+(?:in\s+)?(?:revenue|turnover)\b",
    re.IGNORECASE,
)

#: Six words in a row from their post is a quote, however it is punctuated.
_QUOTE_RUN = 6

_WORD_RE = re.compile(r"[a-z0-9']+")


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


def _banned_in(lowered: str, phrases) -> list[str]:
    """The banned phrases present, matched from a word start ("streamline" finds "streamlined",
    nothing finds "mainstream").

    The apostrophe is matched as either straight or curly: models write both, and "I’d love to"
    must not pass because the list spells it "i'd"."""
    found = []
    text = lowered.replace("’", "'")
    for phrase in phrases:
        if re.search(rf"(?<![a-z0-9]){re.escape(phrase)}", text):
            found.append(phrase)
    return found


def quoted_activity(text: str, activity) -> bool:
    """True when ``text`` repeats `_QUOTE_RUN` consecutive words of one of their posts."""
    body = _WORD_RE.findall((text or "").lower().replace("’", "'"))
    grams = {tuple(body[i:i + _QUOTE_RUN]) for i in range(len(body) - _QUOTE_RUN + 1)}
    for post in (activity or []):
        words = _WORD_RE.findall(str(post or "").lower().replace("’", "'"))
        for i in range(len(words) - _QUOTE_RUN + 1):
            if tuple(words[i:i + _QUOTE_RUN]) in grams:
                return True
    return False


# ---- the fact verifier (content writers, 2026-10-29) -------------------------------------------
#
# "Use only facts given above" is a request; this checks it. Every number and every proper noun in
# a draft must appear in what the model was given (the grounding: product context, value props,
# account facts, signals, the person, the conversation). A house-style sample is NOT grounding:
# "We helped Globex cut onboarding 40%" in a sample is a fact about another deal, and copied into a
# draft for a different buyer it is a fabricated case study. A miss costs one regeneration, and on
# an automatic follow-up it holds the draft for review (`advance.py` holds any draft with problems).
#
# Built for precision, because a false alarm holds a real follow-up:
# * a proper noun is a capitalised word that does not start its sentence, or a word with a capital
#   or digit inside it (HubSpot, SOC2) anywhere. A plain capitalised word starting a sentence is not
#   checked: telling "Globex" from "Congrats" there needs a dictionary;
# * weekdays, months, time zones and the acronyms every sales email uses are always allowed, and
#   so are the contact's, the sender's and the company's names;
# * dates, clock times and (in the ask, or anywhere in a call script) durations are not facts about
#   the buyer, and are not checked.
# * a number keeps its KIND: "40%" needs a percentage of 40 in the context and "$40M" an amount of
#   40, while a plain "12" matches any 12. Comparing digits alone let "Globex saw 40% faster
#   onboarding" pass on an account that had raised $40M (found writing the test, 2026-10-29).
# * `strict` (the P.S., which is dropped rather than retried) also checks a capitalised word that
#   opens a sentence, unless it is one of the common openers in `_STARTERS`.

_ALWAYS_ALLOWED = frozenset("""
monday tuesday wednesday thursday friday saturday sunday mon tue tues wed thu thur thurs fri sat sun
january february march april may june july august september october november december
jan feb mar apr jun jul aug sep sept oct nov dec
utc gmt et pt ct mt cet cest bst ist est edt pst pdt aest am pm
i i'm i'd i've i'll p.s ps re fyi asap ok u.s u.k e.g i.e
ai crm sdr sdrs bdr bdrs ae aes roi b2b b2c saas gtm ceo cfo cto coo cro cmo cio ciso cpo vp vps
svp evp hr it api apis us usa uk eu emea apac latam anz q1 q2 q3 q4 h1 h2 icp kpi kpis okr okrs
revops salesops faq pdf csv smb arr mrr sla slas rfp erp hse ehs qhse ops linkedin
""".split())

#: Words that open a sentence and name nothing, for `strict` checking.
_STARTERS = frozenset("""
a an the and but or so if when while since after before as at by for from in into of on to with
congrats congratulations saw seen noticed loved liked read heard curious also one quick just
your you our we they their this that these those it its there here what how why who which
great nice good thanks thank happy glad worth open would could should can will may might
""".split())

_SEGMENT = re.compile(r"(?<=[.!?:;])\s+|\n+")
_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9&'’.]*")
_NUMBER = re.compile(
    r"(?<![A-Za-z0-9])(?P<cur>[$£€]\s?)?(?P<n>\d+(?:[.,]\d+)*)"
    r"(?:\s?(?P<unit>%|percent\b|per\s?cent\b|k\b|mm\b|m\b|bn\b|b\b|million\b|billion\b|"
    r"thousand\b))?",
    re.IGNORECASE,
)
_PERCENT_UNITS = ("%", "percent", "per cent")

_MONTHS = ("january|february|march|april|may|june|july|august|september|october|november|"
           "december|jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec")
_DAYS = "monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|wed|thu|fri"
_NOT_FACTS = re.compile(
    rf"\b\d{{1,2}}(?::\d{{2}})?\s*(?:am|pm|a\.m\.|p\.m\.)"                  # 10am, 2:30 pm
    rf"|\b\d{{1,2}}:\d{{2}}\b"                                               # 14:30
    rf"|\b(?:{_DAYS}|{_MONTHS}),?\s+\d{{1,2}}(?:st|nd|rd|th)?\b"             # Thursday 10, Oct 3
    rf"|\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?(?:{_MONTHS})\b",            # 10 September
    re.IGNORECASE,
)
_DURATION = re.compile(
    r"\b\d+\s*-?\s*(?:sec|secs|second|seconds|min|mins|minute|minutes|hour|hours|hr|hrs)\b",
    re.IGNORECASE,
)


def _numbers(text: str) -> list[tuple[str, str, str]]:
    """``(value, kind, as written)`` for each number: kind is ``pct``, ``money`` or ``plain``."""
    out = []
    for match in _NUMBER.finditer(text or ""):
        unit = " ".join((match.group("unit") or "").lower().split())
        kind = ("pct" if unit in _PERCENT_UNITS
                else "money" if match.group("cur") or unit else "plain")
        out.append((match.group("n").replace(",", ""), kind, match.group(0).strip()))
    return out


def _number_given(value: str, kind: str, known: set[tuple[str, str]]) -> bool:
    if kind == "plain":
        return any(v == value for v, _ in known)
    return (value, kind) in known


def _allowed_words(allow) -> set[str]:
    words: set[str] = set()
    for name in (allow or []):
        for word in re.findall(r"[A-Za-z][A-Za-z'’.]*", str(name or "")):
            words.add(word.lower().strip(".").replace("’", "'"))
    return words


def _proper_nouns(text: str, *, strict: bool = False) -> list[str]:
    found: list[str] = []
    for segment in _SEGMENT.split(text or ""):
        tokens = _TOKEN.findall(segment)
        for position, token in enumerate(tokens):
            for part in re.split(r"[-/]", token):
                word = re.sub(r"['’]s$", "", part.strip(".'’"))
                if len(word) < 2 or not word[0].isalpha():
                    continue
                inner = any(c.isupper() for c in word[1:]) or any(c.isdigit() for c in word)
                if position == 0 and not inner and (
                        not strict or word.lower().replace("’", "'") in _STARTERS):
                    continue
                if word[0].isupper() or inner:
                    found.append(word)
    return found


def unverified_terms(text: str, context: str, *, allow=(), durations: str = "",
                     strict: bool = False) -> list[str]:
    """Numbers and proper nouns in ``text`` that ``context`` never mentions, in order, deduplicated.

    ``allow`` holds names always permitted (the contact, the sender, the company). ``durations`` is
    the span in which a duration is not a fact: the ask of an email ("15 minutes" there is the
    meeting length), or the whole of a call script. ``strict`` also checks the word that opens a
    sentence (see above)."""
    context = context or ""
    lowered_context = context.lower().replace("’", "'")
    allowed = _ALWAYS_ALLOWED | _allowed_words(allow)
    out: list[str] = []

    def add(term: str) -> None:
        if term not in out:
            out.append(term)

    for word in _proper_nouns(text, strict=strict):
        key = word.lower().replace("’", "'")
        if key in allowed:
            continue
        if not re.search(rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])", lowered_context):
            add(word)

    source = text or ""
    if durations and durations in source:
        source = source.replace(durations, _DURATION.sub(" ", durations))
    scrubbed = _NOT_FACTS.sub(" ", source)
    known = {(value, kind) for value, kind, _ in _numbers(context)}
    for value, kind, written in _numbers(scrubbed):
        if not _number_given(value, kind, known):
            add(written)
    return out


def unverified_in_draft(*, subject, body, context: str, allow=()) -> dict[str, list[str]]:
    """The verifier's misses, subject and body apart: a threaded touch's subject is replaced by the
    thread's own (`drafting/drafter.py`), so a miss there no longer reaches the buyer."""
    lines = [ln.strip() for ln in str(body or "").splitlines() if ln.strip()]
    return {
        "subject": unverified_terms(str(subject or ""), context, allow=allow),
        "body": unverified_terms(str(body or ""), context, allow=allow,
                                 durations=_ask_line(lines)),
    }


def unverified_problem(terms) -> str:
    return (f"These details are not in the facts given: {', '.join(terms)}. Remove them, or use "
            "a fact that is given; never carry a name or a number over from the example emails.")


# ---- the email check ----------------------------------------------------------------------------

def check_draft(*, subject, body, first_name, facts=None, company_name: str = "",
                touch: str = "", word_cap: int | None = None,
                first_ask: str = "interest", context: str | None = None, allow=(),
                activity=None) -> list[str]:
    """Problems with this draft, in the order a reader would notice them. Empty means it is fine.

    Never raises: it is called on whatever the model returned, including None.

    ``facts`` switches on the personalisation rule (D17): the draft must visibly use one of them.
    Callers that pass none — the composer, the orchestrator — are checked exactly as before.

    ``touch`` (the engagement kind, or "first" for any cold email) switches on the playbook rules
    that depend on where the email sits: the subject's length (not for a threaded touch, whose
    subject is the thread's) and no meeting request until the buyer has replied. Callers that pass
    none are checked as before.

    ``word_cap`` and ``first_ask`` are the workspace's own choices (`email_style.draft_options`): a
    workspace that set a target length is checked against it, and one that chose to ask for a
    meeting in its first emails is not told off for doing so. Defaults are the playbook's.

    ``context`` switches on the fact verifier (`unverified_terms`): what the model was given, minus
    the house-style samples. ``allow`` names it may always use. ``activity`` is the person's posts,
    which the draft must not quote back.
    """
    problems: list[str] = []
    text = str(body or "").strip()
    head = str(subject or "").strip()
    name = str(first_name or "").strip()

    if not head:
        problems.append("The subject line is missing; the first line must read 'Subject: ...'.")
    elif touch and touch not in THREADED_TOUCHES and _words(head) > SUBJECT_WORD_LIMIT:
        problems.append(
            "The subject is a sentence. Use 2 to 4 lowercase words, the way a colleague labels "
            "an internal note about one topic."
        )
    if not text:
        problems.append("The body is empty.")
        return problems

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    first_line = lines[0] if lines else ""
    # "Sam," on a line of its own is a greeting too, the usual one on a follow-up. Refused, it held
    # every such automatic follow-up for review (the end-to-end run, 2026-11-05). Only the name and
    # its punctuation, the whole line: "Sam is hiring" is not a greeting.
    name_only = bool(name) and re.fullmatch(rf"{re.escape(name)}\s*[,:\-–—]", first_line, re.I)
    if not (_GREETING.match(first_line) or name_only):
        problems.append(
            "There is no greeting. Open with a greeting line that addresses the person by their "
            "first name."
        )
    elif name and not re.search(rf"\b{re.escape(name.lower())}\b", first_line.lower()):
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
        problems.append("There is no sign-off. End with a one-word sign-off line and your first "
                        "name.")

    normalised = text.replace("\r\n", "\n")
    if _words(text) > _PARAGRAPH_FLOOR and "\n\n" not in normalised:
        problems.append(
            "The email is one block of text. Break it into short paragraphs separated by blank "
            "lines: the greeting, the reason, the problem, the ask, then the sign-off."
        )

    ask = _ask_line(lines)
    if (touch and touch not in WARM_TOUCHES and first_ask != "meeting"
            and _MEETING_ASK.search(ask)):
        problems.append(
            "The ask requests a meeting. In an email they have not answered, ask one short "
            "interest question they can answer yes or no; times come after they reply."
        )

    cap = word_cap or EMAIL_WORD_CAP
    if _words(text) > cap + _WORD_SLACK:
        problems.append(
            f"The email is longer than {cap} words; cut it to the observation, one "
            "value line and the ask."
        )

    for phrase in _banned_in(lowered, BANNED_PHRASES):
        problems.append(f"Remove the phrase '{phrase}' — it is the tell that this is automated.")

    if _CLAIM_ABOUT_THEM.search(text):
        problems.append(
            "The email tells them what their problem is. State it as what teams in their "
            "situation usually run into, not as a fact about them."
        )

    if _FIRMOGRAPHIC.search(f"{head}\n{text}"):
        problems.append(
            "The email mentions their size or revenue. Those set your register; leave them out."
        )

    if activity and quoted_activity(text, activity):
        problems.append(
            "The email repeats their post word for word. Say what it was about in your own words."
        )

    if context is not None:
        terms = unverified_terms(f"{head}\n{text}", context,
                                 allow=(*tuple(allow or ()), first_name, company_name),
                                 durations=ask)
        if terms:
            problems.append(unverified_problem(terms))

    if facts:
        from nexus.engagement.drafting.personalisation import PROBLEM, uses_a_fact

        if not uses_a_fact(text, list(facts), company_name=company_name):
            problems.append(PROBLEM)

    return problems


# ---- the call script check (content writers, 2026-10-29) ----------------------------------------

#: A placeholder a rep would read out or stop to fill mid-dial: "<your name>", "[Company]".
_PLACEHOLDER = re.compile(
    r"[<\[{]\s*(?:your|insert|first|company|name|rep|caller|contact|prospect)[^>\]}]{0,30}[>\]}]",
    re.IGNORECASE,
)

#: The close the call rules forbid: asking the buyer to judge whether a meeting has value.
_EVALUATING_CTA = re.compile(r"\b(?:valuable|helpful|of interest|worth exploring)\b", re.I)


def script_text(script: dict) -> str:
    """Every spoken line of a script, one per line."""
    script = script or {}
    parts: list[str] = [str(script.get(k) or "") for k in
                        ("opener", "hook", "value_prop", "cta", "voicemail")]
    parts += [str(q or "") for q in (script.get("discovery_questions") or [])]
    for key, fields in (("objections", ("objection", "response")),
                        ("first_reactions", ("reaction", "response"))):
        for item in (script.get(key) or []):
            if isinstance(item, dict):
                parts += [str(item.get(f) or "") for f in fields]
            else:
                parts.append(str(item or ""))
    return "\n".join(p for p in parts if p.strip())


def check_call_script(script: dict, *, context: str | None = None, allow=(),
                      activity=None) -> list[str]:
    """Problems with a talk track a rep would notice while saying it. Empty means it is fine.

    The email check's spoken twin: the opener and voicemail fit the time a buyer listens, the opener
    asks permission, nothing is a placeholder or a stock line, all three first reactions have an
    answer, and (with ``context``) every name and number was given. Never raises."""
    problems: list[str] = []
    script = script if isinstance(script, dict) else {}
    opener = str(script.get("opener") or "").strip()
    voicemail = str(script.get("voicemail") or "").strip()
    text = script_text(script)

    if not opener:
        problems.append("The opener is empty.")
    else:
        if _words(opener) > CALL_OPENER_WORDS + 5:
            problems.append(f"The opener is {_words(opener)} words. Keep it under "
                            f"{CALL_OPENER_WORDS}, about ten seconds spoken.")
        if "?" not in opener:
            problems.append("The opener does not ask permission. Say who you are, that this is a "
                            "cold call, and ask for thirty seconds.")
    if not voicemail:
        problems.append("The voicemail is empty.")
    elif _words(voicemail) > CALL_VOICEMAIL_WORDS + 5:
        problems.append(f"The voicemail is {_words(voicemail)} words. Keep it under "
                        f"{CALL_VOICEMAIL_WORDS}, about twenty-five seconds.")
    if not str(script.get("cta") or "").strip():
        problems.append("There is no close. End with one specific, low-friction ask.")
    elif _EVALUATING_CTA.search(str(script.get("cta"))):
        problems.append("The close asks them to judge whether it is valuable or helpful. Ask for a "
                        "short, specific next step they can accept or decline in a word.")

    reactions = [r for r in (script.get("first_reactions") or [])
                 if isinstance(r, dict) and str(r.get("response") or "").strip()]
    if len(reactions) < len(FIRST_REACTIONS):
        problems.append("Answer each of the three first reactions ("
                        + ", ".join(FIRST_REACTIONS) + ") in one or two sentences.")

    placeholder = _PLACEHOLDER.search(text)
    if placeholder:
        problems.append(f"The script has a placeholder, {placeholder.group(0)}. Use the names given "
                        "above, or leave the detail out.")
    for phrase in _banned_in(text.lower(), (*CALL_BANNED_PHRASES, *BANNED_PHRASES)):
        problems.append(f"Remove the line '{phrase}': it sounds read, or invites a no.")
    if _FIRMOGRAPHIC.search(text):
        problems.append("The script mentions their size or revenue. Leave it out.")
    if activity and quoted_activity(text, activity):
        problems.append("The script repeats their post word for word. Say what it was about in "
                        "your own words.")
    if context is not None:
        terms = unverified_terms(text, context, allow=allow, durations=text)
        if terms:
            problems.append(unverified_problem(terms))
    return problems
