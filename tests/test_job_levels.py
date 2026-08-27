# tests/test_job_levels.py
"""Deterministic job-level and keyword title matching.

The tester's example, verbatim: targeting Facilities leadership should match `Head of Facilities`,
`Facilities Head`, `Facilities Director`, `Director of Facilities` and `Director, Facilities`. Exact
title matching found none of them, and contact search returned nothing across three campaigns.

Deterministic on purpose, no LLM. This decides which humans a rep contacts, and a rep who asks "why
did this person match?" deserves a better answer than "the model thought so". It also has to work
when the LLM provider is down — which is exactly the condition that produced the bug report.
"""
from __future__ import annotations

import pytest


@pytest.mark.parametrize("title,level", [
    ("Chief Technology Officer", "c_level"),
    ("CTO", "c_level"),
    ("Chief Executive Officer", "c_level"),
    ("Chief People Officer", "c_level"),
    ("President", "c_level"),
    ("Managing Director", "c_level"),      # in most markets this is the top job, not a director
    ("SVP Engineering", "vp"),
    ("Senior Vice President, Sales", "vp"),
    ("VP Facilities", "vp"),
    ("Head of Facilities", "head"),
    ("Facilities Head", "head"),
    ("Director of Facilities", "director"),
    ("Facilities Director", "director"),
    ("Director, Facilities", "director"),
    ("Senior Manager, Operations", "manager"),
    ("Facilities Manager", "manager"),
    ("Team Lead, Support", "manager"),
    ("Software Engineer", "ic"),
    ("", "ic"),
])
def test_titles_normalise_to_a_level(title, level):
    from nexus.relevance.job_levels import level_of

    assert level_of(title) == level


def test_founder_reads_as_c_level():
    """A 30-person company's Founder IS the economic buyer."""
    from nexus.relevance.job_levels import level_of

    assert level_of("Founder & CEO") == "c_level"
    assert level_of("Co-Founder") == "c_level"
    assert level_of("Cofounder") == "c_level"


def test_the_most_senior_level_wins():
    """Order is load-bearing: 'VP, Engineering Manager' is a VP. Ascending order would call it a
    manager and drop it from a VP-and-above search."""
    from nexus.relevance.job_levels import level_of

    assert level_of("VP, Engineering Manager") == "vp"
    assert level_of("Chief of Staff to the VP") == "c_level"


# ---- matching ---------------------------------------------------------------------------------

def test_word_order_does_not_matter():
    """The exact failure the tester hit, in their own words."""
    from nexus.relevance.job_levels import matches_title

    spec = {"job_levels": ["director", "head"], "title_keywords": ["facilities"]}
    for title in ("Head of Facilities", "Facilities Head", "Facilities Director",
                  "Director of Facilities", "Director, Facilities"):
        assert matches_title(title, spec), f"{title!r} should match Facilities leadership"


def test_the_level_gate_excludes_the_wrong_seniority():
    from nexus.relevance.job_levels import matches_title

    spec = {"job_levels": ["director", "vp"], "title_keywords": ["facilities"]}
    assert not matches_title("Facilities Coordinator", spec)
    assert not matches_title("Facilities Manager", spec)


def test_excluded_keywords_win():
    """'Director of Facilities' yes; 'Assistant Director of Facilities' no — it satisfies both other
    gates and is not the person the rep meant."""
    from nexus.relevance.job_levels import matches_title

    spec = {"job_levels": ["director"], "title_keywords": ["facilities"],
            "exclude_title_keywords": ["assistant", "deputy"]}
    assert matches_title("Director of Facilities", spec)
    assert not matches_title("Assistant Director of Facilities", spec)
    assert not matches_title("Deputy Director, Facilities", spec)


def test_an_empty_spec_matches_everything():
    """THE regression guard. Every existing workspace has no job_levels and no title_keywords, and
    must keep seeing exactly the contacts it sees today."""
    from nexus.relevance.job_levels import matches_title

    for title in ("Software Engineer", "CEO", "", "Facilities Director"):
        assert matches_title(title, {}) is True
        assert matches_title(title, None) is True


def test_keywords_alone_work_without_levels():
    from nexus.relevance.job_levels import matches_title

    spec = {"title_keywords": ["facilities"]}
    assert matches_title("Facilities Coordinator", spec)
    assert not matches_title("Software Engineer", spec)


def test_levels_alone_work_without_keywords():
    from nexus.relevance.job_levels import matches_title

    spec = {"job_levels": ["c_level"]}
    assert matches_title("Chief Financial Officer", spec)
    assert not matches_title("Facilities Manager", spec)


def test_multiple_keywords_are_an_or():
    """A GTM team targets 'facilities OR workplace OR real estate', not all three at once."""
    from nexus.relevance.job_levels import matches_title

    spec = {"job_levels": ["director"], "title_keywords": ["facilities", "workplace"]}
    assert matches_title("Director of Workplace Experience", spec)
    assert matches_title("Facilities Director", spec)
    assert not matches_title("Director of Engineering", spec)


# ---- query expansion --------------------------------------------------------------------------

def test_expand_titles_produces_searchable_phrases():
    """The contact-search provider queries a web index, so it needs phrases, not a predicate."""
    from nexus.relevance.job_levels import expand_titles

    out = {o.lower() for o in expand_titles(
        {"job_levels": ["director", "head"], "title_keywords": ["facilities"]}
    )}
    assert "director of facilities" in out
    assert "facilities director" in out
    assert "head of facilities" in out


def test_expansion_is_bounded():
    """Each phrase becomes a query, and each query is a billed search call."""
    from nexus.relevance.job_levels import expand_titles

    spec = {"job_levels": ["c_level", "vp", "head", "director", "manager"],
            "title_keywords": ["facilities", "workplace", "real estate", "operations"]}
    assert len(expand_titles(spec, limit=8)) <= 8


def test_expansion_with_no_keywords_returns_level_words():
    from nexus.relevance.job_levels import expand_titles

    assert set(expand_titles({"job_levels": ["vp", "head"]})) == {"VP", "Head"}


def test_an_empty_spec_expands_to_nothing():
    """No spec must not invent queries — that would spend search credits on a workspace that asked
    for nothing."""
    from nexus.relevance.job_levels import expand_titles

    assert expand_titles({}) == []
    assert expand_titles(None) == []


def test_spec_from_icp_always_returns_the_three_keys():
    from nexus.relevance.job_levels import spec_from_icp

    assert spec_from_icp(None) == {
        "job_levels": [], "title_keywords": [], "exclude_title_keywords": []
    }
    assert spec_from_icp({"job_levels": ["vp"]})["job_levels"] == ["vp"]
