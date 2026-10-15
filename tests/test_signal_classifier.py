"""Headline classification: negation and speculation.

``_classify_news`` matched needles as bare substrings, so "No funding was raised" produced
kind="funding", strength=0.85 — the strongest class in the system. High strength means an Inbox task
and possibly a triggered play, so a rep opens the article to find there was no round. Every
search-backed source calls this (``WebNewsSource``, ``DorkedSearchSource``, ``RssSignalSource``), so
one bad match reaches all three.

The fix must not blunt true positives, which is the harder half: "raises" appearing near the word
"no" is usually still a funding round ("no fewer than three investors"). The false-positive cases
below are paired with true-positive cases that must keep their full strength.
"""
from __future__ import annotations

import pytest

from nexus.ingestion.sources import _classify_news

# ---- negated and speculative mentions must not be strong events -------------------------------

NOT_EVENTS = [
    # Explicit negation of the event.
    "Acme raised no new funding this year",
    "Acme did not raise a Series B",
    "Acme denies raising at a lower valuation",
    "Acme has no plans to raise again",
    "Acme closed the year without raising",
    "Acme isn't raising despite the reports",
    # Speculation: something that has not happened is not a buying signal.
    "Acme is rumored to raise a Series C",
    "Acme could raise as soon as next quarter",
    "Acme may raise at a $2B valuation",
    "Acme reportedly plans to raise later this year",
]


@pytest.mark.parametrize("text", NOT_EVENTS)
def test_a_negated_or_speculative_mention_is_not_a_funding_event(text):
    kind, strength = _classify_news(text)
    assert (kind, strength) != ("funding", 0.85), f"{text!r} was scored as a real round"
    # It stays in the timeline as a weak mention — the information is real, the *event* is not.
    assert strength <= 0.4


@pytest.mark.parametrize("text", NOT_EVENTS)
def test_a_non_event_never_outranks_a_real_one(text):
    """Strength drives the Inbox. A non-event scoring above a genuine partnership would reorder a
    rep's day around something that did not happen."""
    _, weak = _classify_news(text)
    _, real = _classify_news("Acme partners with Globex")
    assert weak < real


# ---- true positives keep their full strength --------------------------------------------------

REAL_FUNDING = [
    "Acme raises $40M Series B",
    "Acme raised a $40 million Series B led by Sequoia",
    "Acme Corp Raises Series F at $44 Billion Valuation",
    "Acme announces seed round",
    "Acme secures $12M in new financing",
]


@pytest.mark.parametrize("text", REAL_FUNDING)
def test_a_real_round_still_classifies_as_funding(text):
    assert _classify_news(text) == ("funding", 0.85)


# ---- needles are whole words (2026-10-15) ------------------------------------------------------
#
# Measured on the local deploy: of the 20 newest `funding` signals, none was a round for its
# account, and these were the needles that fired. Each was matched as a bare substring.

WORD_PARTS = [
    "The series creator noted the premiere date",                  # "series c"
    "Your Model's MSE Is Lying to You III: Time Series Diffusion",  # "series d"
    "Aaron Cohen on the Saban docuseries airs tonight",            # "series a"
    "Odoo evaluation guide for manufacturers",                      # "valuation"
    "Leading a new team is a challenging adventure",                # "venture"
    "Critics praised the redesign",                                 # "raised"
]


@pytest.mark.parametrize("text", WORD_PARTS)
def test_a_needle_inside_another_word_is_not_an_event(text):
    assert _classify_news(text)[0] != "funding", f"{text!r} was scored as a round"


def test_a_whole_word_needle_still_matches_beside_punctuation():
    assert _classify_news("Acme closes its Series A.") == ("funding", 0.85)
    assert _classify_news("Acme (Series B) announced today") == ("funding", 0.85)
    assert _classify_news("Acme secures $12M from Index") == ("funding", 0.85)
    assert _classify_news("Acme raises $40M") == ("funding", 0.85)


# ---- "raises" needs money or a round; "valuation" and "venture" need a round (2026-10-15) -------
#
# Every one of these was stored as a 0.85 funding signal in the last 30 days on the local deploy.

NOT_ROUNDS = [
    "Waystar posts strong Q2 2026 results, raises outlook",
    "General Motors raised its 2026 guidance after significantly beating estimates",
    "Citigroup raises one-year Bitcoin forecast to $113,000",
    "Cornell Sex Assault Case Raises Questions About Title IX Accountability",
    "US raises Graphite One loan to $2.1B",
    "Why You Should Do Toe Raises, Not Just Calf Raises",
    "Meta Announces New Strategic Venture with BlackRock to Develop Data Centers",
    "Our Team of Venture Capital Professionals | 500 Global",
    "SpaceX Lowers IPO Valuation Target",
    "Browserstack's Financials, Revenue, Profit, Valuation & Shareholding",
]

STILL_ROUNDS = [
    "Celebal Technologies raises ₹ 50.00 Crore in Latest funding round",
    "Turing raises 15.3 billion yen in its Series A first close",
    "How a former DeepMind researcher raised at a $300M pre-seed",
    "Mercor Raises $30M Series A at a $250M Valuation to Create Jobs with AI",
    "Acme raises new funding from Index Ventures",
    "Acme closes $50M round at a $1B valuation",
    "Acme raised a Series B led by Sequoia",
    "Anthropic raises $30bn in latest round",
]


@pytest.mark.parametrize("text", NOT_ROUNDS)
def test_raises_without_money_or_a_round_is_not_a_round(text):
    assert _classify_news(text)[0] != "funding", f"{text!r} was scored as a round"


@pytest.mark.parametrize("text", STILL_ROUNDS)
def test_a_round_with_money_or_a_round_word_is_still_funding(text):
    assert _classify_news(text) == ("funding", 0.85), text


def test_negation_far_from_the_needle_does_not_suppress_the_event():
    """The guard is a window around the match, not a search of the whole string. A headline can
    contain "no" for unrelated reasons and still report a real round."""
    kind, strength = _classify_news(
        "Acme raises $40M Series B with no participation from existing investors"
    )
    assert (kind, strength) == ("funding", 0.85)


def test_other_event_classes_are_guarded_too():
    """Negation is not a funding-specific problem — the same substring matching runs for hiring and
    news needles."""
    assert _classify_news("Acme is not hiring this quarter")[1] <= 0.4
    assert _classify_news("Acme denies acquiring Globex")[1] <= 0.4
    # ...and their true positives survive.
    assert _classify_news("Acme appoints a new CFO") == ("hiring", 0.6)
    assert _classify_news("Acme acquires Globex") == ("news", 0.6)


def test_first_match_wins_ordering_is_preserved():
    """Deliberate, per the comment on _NEWS_PATTERNS: a headline that is both a round and an
    acquisition is a funding signal first."""
    assert _classify_news("Acme raises $40M and acquires Globex") == ("funding", 0.85)


def test_an_unmatched_headline_is_still_a_weak_mention():
    assert _classify_news("Acme Corp opens a design studio") == ("news", 0.4)
    assert _classify_news("") == ("news", 0.4)
