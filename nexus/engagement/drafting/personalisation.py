"""Does a draft use at least one specific fact about the person or their company? (D17)

A rule in a prompt is a request; this is the check. "Hyper-personalised" is only enforceable if it
means something a reader could point at, so it means: **one of the facts in the context pack shows
up in the email.** A fact "shows up" when a number from it appears (a round size, a headcount, a
year), or when two of its distinctive words do — or half of them, for a short fact. Distinctive means four letters or more and not a
word every email contains, so "Hi", "your", "team" and the company's own name never count — naming
the company is not personalisation, it is a mail merge.

Pure, and deliberately literal: an LLM judge here would pass the drafts it wrote itself.
"""
from __future__ import annotations

import re

#: Words that appear in any sales email and so prove nothing about the reader.
_COMMON = frozenset("""
about after again also always because been before being between both could does doing during
each every from have having here hers into just like make many more most much must need only
other over same should since some such than that their them then there these they this those
through under until very want were what when where which while will with within would your
yours team teams company companies business work working help helps helping looking noticed
great quick chat call time week weeks month months year years thanks best regards hello thought
reach reaching out touch follow following share open worth minutes would love happy sounds
growth scale scaling platform solution solutions product products customers customer
""".split())

_WORD = re.compile(r"[a-z][a-z'\-]{3,}")
_NUMBER = re.compile(r"\d[\d,.]*\d|\d")


def _distinctive(text: str, stop: frozenset[str]) -> set[str]:
    return {w for w in _WORD.findall((text or "").lower()) if w not in _COMMON and w not in stop}


def _numbers(text: str) -> set[str]:
    # A lone digit ("1 thing") proves nothing; a figure with at least two digits does.
    return {n.replace(",", "") for n in _NUMBER.findall(text or "") if len(n.replace(",", "")) >= 2}


def fact_used(body: str, fact: str, *, company_words: frozenset[str] = frozenset()) -> bool:
    body_words = _distinctive(body, company_words)
    fact_words = _distinctive(fact, company_words)
    if _numbers(fact) & _numbers(body):
        return True
    matched = len(fact_words & body_words)
    # Two distinctive words, or half of a short fact's: "Series B" is one word of "raises Series B"
    # and is as specific as an email gets; one word of an eight-word headline is a coincidence.
    return matched >= 2 or (matched >= 1 and matched * 2 >= len(fact_words))


def uses_a_fact(body: str, facts: list[str], *, company_name: str = "") -> bool:
    """True when at least one fact is visibly used. No facts at all is a pass: there is nothing the
    draft could have used, and holding every email for want of research is a different problem."""
    usable = [f for f in (facts or []) if (f or "").strip()]
    if not usable:
        return True
    company_words = frozenset(w.lower() for w in re.findall(r"[A-Za-z']{4,}", company_name or ""))
    return any(fact_used(body, fact, company_words=company_words) for fact in usable)


PROBLEM = ("The email uses no specific fact about the person or their company. Open on one of the "
           "facts given above — a signal, their role or something they posted — in your own words.")
