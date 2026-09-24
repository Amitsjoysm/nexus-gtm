"""The ICP's industries become LinkedIn industry codes, once, cheaply, and never by a naive match.

The company-search actor filters on LinkedIn's numeric industry codes (434 of them, v2). An ICP says
"Software & SaaS" or "Fintech". Measured against the real list: "SaaS", "fintech", "cyber",
"e-commerce" and "AI" match no label at all, and a substring match on "artificial" maps "Artificial
Intelligence" to *Artificial Rubber and Synthetic Fiber Manufacturing*. So the mapping is whole-word
and synonym-led first, and the LLM is asked only for what that misses — from a short list, never
all 434 with their descriptions.
"""
from __future__ import annotations

import pytest

from nexus.prospecting.industries import (
    all_industries,
    descendants,
    map_industries,
    match_term,
    shortlist_for,
)


def test_the_full_list_is_available():
    industries = all_industries()
    assert len(industries) == 434
    assert industries[4].label == "Software Development"
    assert "Technology, Information and Internet" in industries[4].hierarchy


@pytest.mark.parametrize("term, expected", [
    ("Software Development", [4]),          # exact label
    ("SaaS", [4]),                          # no label says SaaS
    ("software", [4]),
    ("Cybersecurity", [118]),
    ("E-commerce", [1445]),
    ("Financial Services", [43]),
    ("insurance", [42]),
    ("Staffing and Recruiting", [104]),
    ("health care", [14]),                  # the top-level one, not Home Health Care Services
])
def test_common_icp_terms_map_deterministically(term, expected):
    assert match_term(term) == expected


def test_artificial_intelligence_is_not_rubber_manufacturing():
    ids = match_term("Artificial Intelligence")
    assert 703 not in ids, "'artificial' substring-matched Artificial Rubber manufacturing"
    assert 4 in ids


def test_a_compound_icp_value_splits_on_ampersand_but_not_on_and():
    # "Software & SaaS" is two terms; "Food and Beverage Services" is one label.
    assert match_term("Food and Beverage Services") == [34]


async def test_mapping_a_whole_icp_dedupes_and_keeps_order():
    assert await map_industries(["Software & SaaS", "Cybersecurity", "software"]) == [4, 118]


def test_a_parent_code_covers_its_children_for_database_matching():
    below = descendants([43])
    assert {43, 129, 45}.issubset(below), "Capital Markets and Investment Banking sit under 43"
    assert 4 not in below


async def test_the_llm_is_asked_only_for_misses_and_only_from_a_shortlist():
    asked: list[str] = []

    async def llm(term: str, shortlist):
        asked.append(term)
        valid = shortlist[0].id
        return f"[{valid}, 999999]"          # one real pick and one invented id

    ids = await map_industries(["SaaS", "developer tools platforms"], llm=llm)

    assert asked == ["developer tools platforms"], "the LLM was asked about a term the synonyms know"
    assert shortlist_for("developer tools platforms")[0].id in ids, "the LLM's shortlisted pick was lost"
    assert 999999 not in ids, "an id outside the shortlist was accepted"


async def test_a_broken_llm_costs_the_term_not_the_mapping():
    async def llm(term, shortlist):
        raise RuntimeError("provider down")

    assert await map_industries(["SaaS", "developer tools platforms"], llm=llm) == [4]


def test_the_shortlist_is_short_and_relevant():
    names = [i.label for i in shortlist_for("marketing automation software")]
    assert len(names) <= 20
    assert "Marketing Services" in names or "Software Development" in names
