# nexus/agents/copy.py
"""Text shaping for agent prompts and the offline draft templates.

Small on purpose. It exists because one variable was carrying two incompatible grammatical forms
and nothing in the type system could notice.
"""
from __future__ import annotations

import re

# How many problems to name in one sentence. Four reads as a list being recited at the prospect;
# two is a sentence. The rest of the value prop still reaches the model through the prompt body.
MAX_PAINS_IN_A_SENTENCE = 2

# A noun phrase, because that is what the templates now need. The old fallback was
# "hit pipeline goals" — a VERB phrase — which is exactly how the mismatch stayed invisible: with
# no value props configured the sentence read correctly, and it only broke for the customers who
# had actually filled the field in.
DEFAULT_PAINS = "the usual pipeline bottlenecks"


def format_pains(pains_solved: list[str] | None) -> str:
    """A list of problems as a fragment that reads inside a sentence.

    ``pains_solved`` holds problem NOUNS ("Stale lists", "Duplicate records"). They used to be
    ``", ".join``-ed straight into ``use {value_prop} to {pains}``, which produced:

        Teams like Marketjoy use Accurate Lead Generation to Stale lists, Duplicate records,
        No signal on in-market accounts, Wasted time chasing wrong leads.

    That is a real email that would have gone to a real prospect. The join is not the bug on its
    own — the bug is that the template's connective ("to") wanted a verb and the data was nouns.
    Fixing only one half would leave the other half free to break again, so the connective moved
    to one that takes nouns ("to get ahead of ...") and this function guarantees the noun form.

    Lowercases the first letter so the fragment sits mid-sentence, but only when the word looks
    like ordinary prose: "SOC2 gaps" and "CRM hygiene" must keep their capitals, and a rule that
    blindly lowercased would quietly mangle every acronym a customer typed.
    """
    items = [p.strip() for p in (pains_solved or []) if p and p.strip()]
    if not items:
        return DEFAULT_PAINS
    items = items[:MAX_PAINS_IN_A_SENTENCE]
    items = [_downcase_lead(p) for p in items]
    if len(items) == 1:
        return items[0]
    return f"{', '.join(items[:-1])} and {items[-1]}"


def first_pain(pains_solved: list[str] | None) -> str:
    """One problem, for places where a list would not fit.

    A discovery question is the clearest case: "How are you handling stale lists today?" is a
    question a person can answer, and "How are you handling stale lists, duplicate records, no
    signal on in-market accounts and wasted time chasing wrong leads today?" is not.
    """
    items = [p.strip() for p in (pains_solved or []) if p and p.strip()]
    return _downcase_lead(items[0]) if items else DEFAULT_PAINS


# ---- prompt rules ------------------------------------------------------------------------------
#
# Distilled from four public GTM prompt libraries (Prospeda/claude-gtm-skills, gtm-skills/gtm,
# gtmagents/gtm-agents, sidchaudhary/gtm-skills). Those repos disagree on plenty, but four rules
# appear in all of them, and each maps to a failure this product can actually have:
#
#   * a hard word cap                 — an unbounded model writes three paragraphs nobody reads
#   * a banned-phrase list            — "hope this finds you well" is the tell that it is automated
#   * observation before ask          — leading with ourselves is the most common cold-email fault
#   * no invented facts               — the one that matters here, because we HAVE the real facts
#
# Their prompt text is not copied: these are the underlying constraints, written against this
# codebase's own grounding (the system message already supplies ICP, value props and account fit).
#
# The output-contract line is not from any of them and is the highest-value addition:
# `_split_subject` has always parsed a leading "Subject:" and the prompt never once asked for one,
# so a model that opened with the body produced an email with a blank subject.

EMAIL_WORD_CAP = 90

OUTPUT_CONTRACT = (
    "Return the email as: a first line reading exactly 'Subject: <subject>', then a blank line, "
    "then the body. No preamble, no commentary, no markdown."
)

#: Characters model output carries that look like ordinary ones and are not: a non-breaking hyphen
#: renders as a box in some fonts, and zero-width marks survive into copy-paste and spam scoring.
_LOOKALIKES = str.maketrans({
    "\u2010": "-", "\u2011": "-", "\u2012": "-",        # hyphen, non-breaking hyphen, figure dash
    "\u00a0": " ", "\u202f": " ", "\u2007": " ", "\u2009": " ",   # non-breaking and thin spaces
    "\u200b": None, "\u200c": None, "\u200d": None, "\u2060": None, "\ufeff": None,
})


#: Spaces at the end of a line: a model writing markdown ends lines with two spaces to force a
#: break (measured on a live draft, 2026-10-01). Invisible in the text part, noise everywhere else.
_LINE_END_SPACE = re.compile(r"[ \t]+(?=\r?\n|$)")


def tidy_text(text: str) -> str:
    """The same words, without look-alike or invisible characters or spaces at line ends. Curly
    quotes and dashes stay: they are ordinary punctuation every client renders."""
    return _LINE_END_SPACE.sub("", (text or "").translate(_LOOKALIKES))


def sender_block(name: str) -> str:
    """Who the email is from, for the prompt. The structure rule asks for a sign-off with "your
    first name" and a model never told it invents one: "Best, Alex" above the rep's own signature
    (reported 2026-09-30, and again on the contact composer 2026-10-01). Empty when no name is known,
    which leaves the send path to sign with the mailbox signature."""
    name = " ".join((name or "").split())
    if not name:
        return ""
    first = name.split()[0]
    return ("YOU (THE SENDER)\n"
            f"- Name: {name}\n"
            f"- Sign off with 'Best,' and your first name, {first}. Write nothing under it: your "
            "signature is added when the email is sent.\n")


# ---------------------------------------------------------------------------------------------
# The email's shape, from the B2B cold email playbook the product owner supplied (2026-10-01).
#
# Its rules that this product can act on: a short lowercase subject that reads like an internal
# note; one trigger that explains why now; the problem it creates, stated as an observation; proof
# only as a real peer result; ONE soft, interest-based ask (never a meeting in a cold email);
# short paragraphs; plain text; every follow-up adds something new and the last one closes the
# loop. The per-touch half lives in `engagement/drafting/context.py`, which knows which touch this
# is, and `agents/email_quality.py` checks the parts a reader can point at.

#: The subject line. A title-case pitch with urgency reads as marketing and is skimmed as
#: marketing; two or three lowercase words read like a colleague's note.
SUBJECT_RULE = (
    "Subject line: 2 to 4 words, lowercase except names and acronyms, the way a colleague labels "
    "an internal note (for example 'eu expansion' or 'new sdr pod'). No urgency, no clickbait, no "
    "question or exclamation marks, and not the recipient's name."
)

#: The shape of the body itself.
#:
#: Reported 2026-09-16: drafts arrived with no salutation. Nothing had ever asked for one — the
#: contract above names the subject and the body, and "open with a specific observation about THEM"
#: was read literally, so the email began mid-thought with no greeting and often no sign-off. A
#: buyer reads those two lines before anything else, and their absence is the first thing that says
#: a machine wrote this.
#:
#: Paragraphs since 2026-10-01: a 90-word block with no blank line is what a reader skims past, and
#: the HTML part (`engagement/sending/mime.py`) turns each blank-line paragraph into a real one.
#:
#: The sign-off is the NAME ONLY: the rep's signature block (title, company, phone) is appended by
#: `nexus/outreach/signature.py` at send time, and a model inventing one would put a made-up title
#: and number under a real person's name.
STRUCTURE_RULE = (
    "Lay the body out as short paragraphs of one or two sentences, separated by one blank line: "
    "a greeting line addressing the recipient by first name ('Hi Sam,'); the reason you are "
    "writing now; the problem that creates for someone in their role, stated as an observation, "
    "and what we remove; one line of proof only if the context names a real customer result; the "
    "ask, on its own line; then a sign-off line ('Best,') with your first name on the line below "
    "it. Do not write a title, company, phone number or any other signature detail under the "
    "sign-off — that is added automatically. Plain text only: no bullet points, bold, headings, "
    "emoji or em dashes, and at most one link, only if the context gives it."
)

#: One signal, used rather than recited. Three signals in one email reads as a dossier, and quoting
#: someone's post back to them reads as surveillance; the playbook's line is "reference the idea".
SIGNAL_RULE = (
    "Use ONE signal: the single fact that best explains why you are writing now, and connect it "
    "to the problem we solve. Say what it means for them rather than reciting it, and never quote "
    "their post or article back to them. Do not pretend to know them, and do not compliment them."
)

#: The ask in an email nobody has answered yet: one interest question, NOT a meeting.
#:
#: This replaced "name a short duration and a rough time" on 2026-10-01, on the playbook's
#: evidence that an interest-based ask ("Worth a look?") is answered far more often than a meeting
#: request in a cold email: a stranger can say yes to a question in one word, and a calendar ask
#: wants half an hour before they know why. What survives from the old rule is its reason: do not
#: make the reader judge whether something is "valuable".
EMAIL_CTA_RULE = (
    "End with ONE short, interest-based question they can answer in a word, such as 'Worth a "
    "look?' or 'Open to seeing how?'. Do not ask for a meeting, call or demo, and do not name a "
    "duration, a day or a time: that comes after they reply. Do not offer to send anything the "
    "context does not mention, and do not ask whether something would be 'valuable', 'helpful' "
    "or 'of interest'."
)

#: The ask in a reply to someone who wrote back. Interest is the moment to make booking easy, and
#: two concrete times beat "when works for you?", which hands the scheduling back to the buyer.
REPLY_CTA_RULE = (
    "If they showed interest, end by offering two specific times in their timezone (weekday, date "
    "and time, worked out from the date given above) and ask which suits. Otherwise end with one "
    "clear next step they can accept or decline in a word."
)

#: The call script's closing ask. A call is already a conversation, so it still books the meeting.
#:
#: **A question is not a CTA.** Measured on live drafts: every one closed with "Would a brief
#: conversation about reducing plant energy spend be valuable for you?" or "...be helpful?" — a
#: question, technically, and one a busy buyer cannot answer. It asks them to evaluate whether a
#: meeting has value rather than to accept a small, specific commitment. Emails moved to
#: `EMAIL_CTA_RULE` on 2026-10-01; this stays for the call script.
CALL_CTA_RULE = (
    "End with ONE specific, low-friction ask: name a short duration and a rough time, or propose "
    "one concrete next step they can accept or decline in a word. Do not ask whether something "
    "would be 'valuable', 'helpful', 'of interest' or 'worth exploring' — those ask the reader to "
    "do the evaluating, and a busy buyer will not."
)

#: The register. Stated because "professional" is what a buyer reads as credible and it is not the
#: default voice a model reaches for on a sales prompt: unprompted it drifts either to breathless
#: marketing or to matey over-familiarity, and both cost the reply.
TONE_RULE = (
    "Tone: professional and plain, the way a competent peer writes to another. Confident without "
    "hype, warm without familiarity. No exclamation marks, no emoji, no flattery."
)

#: Touches that answer someone who wrote to us. Every other touch is still unanswered outreach.
WARM_TOUCHES = frozenset({"response"})

#: Touches sent inside an existing thread, whose subject is the thread's ("Re: ...") whatever the
#: model writes (`drafting/drafter.py`), so a subject rule there would only cost a retry.
THREADED_TOUCHES = frozenset({"followup", "reengage", "response", "signal"})


def email_rules(touch: str = "first") -> str:
    """The rules for one email. ``touch`` is the engagement kind (`first`, `followup`, `signal`,
    `reengage`, `response`); anything else is read as a first email, which is what every caller
    outside the engagement engine (the composer, plays, the orchestrator) is writing."""
    warm = touch in WARM_TOUCHES
    return (
        f"Rules: Under {EMAIL_WORD_CAP} words; a follow-up can be much shorter. Short sentences. "
        f"{STRUCTURE_RULE} "
        f"{'' if touch in THREADED_TOUCHES else SUBJECT_RULE + ' '}"
        f"{'' if warm else SIGNAL_RULE + ' '}"
        "Never open with a pitch or with our company. "
        f"{TONE_RULE} "
        "Do not write 'hope this finds you well', 'I wanted to reach out', 'circling back', "
        "'just following up', 'just bumping', 'just checking in', 'synergy', 'leverage', "
        "'game-changer', or 'revolutionary'. "
        "No more than one question. "
        f"{REPLY_CTA_RULE if warm else EMAIL_CTA_RULE} "
        "Use only facts given above — if a detail is missing, leave it out rather than inventing "
        "it. Never state a metric, customer name or case study that is not in the context."
    )


EMAIL_RULES = email_rules()

CALL_RULES = (
    "Rules: written to be SPOKEN, not read. Short sentences a person can say without pausing. "
    "No jargon, no buzzwords, no bullet-point phrasing. "
    f"{TONE_RULE} "
    f"The `cta` field: {CALL_CTA_RULE} "
    "Use only facts given above — if a detail is missing, leave it out rather than inventing it. "
    "Never state a metric, customer name or case study that is not in the context."
)


def _downcase_lead(text: str) -> str:
    """Lowercase the first character unless doing so would damage an acronym or proper noun.

    The test is the SECOND character: "Stale lists" -> "stale lists", but "SOC2 gaps" and
    "CRM hygiene" are left alone because a capital following a capital means the word is not
    ordinary prose.
    """
    if len(text) < 2:
        return text.lower()
    if text[1].isupper():
        return text
    return text[0].lower() + text[1:]


# ---------------------------------------------------------------------------------------------
# Grounding the prompt in what we already know
#
# Audited 2026-09-01 after a user reported generic copy. The messaging prompt carried
# `account.name` and NOTHING else about the company, plus exactly one signal's `title` -- never its
# `body`. So the model did not know whether it was writing to a 40-person fintech or a 6,000-person
# manufacturer, what stack they run, or what the signal actually said.
#
# We crawl a funding announcement, store "raised $40M led by Sequoia to expand European operations"
# in `signal.body`, and hand the model the headline "Acme raises Series B". The substance was
# fetched, stored, billed for, and dropped. That is the difference between a mail-merge and
# personalisation.
#
# One rule runs through all three helpers: **an unknown fact is OMITTED, never rendered as
# "unknown"**. A line reading "Employees: unknown" invites the model to write around a hole, and
# writing around a hole is how invented detail gets in.

# The stack is the single most useful "I noticed you run X" hook, but it is also the field most
# likely to arrive with forty entries from an enrichment provider. Capped so it cannot crowd out
# the signal, which is the more perishable fact and the better opener.
MAX_TECH_IN_PROMPT = 8

# How much of a signal body to carry. Enough for the specifics a rep would open on -- the amount,
# the lead investor, the headcount -- without letting one press release dominate the budget.
MAX_SIGNAL_BODY_CHARS = 320

# Beyond three, the model starts writing a summary of the company's news rather than an email.
MAX_SIGNALS_IN_PROMPT = 3


def _revenue_band(revenue: int | None) -> str:
    """A band rather than the raw figure, for the same reason as `_employee_band`.

    `annual_revenue` is enriched (migration 0051) and filterable in the product, and it was the one
    firmographic that never reached the prompt — so a draft to a $2M business and one to a $2bn
    business were written against identical context. It changes how you write: what a 40-person
    company treats as a project, an enterprise treats as a line item.

    Quoting the exact number back would be worse than omitting it. Revenue estimates are the least
    reliable field any provider sells, and being precisely wrong about someone's turnover in a cold
    email is the kind of error that ends the conversation.
    """
    if not revenue or revenue <= 0:
        return ""
    if revenue < 10_000_000:
        return "under $10M revenue"
    if revenue < 50_000_000:
        return "$10-50M revenue"
    if revenue < 250_000_000:
        return "$50-250M revenue"
    if revenue < 1_000_000_000:
        return "$250M-1B revenue"
    return "over $1B revenue"


def _employee_band(count: int | None) -> str:
    """A band rather than the raw number.

    "120 employees" invites the model to quote it back at the buyer, which reads as surveillance
    and is often wrong by the time it lands. The band is what actually changes the email -- you
    write differently to a 40-person company than to a 6,000-person one -- without handing over a
    figure precise enough to be embarrassing.
    """
    if count is None:
        return ""
    if count < 50:
        return "under 50 employees"
    if count < 200:
        return "50-200 employees"
    if count < 1000:
        return "200-1,000 employees"
    if count < 5000:
        return "1,000-5,000 employees"
    return "5,000+ employees"


def account_facts(account) -> str:
    """What we know about the company, as prompt lines. Empty when we know nothing but the name."""
    lines: list[str] = [f"Company: {getattr(account, 'name', '') or 'the company'}"]

    industry = (getattr(account, "industry", "") or "").strip()
    if industry:
        lines.append(f"Industry: {industry}")

    band = _employee_band(getattr(account, "employee_count", None))
    if band:
        lines.append(f"Size: {band}")

    revenue = _revenue_band(getattr(account, "annual_revenue", None))
    if revenue:
        lines.append(f"Scale: {revenue}")

    # Region before country: "California" tells a rep more than "United States", and both together
    # read as a database dump.
    where = (getattr(account, "region", "") or "").strip() or (
        getattr(account, "country", "") or ""
    ).strip()
    if where:
        lines.append(f"Location: {where}")

    stack = [str(t).strip() for t in (getattr(account, "tech_stack", None) or []) if str(t).strip()]
    if stack:
        lines.append(f"Known tech: {', '.join(stack[:MAX_TECH_IN_PROMPT])}")

    description = (getattr(account, "custom_fields", None) or {}).get("description")
    if description:
        lines.append(f"What they do: {str(description).strip()[:240]}")

    return "\n".join(lines)


def today_line(now=None) -> str:
    """Anchor the model in real time, and give it dates it can actually name.

    Signals already carry a relative age ("3 days ago"), but nothing told the model what day it is.
    That became load-bearing the moment the CTA rule started asking for a specific slot: "Would
    Tuesday at 10am work?" is a better ask than "would that be valuable?", and it is a WORSE one if
    Tuesday was yesterday. A model with no clock either invents a date or hedges back into the vague
    close the rule exists to remove.

    So it gets today, and the two concrete weekdays it should choose between — computed here rather
    than left to the model, because date arithmetic is exactly the kind of thing it gets quietly
    wrong. Weekends are skipped: nobody takes a discovery call on Sunday.

    Also stops a stale signal being written up as fresh. The age phrase says "5 months ago"; this
    says what that means in a sentence the reader will check against their own calendar.
    """
    from datetime import datetime, timedelta, timezone

    now = now or datetime.now(timezone.utc)
    slots: list[str] = []
    probe = now
    while len(slots) < 2:
        probe += timedelta(days=1)
        if probe.weekday() < 5:                      # Mon-Fri
            slots.append(f"{probe.strftime('%A')} {probe.day} {probe.strftime('%B')}")
    return (
        f"Today is {now.strftime('%A')}, {now.day} {now.strftime('%B')} {now.year} (UTC). "
        f"If you propose a time, name one of these: {slots[0]} or {slots[1]}. "
        "Never propose a day that has already passed, and never a weekend."
    )


def _age_phrase(occurred_at) -> str:
    """How fresh the fact is.

    A rep opening on a nine-month-old funding round sounds like they only just found it. The model
    cannot phrase around staleness it was never told about.
    """
    if occurred_at is None:
        return ""
    from nexus.core.db import utcnow

    try:
        days = (utcnow() - occurred_at).days
    except (TypeError, ValueError):
        return ""
    if days < 0:
        return ""
    if days <= 10:
        return "in the last few days"
    if days <= 45:
        return "in the last month"
    if days <= 120:
        return "a few months ago"
    return "over six months ago"


def signal_age(signal) -> str:
    """How old a signal is, for a prompt — or "date unknown" when all we know is when we found it.

    An undated signal is dated at collection, so `_age_phrase` alone would call a two-year-old
    article "in the last few days" and invite the model to open on it as news.
    """
    from nexus.core.db import ensure_aware

    age = _age_phrase(ensure_aware(getattr(signal, "occurred_at", None)))
    if (getattr(signal, "dated", None) or "found") == "event":
        return age
    # Undated: the day we found it is the LATEST it can have happened. A recent find says nothing
    # about when; an old one is known to be at least that old, and saying so is what stops a rep
    # opening on it as news.
    if age in ("", "in the last few days", "in the last month"):
        return "date unknown"
    return f"date unknown, found {age}"


def signal_facts(signals, *, limit: int = MAX_SIGNALS_IN_PROMPT) -> str:
    """Render the strongest signals WITH their bodies. Empty string when there are none.

    Strongest first, because the model leans on what it reads first and the lead signal is the one
    the email should open on.
    """
    ranked = sorted(
        [s for s in (signals or []) if getattr(s, "title", None)],
        key=lambda s: getattr(s, "strength", 0.0) or 0.0,
        reverse=True,
    )[: max(1, limit)]
    if not ranked:
        return ""

    lines: list[str] = []
    for signal in ranked:
        kind = (getattr(signal, "kind", "") or "signal").replace("_", " ")
        age = signal_age(signal)
        head = f"- [{kind}{f', {age}' if age else ''}] {signal.title}"
        body = (getattr(signal, "body", "") or "").strip()
        if body:
            trimmed = body[:MAX_SIGNAL_BODY_CHARS]
            if len(body) > MAX_SIGNAL_BODY_CHARS:
                trimmed = trimmed.rsplit(" ", 1)[0] + "..."
            head += f"\n  {trimmed}"
        lines.append(head)
    return "\n".join(lines)


def select_value_prop(value_props: list[dict] | None, signals) -> dict:
    """Pick the value prop that best matches what actually triggered the outreach.

    Pitching ``value_props[0]`` at every account regardless of the trigger IS the mail-merge
    failure -- a hiring signal should pull the value prop about ramping new hires, not whichever
    one happens to be first in the list.

    Deterministic word overlap, not an LLM call: this runs on the copy path where an extra
    completion is latency and cost, and a rep asking "why did it pitch this?" deserves an answer.
    Ties and no-matches fall back to the first, so the behaviour is unchanged for a workspace with
    a single value prop -- which is most of them.
    """
    props = [vp for vp in (value_props or []) if isinstance(vp, dict)]
    if not props:
        return {"name": "our platform", "pains_solved": []}
    if len(props) == 1:
        return props[0]

    haystack = " ".join(
        f"{getattr(s, 'title', '') or ''} {getattr(s, 'body', '') or ''}"
        for s in (signals or [])
    ).lower()
    if not haystack.strip():
        return props[0]

    def score(vp: dict) -> int:
        text = " ".join([
            str(vp.get("name") or ""),
            str(vp.get("description") or ""),
            " ".join(str(p) for p in (vp.get("pains_solved") or [])),
        ]).lower()
        # Words shorter than five characters are almost all stopwords here ("the", "with", "for",
        # "new"), and they match everything -- which would make the score meaningless.
        # PREFIX match on a 5-character stem, not whole words. Measured while writing this: a
        # hiring signal reading "hiring 12 engineers" scored ZERO against a value prop whose pain
        # was "slow ramp for new engineering hires", because `engineers` != `engineering`. Exact
        # matching fails on precisely the inflections GTM copy is written in, and the fallback then
        # silently returns value_props[0] -- the mail-merge behaviour this function exists to end.
        stems = {w.strip(".,;:()-")[:5] for w in text.split() if len(w.strip(".,;:()-")) > 4}
        hay_stems = {w.strip(".,;:()-")[:5] for w in haystack.split() if len(w.strip(".,;:()-")) > 4}
        return len(stems & hay_stems)

    best = max(props, key=score)
    return best if score(best) > 0 else props[0]
