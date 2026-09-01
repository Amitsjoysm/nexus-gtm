# nexus/agents/copy.py
"""Text shaping for agent prompts and the offline draft templates.

Small on purpose. It exists because one variable was carrying two incompatible grammatical forms
and nothing in the type system could notice.
"""
from __future__ import annotations

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

EMAIL_RULES = (
    f"Rules: Under {EMAIL_WORD_CAP} words. Short sentences. "
    "Open with a specific observation about THEM, then connect it to one problem, then ask one "
    "question. Never open with a pitch or with our company. "
    "Do not write 'hope this finds you well', 'I wanted to reach out', 'circling back', "
    "'synergy', 'leverage', 'game-changer', or 'revolutionary'. "
    "No more than one question. No bullet lists. Plain sentences only. "
    "Use only facts given above — if a detail is missing, leave it out rather than inventing it. "
    "Never state a metric, customer name or case study that is not in the context."
)

CALL_RULES = (
    "Rules: written to be SPOKEN, not read. Short sentences a person can say without pausing. "
    "No jargon, no buzzwords, no bullet-point phrasing. "
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
        age = _age_phrase(getattr(signal, "occurred_at", None))
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
