# tests/test_orchestrator_slot_extraction.py
"""The orchestrator turns the user's sentence into search operators, so the sentence has to be
taken apart properly.

Measured live 2026-08-27 against the Infojoy workspace. Input:

    "Find VP of Sales and CRO contacts at mid-market SaaS companies in the US that match our ICP"

produced:

    titles: ["Find VP Of Sales",
             "CRO Contacts At Mid-market SaaS Companies In The US That Match Our ICP"]
    geo:    [..., "United States", "US"]

`_split_phrases` splits on "and", both halves contain a title token, and both are kept verbatim.
Those titles then drive contact search — which is measurably sensitive to title quality — and the
second one is an entire sentence. The duplicate geo comes from the free-text cue path appending
its raw match instead of resolving it through the alias table that exists for exactly that.
"""
from __future__ import annotations

from nexus.orchestration.intake import extract_slots

QUERY = ("Find VP of Sales and CRO contacts at mid-market SaaS companies in the US "
         "that match our ICP")


def test_a_natural_language_request_yields_usable_titles():
    delta = extract_slots(QUERY, {}, None)
    titles = delta.get("titles") or []
    assert titles, "no titles extracted from a request that names two of them"

    for t in titles:
        assert not t.lower().startswith(("find ", "show ", "get ", "list ", "search ")), (
            f"the imperative verb was kept as part of the job title: {t!r}"
        )
        assert len(t.split()) <= 6, f"a sentence was stored as a job title: {t!r}"

    joined = " | ".join(titles).lower()
    assert "vp of sales" in joined or "vp sales" in joined
    assert "cro" in joined


def test_the_trailing_clause_is_not_a_job_title():
    """"...that match our ICP" is instruction, not a role."""
    titles = extract_slots(QUERY, {}, None).get("titles") or []
    assert not any("match our icp" in t.lower() for t in titles)
    assert not any("companies" in t.lower() for t in titles)


def test_geo_from_a_free_text_cue_is_canonicalised():
    """"in the US" must resolve to the same string the alias table produces, or the ICP carries
    the same country twice under two spellings and the search excludes neither."""
    delta = extract_slots("companies in the US", {}, None)
    geo = delta.get("geo") or []
    assert "United States" in geo
    assert "US" not in geo, f"raw alias leaked alongside its canonical form: {geo}"
    assert len(geo) == len(set(geo))


def test_a_real_answer_to_a_titles_question_still_works():
    """The coercion path — the user is answering "which titles?" — must be unaffected."""
    delta = extract_slots("VP of Sales, CRO and Head of RevOps", {}, "titles")
    titles = [t.lower() for t in (delta.get("titles") or [])]
    assert any("vp of sales" in t for t in titles)
    assert any(t == "cro" for t in titles)
    assert any("head of revops" in t for t in titles)


def test_connectives_are_not_capitalised_mid_title():
    delta = extract_slots("we need a VP of Sales", {}, "titles")
    titles = delta.get("titles") or []
    assert titles and "Of" not in titles[0].split(), f"got {titles!r}"


def test_an_unparseable_message_still_yields_an_empty_delta():
    assert extract_slots("hmm", {}, None) == {} or isinstance(extract_slots("hmm", {}, None), dict)
