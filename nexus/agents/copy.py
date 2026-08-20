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
