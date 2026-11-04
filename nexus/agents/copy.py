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
#:
#: **No literal examples in any rule** (content writers, 2026-10-29). The examples this rule and the
#: ask rule used to quote ('eu expansion', 'Worth a look?') came back word for word in thousands of
#: drafts, and a phrase every vendor's AI writes is one buyers and spam filters learn to skip. The
#: style is described instead; `shape_for` varies it per contact.
SUBJECT_RULE = (
    "Subject line: 2 to 4 words, lowercase except names and acronyms, the way a colleague labels "
    "an internal note about one topic. No urgency, no clickbait, no question or exclamation "
    "marks, and not the recipient's name."
)

#: Phrases that mark an email as automated, in the prompt AND in `email_quality.check_draft`, from
#: one list so the two cannot drift. The second half are the AI tells the content writers listed
#: (2026-10-29): a buyer who has read a hundred machine-written emails stops at the first of them.
BANNED_PHRASES = (
    "hope this finds you well",
    "hope you're doing well",
    "hope you are doing well",
    "i wanted to reach out",
    "i'm reaching out",
    "i am reaching out",
    "circling back",
    "touching base",
    "synergy",
    "game-changer",
    "game changer",
    "revolutionary",
    # A follow-up that adds nothing new (the 2026-10-01 playbook: every touch must earn its place).
    "just following up",
    "just bumping",
    "bumping this",
    "just checking in",
    # AI tells.
    "i came across",
    "came across your",
    "impressive",
    "streamline",
    "seamless",
    "i'd love to",
    "i would love to",
    "delve",
    "elevate",
    "unlock",
    "empower",
    "cutting-edge",
    "cutting edge",
    "fast-paced",
    "in today's",
    "look no further",
    "excited to share",
    "thrilled",
)

#: Banned in the prompt only: "leverage" is also an ordinary noun ("negotiating leverage"), so a
#: check on it would cost a regeneration on honest copy.
PROMPT_ONLY_BANNED = ("leverage",)


def banned_phrases_rule() -> str:
    quoted = ", ".join(f"'{p}'" for p in (*BANNED_PHRASES, *PROMPT_ONLY_BANNED))
    return f"Never write any of these: {quoted}."


#: How a post, a talk or an article of theirs may be used, in the email rules and in the person
#: brief alike. `brief.py` used to tell the call script "in their own words" while `SIGNAL_RULE`
#: said never quote it, and a model given both followed whichever it read last.
ACTIVITY_RULE = (
    "Reference the idea, never the quote: say in your own words what their post or article was "
    "about, and never repeat its wording back to them."
)

#: Size and revenue are for register, never for the page (content writers, 2026-10-29).
#: `account_facts` gives the band so a draft to a 40-person firm reads differently from one to a
#: 6,000-person one; "as a 50-200 employee company" in the email reads as a database lookup, and a
#: revenue band is the least reliable figure any provider sells. Checked by `check_draft`.
FIRMOGRAPHICS_RULE = (
    "Never mention their headcount, company size, size band or revenue: those set how you write, "
    "they are not something to say."
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
#:
#: Two parts added 2026-10-29 from the content writers:
#: * **What we do.** The model was given the value prop's name and the pains it removes and never
#:   asked what the product IS, so drafts described a problem and never said what was on offer. A
#:   buyer cannot say yes to something unnamed; the value prop's description now reaches the prompt
#:   and this rule asks for one plain sentence of it.
#: * **The problem as a pattern, not a claim about them.** "Your team is struggling with X" is a
#:   claim nobody in the context can support, and the pressure to write one is what produces an
#:   invented fact. What teams in their situation usually find is true by construction.
STRUCTURE_RULE = (
    "Lay the body out as short paragraphs of one or two sentences, separated by one blank line: "
    "a greeting line that addresses the recipient by first name; the reason you are writing now; "
    "the problem that kind of moment usually creates for teams like theirs, stated as a pattern "
    "you have seen rather than as a fact about them; one plain sentence saying what we do, in the "
    "words a buyer would use; one line of proof only if the context names a real customer "
    "result; the ask, on its own line; then a one-word sign-off line with your first name on the "
    "line below it. Do not write a title, company, phone number or any other signature detail "
    "under the sign-off; that is added automatically. Plain text only: no bullet points, bold, "
    "headings, emoji or em dashes, and at most one link, only if the context gives it."
)

#: One signal, used rather than recited. Three signals in one email reads as a dossier, and quoting
#: someone's post back to them reads as surveillance; the playbook's line is "reference the idea".
SIGNAL_RULE = (
    "Use ONE signal: the trigger named above, which explains why you are writing now, and "
    "connect it to the problem we solve. Say what it means for them rather than reciting it. "
    f"{ACTIVITY_RULE} Do not pretend to know them, and do not compliment them."
)

#: The ask in an email nobody has answered yet: one interest question, NOT a meeting.
#:
#: This replaced "name a short duration and a rough time" on 2026-10-01, on the playbook's
#: evidence that an interest-based ask ("Worth a look?") is answered far more often than a meeting
#: request in a cold email: a stranger can say yes to a question in one word, and a calendar ask
#: wants half an hour before they know why. What survives from the old rule is its reason: do not
#: make the reader judge whether something is "valuable".
#:
#: It quoted two example questions until 2026-10-29, and those two questions closed most drafts
#: (see `SUBJECT_RULE`). The form is described; `shape_for` picks one of a few per contact.
EMAIL_CTA_RULE = (
    "End with ONE short, interest-based question of a few words that they can answer yes or no. "
    "Do not ask for a meeting, call or demo, and do not name a duration, a day or a time: that "
    "comes after they reply. Do not offer to send anything the context does not mention, and do "
    "not ask whether something would be 'valuable', 'helpful' or 'of interest'."
)

#: The ask for a workspace that chose "a short meeting" for its first emails (Settings → Email style,
#: `style.first_ask = "meeting"`, 2026-10-01). Opt-in: the playbook's interest question stays the
#: default, and this keeps the old rule's guard against asking whether a meeting would be "valuable".
MEETING_CTA_RULE = (
    "End with ONE specific, low-friction ask for a short meeting: name a short duration and a "
    "rough day, which they can accept or decline in a word. Do not ask whether a conversation "
    "would be 'valuable', 'helpful' or 'of interest'."
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


#: The ask of a reply the AI sends by itself (`engagement/replies/auto_reply.py`). Nobody reads it
#: before it goes, and the AI cannot see the rep's calendar, so it asks rather than offers: a time
#: it proposed would be a promise the rep never made. `auto_reply.names_a_time` holds a draft that
#: names one anyway.
AUTO_REPLY_CTA_RULE = (
    "This reply is sent without the rep reading it first and you cannot see their calendar, so "
    "never propose a specific day, date or time, and never promise anything the facts above do "
    "not state. If they showed interest, end by asking which days suit them for a short call. "
    "Otherwise answer their question from the facts above and end with one clear next step they "
    "can accept or decline in a word."
)


# ---------------------------------------------------------------------------------------------
# A seeded shape per contact (content writers, 2026-10-29).
#
# One rule set produces one email: the same opening move, the same ask, the same kind of subject,
# for every buyer of every customer, which is what buyers and spam filters learn to recognise. The
# contact's id picks one of a few DESCRIBED styles for each part: stable for that person (a
# regenerated draft keeps its shape, so a rep comparing two attempts compares content), varied
# across a list. A digest, not Python's `hash()`, which changes between processes.

OPENER_STYLES = {
    "trigger": "After the greeting, open on the trigger itself, in one short plain sentence.",
    "question": "After the greeting, open with the one question that kind of trigger usually "
                "raises for someone in their role.",
    "pattern": "After the greeting, open on what that kind of trigger usually changes for teams "
               "like theirs.",
}
ASK_STYLES = {
    "look": "Phrase the ask as whether it is worth a short look, in five words or fewer.",
    "priority": "Phrase the ask as whether this is on their list for this quarter, in eight "
                "words or fewer.",
    "how": "Phrase the ask as whether they would like to see how it works, in six words or fewer.",
}
SUBJECT_STYLES = {
    "trigger": "Let the subject name the trigger.",
    "problem": "Let the subject name the problem.",
    "team": "Let the subject name their team or function and the topic.",
}


def shape_for(seed: str | None) -> dict:
    """The opener, ask and subject style for one contact: ``{"opener", "ask", "subject"}``."""
    import hashlib

    digest = int(hashlib.sha256(str(seed or "").encode("utf-8")).hexdigest(), 16)
    openers, asks, subjects = list(OPENER_STYLES), list(ASK_STYLES), list(SUBJECT_STYLES)
    return {
        "opener": openers[digest % len(openers)],
        "ask": asks[(digest // len(openers)) % len(asks)],
        "subject": subjects[(digest // (len(openers) * len(asks))) % len(subjects)],
    }


def shape_label(shape: dict | None) -> str:
    """How the shape is logged on the message (`draft_facts`, kind ``shape``)."""
    shape = shape or {}
    return (f"Shape: opener {shape.get('opener', '?')}, ask {shape.get('ask', '?')}, "
            f"subject {shape.get('subject', '?')}")


def _shape_rules(shape: dict | None, touch: str, first_ask: str) -> str:
    """The parts of the shape this touch uses. The opener and subject belong to a first email (a
    follow-up's opening is its own instruction, its subject the thread's); the ask style to any
    email nobody has answered, unless the workspace chose to ask for a meeting."""
    if not shape:
        return ""
    parts = []
    if touch not in THREADED_TOUCHES and touch not in WARM_TOUCHES:
        parts += [OPENER_STYLES.get(shape.get("opener"), ""),
                  SUBJECT_STYLES.get(shape.get("subject"), "")]
    if touch not in WARM_TOUCHES and first_ask != "meeting":
        parts.append(ASK_STYLES.get(shape.get("ask"), ""))
    return " ".join(p for p in parts if p)


# ---------------------------------------------------------------------------------------------
# An optional P.S. a person confirms (content writers, 2026-10-29; off by default,
# `email_style.ps_line_on`).
#
# A P.S. is the most-read line of a cold email after the subject, which is exactly why it must be
# true: it is written as a separate field, never into the body, shown on Review with "I confirm
# this is true", and added below the signature only when the rep ticks it. Never on an automatic
# follow-up or reply, because nothing can know a human read it.

PS_PREFIX = "P.S."

PS_RULE = (
    "After your first name, you may add one optional line starting 'P.S.' that restates one "
    "specific fact from the context above (a signal, something they posted, a real customer "
    "result) in one short sentence. Only a fact given above; if none fits, leave the P.S. out."
)

#: A trailing P.S. paragraph: "P.S." or "PS", with or without a colon, at the start of a line.
_PS_LINE = re.compile(r"(?im)^\s*p\.?\s?s\.?\s*[:\-]?\s+(?=\S)")


def split_ps(text: str) -> tuple[str, str]:
    """``(body, ps)``: a P.S. at the end of the text, taken off it. ``ps`` is "" when there is none.

    Every draft goes through this, whether a P.S. was asked for or not: a P.S. a model added on its
    own would otherwise sit in the body and be sent without anyone confirming it."""
    text = (text or "").replace("\r\n", "\n")
    matches = list(_PS_LINE.finditer(text))
    if not matches:
        return text, ""
    last = matches[-1]
    # Its own paragraph only: a model that put the P.S. above the sign-off keeps its sign-off.
    ps, _, after = text[last.end():].partition("\n\n")
    body = text[: last.start()].rstrip()
    if after.strip():
        body = f"{body}\n\n{after.strip()}"
    return body, " ".join(ps.split())


def with_ps(body: str, ps: str) -> str:
    """The body with a confirmed P.S. as its last paragraph. The send path moves it below the
    signature (`engagement/sending/mime.compose_message_text`)."""
    ps = " ".join((ps or "").split())
    body = (body or "").rstrip()
    return f"{body}\n\n{PS_PREFIX} {ps}" if ps else body


def email_rules(touch: str = "first", *, first_ask: str = "interest",
                word_cap: int | None = None, automatic: bool = False,
                shape: dict | None = None, ps: bool = False) -> str:
    """The rules for one email. ``touch`` is the engagement kind (`first`, `followup`, `signal`,
    `reengage`, `response`); anything else is read as a first email, which is what every caller
    outside the engagement engine (the composer, plays, the orchestrator) is writing.

    ``first_ask`` and ``word_cap`` are the workspace's own choices (`email_style.draft_options`).
    Their defaults are the playbook's, so a workspace that set neither is unchanged.

    ``shape`` is this contact's seeded style (`shape_for`); ``ps`` asks for the optional P.S.,
    which only a first email a person reviews may carry (`PS_RULE`)."""
    warm = touch in WARM_TOUCHES
    cap = word_cap or EMAIL_WORD_CAP
    if warm:
        ask = AUTO_REPLY_CTA_RULE if automatic else REPLY_CTA_RULE
    elif first_ask == "meeting":
        ask = MEETING_CTA_RULE
    else:
        ask = EMAIL_CTA_RULE
    styled = _shape_rules(shape, touch, first_ask)
    with_ps_rule = ps and touch == "first" and not automatic
    return (
        f"Rules: Under {cap} words; a follow-up can be much shorter. Short sentences. "
        f"{STRUCTURE_RULE} "
        f"{'' if touch in THREADED_TOUCHES else SUBJECT_RULE + ' '}"
        f"{'' if warm else SIGNAL_RULE + ' '}"
        "Never open with a pitch or with our company. "
        f"{TONE_RULE} "
        f"{banned_phrases_rule()} "
        f"{FIRMOGRAPHICS_RULE} "
        "No more than one question. "
        f"{ask} "
        f"{styled + ' ' if styled else ''}"
        f"{PS_RULE + ' ' if with_ps_rule else ''}"
        "Use only facts given above — if a detail is missing, leave it out rather than inventing "
        "it. Never state a metric, customer name or case study that is not in the context."
    )


EMAIL_RULES = email_rules()

# ---- the call script, written for the ear (content writers, 2026-10-29) -------------------------
#
# A talk track is heard once, at speaking pace, by someone who did not expect the call. So the
# opener is short and asks permission (a person who agreed to thirty seconds listens; one who was
# ambushed waits for a gap to hang up), the voicemail fits the time people actually listen, and the
# first three things a buyer says (who is this, send me an email, I'm busy) each have an answer that
# names a real problem we remove. `email_quality.check_call_script` checks the parts a rep can point
# at, and the agent regenerates once.

#: About ten seconds spoken: past this a buyer has stopped listening to the opener.
CALL_OPENER_WORDS = 30
#: About twenty-five seconds: the length of voicemail people listen to the end of.
CALL_VOICEMAIL_WORDS = 55

#: Stock call lines, banned on top of `BANNED_PHRASES`: each one invites "no" or sounds read.
CALL_BANNED_PHRASES = (
    "how are you today",
    "how are you doing today",
    "did i catch you at a bad time",
    "is this a bad time",
    "i hope i'm not bothering you",
)

#: The three reactions every cold call meets first, in the order a buyer says them.
FIRST_REACTIONS = ("Who is this?", "Just send me an email.", "I'm busy.")

CALL_RULES = (
    "Rules: written to be SPOKEN, not read. Short sentences a person can say without pausing. "
    "No jargon, no buzzwords, no bullet-point phrasing. "
    f"{TONE_RULE} "
    f"The `opener` field asks permission: your name and company, that this is a cold call in "
    f"plain words, and a request for thirty seconds to say why you called, as a question; under "
    f"{CALL_OPENER_WORDS} words. "
    "The `hook` states the problem as a pattern teams in their situation usually hit, never as a "
    "fact about them. "
    "The `first_reactions` field answers exactly these three first reactions, in this order: "
    + "; ".join(f"'{r}'" for r in FIRST_REACTIONS) + ". Each answer is one or two sentences "
    "that come back to one of the problems we remove, named above. "
    f"The `voicemail`: under {CALL_VOICEMAIL_WORDS} words, your name and company, the one reason "
    "you called, and that you will follow up by email. "
    f"The `cta` field: {CALL_CTA_RULE} "
    f"{ACTIVITY_RULE} {FIRMOGRAPHICS_RULE} "
    "Never write a placeholder such as a bracketed name or company: use the names given above, "
    "or leave the detail out. "
    + "Never say any of these: "
    + ", ".join(f"'{p}'" for p in (*CALL_BANNED_PHRASES, *BANNED_PHRASES)) + ". "
    "Use only facts given above — if a detail is missing, leave it out rather than inventing it. "
    "Never state a metric, customer name or case study that is not in the context."
)


def caller_block(name: str, company: str) -> str:
    """Who is calling, for the call script prompt (the email's `sender_block`, spoken). Without it
    the model wrote "this is <your name> from <your company>" and the rep read the brackets out
    or stopped to edit them mid-dial. Empty when neither is known."""
    name = " ".join((name or "").split())
    company = " ".join((company or "").split())
    if not name and not company:
        return ""
    lines = ["YOU (THE CALLER)"]
    if name:
        lines.append(f"- Name: {name}. Introduce yourself by your first name, {name.split()[0]}.")
    else:
        # Measured on the local deploy: told only the company, the model introduced "Alex", a
        # person nobody in the workspace is. A name nobody gave is a fabrication, spoken live.
        lines.append("- Name: not known. Never make one up: introduce yourself by the company only.")
    if company:
        lines.append(f"- Company: {company}")
    return "\n".join(lines) + "\n"


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


def account_facts(account, icp: dict | None = None) -> str:
    """What we know about the company, as prompt lines. Empty when we know nothing but the name.

    Minus what is likely wrong (`agents.facts`, 2026-10-26): an industry or country that is the
    ICP's own word stamped on an old web find, and website plumbing listed as tech."""
    from nexus.agents.facts import looks_stamped, useful_tech

    lines: list[str] = [f"Company: {getattr(account, 'name', '') or 'the company'}"]

    industry = (getattr(account, "industry", "") or "").strip()
    if industry and not looks_stamped(account, "industry", icp):
        lines.append(f"Industry: {industry}")

    # Labelled as register, never content (`FIRMOGRAPHICS_RULE`): the band was quoted back in
    # drafts ("as a 50-200 employee company"), which reads as a lookup. Checked by `check_draft`.
    band = _employee_band(getattr(account, "employee_count", None))
    if band:
        lines.append(f"Size (sets your register; never mention it): {band}")

    revenue = _revenue_band(getattr(account, "annual_revenue", None))
    if revenue:
        lines.append(f"Scale (sets your register; never mention it): {revenue}")

    # Region before country: "California" tells a rep more than "United States", and both together
    # read as a database dump.
    country = (getattr(account, "country", "") or "").strip()
    if looks_stamped(account, "country", icp):
        country = ""
    where = (getattr(account, "region", "") or "").strip() or country
    if where:
        lines.append(f"Location: {where}")

    stack = useful_tech(getattr(account, "tech_stack", None))
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


def pick_anchor(signals, contact=None):
    """The ONE signal a draft is about, picked once (content writers, 2026-10-29).

    It used to be picked three times: the hook preferred a signal tied to the person, the value prop
    was scored against every signal at once, and `trigger_signal_id` logged the strongest account
    signal, so one email could open on a funding round, pitch the hiring value prop and record a
    third event as its reason. Now the anchor drives all three. A signal about this person wins
    (it is the most specific thing we know), else the strongest; ties keep the newest, which is the
    order signals are loaded in. None when there are no signals."""
    pool = [s for s in (signals or []) if getattr(s, "title", None)]
    if not pool:
        return None
    contact_id = getattr(contact, "id", None)
    personal = [s for s in pool if contact_id and getattr(s, "contact_id", None) == contact_id]
    return max(personal or pool, key=lambda s: getattr(s, "strength", 0.0) or 0.0)


#: What each signal kind is usually about, so a value prop can match the KIND of trigger and not
#: only the words of one headline (a funding headline names investors, not the growth it pays for).
_KIND_WORDS = {
    "funding": "funding raised round investors growth scaling expansion capital budget",
    "hiring": "hiring hires recruiting headcount onboarding ramping growing",
    "job_posting": "hiring hires recruiting headcount onboarding ramping growing",
    "job_switch": "leader leadership change priorities transition",
    "tech_install": "stack tooling integration migration adoption platform",
    "website_change": "pricing positioning launch strategy",
    "g2_intent": "evaluating comparing vendors alternatives switching",
    "web_visit": "evaluating interest",
    "product_usage": "usage adoption limits",
}

#: Five-letter-or-longer words that appear in any value prop or headline and so match everything.
_VP_STOPWORDS = frozenset("""
about after again their there these those which while where would could should every other
teams company companies business businesses platform solution solutions product products customer
customers using without through across faster better easier simple simply quickly within before
""".split())


def _stems(text: str) -> set[str]:
    words = (w.strip(".,;:()-'\"!?") for w in (text or "").lower().split())
    return {w[:5] for w in words if len(w) > 4 and w not in _VP_STOPWORDS}


def select_value_prop(value_props: list[dict] | None, signals, *, role: str = "") -> dict:
    """Pick the value prop that best matches what actually triggered the outreach.

    Pitching ``value_props[0]`` at every account regardless of the trigger IS the mail-merge
    failure -- a hiring signal should pull the value prop about ramping new hires, not whichever
    one happens to be first in the list.

    ``signals`` is the anchor (`pick_anchor`) as a one-item list; the score reads its title, body
    and what its KIND is usually about (`_KIND_WORDS`). ``role`` is the person's function words
    (`personalization.brief.role_words`), a tie-breaker that also answers when the trigger matches
    nothing, so fewer drafts fall back silently to the first value prop.

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

    trigger = " ".join(
        f"{getattr(s, 'title', '') or ''} {getattr(s, 'body', '') or ''} "
        f"{_KIND_WORDS.get(getattr(s, 'kind', '') or '', '')}"
        for s in (signals or [])
    )
    # PREFIX match on a 5-character stem, not whole words. Measured: a hiring signal reading
    # "hiring 12 engineers" scored ZERO against a value prop whose pain was "slow ramp for new
    # engineering hires", because `engineers` != `engineering`. Exact matching fails on precisely
    # the inflections GTM copy is written in.
    hay, role_stems = _stems(trigger), _stems(role)
    if not hay and not role_stems:
        return props[0]

    def score(vp: dict) -> int:
        stems = _stems(" ".join([
            str(vp.get("name") or ""),
            str(vp.get("description") or ""),
            " ".join(str(p) for p in (vp.get("pains_solved") or [])),
        ]))
        # The trigger counts double: it is why we are writing; the role is who we write to.
        return 2 * len(stems & hay) + len(stems & role_stems)

    best = max(props, key=score)
    return best if score(best) > 0 else props[0]


def product_line(vp: dict | None) -> str:
    """What we do, from the value prop's description, for the one plain sentence the structure
    rule asks for. "" when the value prop carries no description (the system message still has the
    product context)."""
    description = " ".join(str((vp or {}).get("description") or "").split())
    return description[:300]


# ---- what we already fetched about the account (content writers, 2026-10-29) --------------------
#
# The scoring agent's fit reasons and the research agent's highlights were computed, stored and
# never shown to the writer. Both are capped (three lines between them) and filtered to text a
# person could use: a fit reason is machine phrasing ("industry 'SaaS' is in ICP") and some carry a
# raw headcount or revenue, which `FIRMOGRAPHICS_RULE` keeps out of an email; a research line can
# be a raw search snippet, cut mid-word or a URL.

MAX_EXTRA_FACT_LINES = 3


def fit_lines(fit) -> list[str]:
    """The ICP fit reasons worth telling the writer, as plain sentences. Negative and unknown
    reasons, and any carrying a headcount or revenue figure, are left out."""
    out: list[str] = []
    for reason in (getattr(fit, "reasons", None) or []):
        text = str(reason or "").strip()
        lowered = text.lower()
        if not text or "not in" in lowered or "unknown" in lowered or "without" in lowered:
            continue
        if lowered.startswith(("headcount", "revenue", "postal")):
            continue
        match = re.match(r"industry '(.+)' (?:is in|matches)", text)
        if match:
            out.append(f"Their industry, {match.group(1)}, is one we sell to.")
            continue
        match = re.match(r"(?:geo|region) '(.+)' in ICP", text)
        if match:
            out.append(f"They are in {match.group(1)}, a market we sell to.")
            continue
        if lowered.startswith("uses "):
            # Only what the reason says: the ICP names these tools. Not "our customers run them",
            # which nothing in the context supports.
            out.append(f"They use {text[5:]}, tools our ideal customer profile names.")
    return out


def research_lines(facts) -> list[str]:
    """Research highlights a writer could use: a sentence of 6 to 40 words, not a URL, not a snippet
    cut off mid-thought, not a "no facts" placeholder."""
    out: list[str] = []
    for fact in (facts or []):
        text = " ".join(str(fact or "").split())
        words = text.split()
        if not 6 <= len(words) <= 40:
            continue
        if "http://" in text or "https://" in text or "www." in text:
            continue
        if text.endswith(("...", "…")) or text.lower().startswith(("no external", "no facts")):
            continue
        out.append(text)
    return out


def extra_fact_lines(*, research=None, fit=None) -> str:
    """At most `MAX_EXTRA_FACT_LINES`, research first (more specific), as a prompt block."""
    lines = research_lines(research)[:MAX_EXTRA_FACT_LINES]
    lines += fit_lines(fit)[: MAX_EXTRA_FACT_LINES - len(lines)]
    if not lines:
        return ""
    return "MORE WE KNOW (use one only if it sharpens the email):\n" + "\n".join(
        f"- {line}" for line in lines)
