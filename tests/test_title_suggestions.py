"""Buyer titles that fit the offer (W4, SDR feedback 2026-10-29).

Reported: "Suggest titles" gave generic CXO and CEO titles and ignored the Title keywords and
Exclude keywords. The model is now asked with everything the ICP says, and a deterministic filter
then drops a title containing an exclude keyword, a C-level title unless the job levels allow it
or the companies are under 50 people, and a title already listed. The catalogue fallback goes
through the same filter.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from nexus.relevance.title_suggest import (
    build_prompt,
    c_level_allowed,
    from_catalogue,
    parse_titles,
    suggest_titles,
)
from tests.conftest import auth, signup

ICP = {
    "industries": ["Hospitals and Health Care"],
    "employee_min": 200, "employee_max": 2000,
    "title_keywords": ["facilities", "estates"],
    "exclude_title_keywords": ["assistant"],
    "job_levels": ["director", "head"],
    "buyer_titles": ["Director of Estates"],
    "product_context": "Energy monitoring for hospital buildings.",
    "value_props": [{"name": "Lower energy bills", "description": "Live meter data",
                     "pains_solved": ["rising energy costs", "manual meter readings"]}],
}


class _LLM:
    def __init__(self, titles: list, *, stub: bool = False, raises: bool = False):
        self.text = json.dumps({"titles": titles})
        self.stub = stub
        self.raises = raises
        self.messages = None

    async def complete(self, messages, **kwargs):
        if self.raises:
            raise RuntimeError("model down")
        self.messages = messages
        return SimpleNamespace(text=self.text, is_stub=self.stub)


async def test_only_the_title_that_fits_survives_the_filter():
    llm = _LLM([
        {"title": "Chief Executive Officer", "reason": "Signs everything"},
        {"title": "Assistant Facilities Manager", "reason": "Reads the meters"},
        {"title": "Head of Facilities", "reason": "Owns the energy budget for the estate",
         "department": "Facilities", "influence": "economic_buyer"},
    ])
    titles, source = await suggest_titles(ICP, llm=llm, limit=10)
    assert source == "ai"
    assert [t.title for t in titles] == ["Head of Facilities"]
    assert titles[0].reason == "Owns the energy budget for the estate"
    assert titles[0].buying_influence == "economic_buyer"


async def test_the_model_is_given_everything_the_icp_says():
    llm = _LLM([{"title": "Head of Facilities", "reason": "r"}])
    await suggest_titles(ICP, llm=llm)
    prompt = " ".join(m.content for m in llm.messages)
    for expected in ("Energy monitoring for hospital buildings", "Lower energy bills",
                     "rising energy costs", "Hospitals and Health Care", "200 to 2000",
                     "facilities, estates", "Never suggest a title containing any of: assistant",
                     "director, head", "Director of Estates", "Do not suggest C-level"):
        assert expected in prompt, expected


def test_c_level_is_allowed_by_job_levels_or_a_small_company():
    assert not c_level_allowed({"employee_min": 200, "employee_max": 2000})
    assert not c_level_allowed({}), "an unknown size is not a small company"
    assert c_level_allowed({"job_levels": ["c_level"], "employee_min": 5000})
    assert c_level_allowed({"employee_min": 10, "employee_max": 40})
    assert "Do not suggest C-level" not in build_prompt({"employee_max": 30}, limit=5)[1]


async def test_a_small_company_keeps_its_founder():
    llm = _LLM([{"title": "Founder", "reason": "Buys everything at 20 people"},
                {"title": "CEO", "reason": "Same"}])
    titles, _ = await suggest_titles({"employee_min": 5, "employee_max": 40}, llm=llm)
    assert [t.title for t in titles] == ["Founder", "CEO"]


async def test_a_title_already_listed_or_repeated_is_not_suggested():
    llm = _LLM([{"title": "Director of Estates", "reason": "listed"},
                {"title": "Head of Facilities", "reason": "a"},
                {"title": "head of  facilities", "reason": "again"}])
    titles, _ = await suggest_titles(ICP, llm=llm)
    assert [t.title for t in titles] == ["Head of Facilities"]


async def test_no_model_answer_falls_back_to_the_catalogue_through_the_same_filter():
    for llm in (None, _LLM([], raises=True), _LLM([{"title": "x"}], stub=True),
                _LLM([{"title": "CEO", "reason": "filtered out"}])):
        titles, source = await suggest_titles(ICP, llm=llm)
        assert source == "catalogue"
        assert titles, "the catalogue always has something"
        from nexus.relevance.job_levels import level_of

        assert all(level_of(t.title) != "c_level" for t in titles), [t.title for t in titles]
        assert all("assistant" not in t.title.lower() for t in titles)
    names = [t.title for t in from_catalogue(ICP, limit=10)]
    assert "Head of Facilities" in names[:3], "the title keywords rank the facilities roles up"


SALES_OFFER = {
    "employee_min": 50, "employee_max": 5000, "job_levels": ["c_level", "vp", "director"],
    "buyer_titles": ["Chief Revenue Officer (CRO)", "VP Sales"],
    "product_context": "AI buyer intent signals and verified contacts that reduce risk.",
}


async def test_titles_come_back_spelled_as_a_database_holds_them():
    """Measured 2026-11-19: U+2011 hyphens and bracketed abbreviations match no stored title."""
    llm = _LLM([{"title": "Account‑Based Marketing Manager", "reason": "r",
                 "influence": "champion"},
                {"title": "Chief Marketing Officer (CMO)", "reason": "r",
                 "influence": "economic_buyer"}])
    titles, _ = await suggest_titles(SALES_OFFER, llm=llm)
    assert [t.title for t in titles] == ["Chief Marketing Officer",
                                         "Account-Based Marketing Manager"]


async def test_a_listed_title_in_other_words_is_not_suggested_again():
    llm = _LLM([{"title": "Vice President of Sales", "reason": "listed as VP Sales"},
                {"title": "Chief Revenue Officer", "reason": "listed with its acronym"},
                {"title": "Head of Revenue Operations", "reason": "new", "influence": "champion"}])
    titles, _ = await suggest_titles(SALES_OFFER, llm=llm)
    assert [t.title for t in titles] == ["Head of Revenue Operations"]


async def test_support_roles_are_dropped_unless_the_offer_is_for_them():
    padding = [{"title": "Procurement Specialist", "reason": "buys"},
               {"title": "Compliance Officer", "reason": "checks"},
               {"title": "Director of Demand Generation", "reason": "owns pipeline",
                "influence": "economic_buyer"}]
    titles, _ = await suggest_titles(SALES_OFFER, llm=_LLM(padding))
    assert [t.title for t in titles] == ["Director of Demand Generation"], \
        "'buyer intent' and 'reduce risk' do not make a sales offer a procurement or compliance one"
    compliance = {**SALES_OFFER, "product_context": "Regulatory compliance monitoring for banks."}
    titles, _ = await suggest_titles(compliance, llm=_LLM(padding))
    assert "Compliance Officer" in [t.title for t in titles]


async def test_budget_owners_and_champions_rank_above_users():
    llm = _LLM([{"title": "Sales Development Representative", "reason": "u", "influence": "end_user"},
                {"title": "Head of Sales Development", "reason": "c", "influence": "champion"},
                {"title": "VP Marketing", "reason": "b", "influence": "economic_buyer"}])
    titles, _ = await suggest_titles(SALES_OFFER, llm=llm)
    assert [t.title for t in titles] == ["VP Marketing", "Head of Sales Development",
                                         "Sales Development Representative"]
    assert titles[0].priority_score > titles[-1].priority_score


def test_an_answer_cut_off_at_the_token_limit_keeps_its_complete_titles():
    """Measured 2026-11-19: three of four workspaces got the catalogue because the JSON ended
    mid-reason and nothing parsed."""
    cut = ('{"titles": [{"title": "Head of Growth Marketing", "reason": "r", "influence": '
           '"champion"}, {"title": "Director of Sales Technology", "reason": "r"}, '
           '{"title": "Vice President of Marketing Technology", "reason": "Oversees mart')
    assert [t["title"] for t in parse_titles(cut)] == ["Head of Growth Marketing",
                                                        "Director of Sales Technology"]


def test_a_title_written_in_lower_case_is_capitalised():
    assert parse_titles('["director of qa", "vp of sales-enablement"]') == [
        {"title": "Director of QA", "reason": "", "department": "", "influence": ""},
        {"title": "VP of Sales-Enablement", "reason": "", "department": "", "influence": ""}]
    assert parse_titles('["DevOps Manager"]')[0]["title"] == "DevOps Manager", "own casing kept"


def test_the_catalogue_offers_roles_the_offer_speaks_to_not_every_industrys():
    lead_gen = {
        "industries": ["Manufacturing", "Software Development", "Hospitals and Health Care"],
        "employee_min": 50, "employee_max": 5000,
        "product_context": "B2B lead generation: buyer intent data, verified contacts and "
                           "demand generation campaigns.",
    }
    names = [t.title for t in from_catalogue(lead_gen, limit=10)]
    assert names, "the catalogue still answers"
    for operational in ("Plant Manager", "Head of Maintenance", "Head of Facilities"):
        assert operational not in names, names


def test_an_unreadable_answer_reads_as_nothing():
    assert parse_titles("Here are some ideas: VP Sales") == []
    assert parse_titles("```json\n[\"Head of Facilities\"]\n```")[0]["title"] == \
        "Head of Facilities"
    assert parse_titles('{"titles": [{"title": "  "}, 7, {"title": "X", "influence": "boss"}]}') \
        == [{"title": "X", "reason": "", "department": "", "influence": ""}]


async def test_the_endpoint_asks_the_model_and_returns_only_what_fits(client, monkeypatch):
    import nexus.agents.llm as llm_mod

    llm = _LLM([{"title": "CEO", "reason": "no"},
                {"title": "Head of Facilities", "reason": "Owns the estate budget"}])
    monkeypatch.setattr(llm_mod, "get_llm_provider", lambda: llm)
    token = await signup(client, slug="tsg1", email="o@tsg1.io", company="TSG")
    r = await client.post("/api/relevance/suggest-titles", headers=auth(token), json=ICP)
    assert r.status_code == 200, r.text
    assert [(t["title"], t["reason"]) for t in r.json()] == [
        ("Head of Facilities", "Owns the estate budget")]


async def test_the_endpoint_reads_a_saved_services_icp_when_nothing_is_posted(client,
                                                                             monkeypatch):
    import nexus.agents.llm as llm_mod

    llm = _LLM([{"title": "Head of Facilities", "reason": "r"}])
    monkeypatch.setattr(llm_mod, "get_llm_provider", lambda: llm)
    token = await signup(client, slug="tsg2", email="o@tsg2.io", company="TSG2")
    sid = (await client.post("/api/relevance/services", headers=auth(token), json={
        "name": "Energy", "icp": {"industries": ["Hospitals and Health Care"]},
        "product_context": "Energy monitoring for hospital buildings."})).json()["id"]
    r = await client.post("/api/relevance/suggest-titles", headers=auth(token),
                          json={"service_id": sid})
    assert r.status_code == 200, r.text
    assert "Energy monitoring for hospital buildings" in " ".join(
        m.content for m in llm.messages)
