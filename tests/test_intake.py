# tests/test_intake.py
"""Unit tests for the deterministic orchestrator brain (no DB, no network)."""
from __future__ import annotations

from nexus.orchestration.intake import missing_required


def test_missing_required_companies_truth_table():
    # Empty ICP, companies target → industries, geo, company_size all missing.
    assert missing_required({}, "companies") == ["industries", "geo", "company_size"]
    # Industries present (or description) clears the first slot.
    assert missing_required({"industries": ["Fintech"]}, "companies") == ["geo", "company_size"]
    assert missing_required({"icp_description": "B2B fintech"}, "companies") == [
        "geo",
        "company_size",
    ]
    # Geo present.
    assert missing_required(
        {"industries": ["Fintech"], "geo": ["United States"]}, "companies"
    ) == ["company_size"]
    # Fully specified companies ICP → nothing missing.
    assert (
        missing_required(
            {
                "industries": ["Fintech"],
                "geo": ["United States"],
                "company_size": {"min": 200, "max": 5000},
            },
            "companies",
        )
        == []
    )


def test_missing_required_contacts_needs_titles_not_size():
    base = {"industries": ["Fintech"], "geo": ["US"]}
    assert missing_required(base, "contacts") == ["titles"]
    assert missing_required({**base, "titles": ["VP Sales"]}, "contacts") == []


def test_missing_required_defaults_target_to_companies():
    # target None behaves like "companies".
    assert missing_required({}, None) == ["industries", "geo", "company_size"]


from nexus.orchestration.intake import extract_slots, merge_icp


def test_extract_rich_first_message_fills_multiple_slots():
    delta = extract_slots("Find B2B fintech companies in the US with 200-5000 employees", {}, None)
    assert "Fintech" in delta["industries"]
    assert "United States" in delta["geo"]
    assert delta["company_size"] == {"min": 200, "max": 5000}


def test_extract_named_size_bands():
    assert extract_slots("mid-market", {}, "company_size")["company_size"] == {"min": 200, "max": 1000}
    assert extract_slots("enterprise only", {}, "company_size")["company_size"] == {"min": 1000, "max": None}
    assert extract_slots("under 500", {}, "company_size")["company_size"] == {"min": None, "max": 500}
    assert extract_slots("over 1000", {}, "company_size")["company_size"] == {"min": 1000, "max": None}


def test_extract_coerces_bare_answer_to_pending_slot():
    # Answering a geo question with a bare country name still fills geo.
    assert extract_slots("Canada and Germany", {}, "geo")["geo"] == ["Canada", "Germany"]
    # Answering an industries question with an unknown noun phrase still fills industries —
    # the full phrase wins over an incidental keyword hit because the user is answering it.
    assert extract_slots("logistics tech", {}, "industries")["industries"] == ["Logistics Tech"]
    # Answering a titles question.
    assert extract_slots("VP Sales, CRO", {}, "titles")["titles"] == ["VP Sales", "CRO"]


def test_merge_unions_lists_and_overrides_size():
    state = {"industries": ["Fintech"], "company_size": {"min": 10, "max": 50}}
    out = merge_icp(state, {"industries": ["fintech", "SaaS"], "company_size": {"min": 200, "max": 5000}})
    # Case-insensitive dedupe, order preserved, new value appended.
    assert out["industries"] == ["Fintech", "SaaS"]
    assert out["company_size"] == {"min": 200, "max": 5000}


import pytest

from nexus.agents.llm import LLMMessage, StubLLMProvider


@pytest.mark.asyncio
async def test_stub_clarify_question_per_slot():
    stub = StubLLMProvider()
    r = await stub.complete(
        [LLMMessage(role="user", content="x")],
        purpose="clarify_question",
        variables={"slot": "geo", "icp_state": {"industries": ["Fintech"]}},
    )
    assert "countr" in r.text.lower() or "region" in r.text.lower()


@pytest.mark.asyncio
async def test_stub_chat_summary_is_capped_and_structured():
    stub = StubLLMProvider()
    r = await stub.complete(
        [LLMMessage(role="user", content="x")],
        purpose="chat_summary",
        variables={
            "prior": "",
            "target": "companies",
            "icp_state": {"industries": ["Fintech"], "geo": ["United States"]},
        },
    )
    assert "Fintech" in r.text
    assert len(r.text) <= 600  # ~150-token cap * 4 chars


from nexus.orchestration.intake import ContextEnvelope, IntakeController


def test_envelope_trims_recency_before_summary():
    msgs = [{"role": "user", "content": "m" * 400} for _ in range(8)]
    env = ContextEnvelope.build(
        icp_state={"industries": ["Fintech"]},
        target="companies",
        account_id=None,
        missing_slots=["geo"],
        context_summary="s" * 400,
        recent_messages=msgs,
        budget=120,            # tiny budget forces trimming
        recency_window=4,
        summary_token_cap=150,
    )
    # Recency window is trimmed first (oldest dropped); never exceeds K.
    assert len(env.recent_messages) < 4
    # Summary survives if possible; here the budget is so tight it is also truncated.
    assert env.token_estimate <= 120


def test_envelope_keeps_recent_when_budget_allows():
    msgs = [{"role": "user", "content": "hi"} for _ in range(6)]
    env = ContextEnvelope.build(
        icp_state={}, target="companies", account_id=None, missing_slots=[],
        context_summary="short", recent_messages=msgs,
        budget=1200, recency_window=4, summary_token_cap=150,
    )
    assert len(env.recent_messages) == 4  # last K kept


@pytest.mark.asyncio
async def test_controller_asks_one_question_when_missing():
    ctrl = IntakeController()
    d = await ctrl.advance(
        icp_state={}, target="companies", missing_slots=[],
        context_summary="", user_text="find me some companies", is_first_turn=True,
    )
    assert d.action == "clarify"
    assert d.data["slot"] == "industries"
    assert d.assistant_kind == "clarifying_question"
    assert d.assistant_text  # phrased question
    assert d.data["suggestions"]


@pytest.mark.asyncio
async def test_controller_launches_on_complete_first_turn():
    ctrl = IntakeController()
    d = await ctrl.advance(
        icp_state={}, target="companies", missing_slots=[], context_summary="",
        user_text="Find Fintech companies in the US with 200-5000 employees",
        is_first_turn=True,
    )
    assert d.action == "launch"
    assert d.missing_slots == []
    assert d.icp_state["company_size"] == {"min": 200, "max": 5000}


@pytest.mark.asyncio
async def test_controller_confirms_then_launches_on_go():
    ctrl = IntakeController()
    complete = {"industries": ["Fintech"], "geo": ["United States"],
                "company_size": {"min": 200, "max": 5000}}
    # Not first turn, no affirmative → confirm (ready), do not launch.
    ready = await ctrl.advance(icp_state=complete, target="companies", missing_slots=[],
                               context_summary="", user_text="200 to 5000", is_first_turn=False)
    assert ready.action == "ready"
    # Explicit go → launch.
    go = await ctrl.advance(icp_state=complete, target="companies", missing_slots=[],
                            context_summary="", user_text="go", is_first_turn=False)
    assert go.action == "launch"


# -- Bug fixes: re-targeting, URL/geo/free-text-ICP capture, LLM no-op on stub ---------
from nexus.orchestration.intake import infer_target


def test_infer_target_retargets_away_from_companies():
    # The reported bug: once "companies" was set, "I want prospects not companies" never took.
    assert infer_target("I don't want companies, I want prospects", "companies") == "contacts"
    assert infer_target("actually I want prospects not companies", "companies") == "contacts"
    assert infer_target("show me the VP Sales decision makers", "companies") == "contacts"


def test_infer_target_negation_picks_opposite():
    assert infer_target("I want leads, not companies", "companies") == "contacts"
    assert infer_target("give me accounts, not individual people", "contacts") == "companies"


def test_infer_target_keeps_current_when_silent():
    # No company/contact signal on this turn → keep what we had.
    assert infer_target("200 to 5000", "contacts") == "contacts"
    assert infer_target("200 to 5000", "companies") == "companies"
    # Cold start with no signal defaults to companies.
    assert infer_target("hello", None) == "companies"


def test_extract_captures_seed_url():
    assert extract_slots("here's our site acme.com", {}, None)["seed_url"] == "https://acme.com"
    assert (
        extract_slots("check https://www.acme.io/about", {}, None)["seed_url"]
        == "https://www.acme.io/about"
    )
    # Abbreviations with short trailing segments are not URLs.
    assert "seed_url" not in extract_slots("e.g. mid-market saas", {}, None)


def test_extract_captures_freetext_geo_via_cue():
    # A city the alias gazetteer doesn't list is still captured after an explicit cue.
    assert extract_slots("companies based in Austin", {}, None)["geo"] == ["Austin"]
    # Industry/size words after a cue are filtered out, not mistaken for a place.
    assert "geo" not in extract_slots("targeting Fintech", {}, None)


def test_extract_captures_freetext_icp_description():
    # Explicit ICP cue.
    d1 = extract_slots("We sell observability tooling to engineering orgs", {}, None)
    assert d1["icp_description"] == "We sell observability tooling to engineering orgs"
    # No keyword, but clearly descriptive (>=6 words + qualifier).
    d2 = extract_slots("teams that manage large fleets of delivery vehicles", {}, None)
    assert "icp_description" in d2


def test_extract_icp_description_not_triggered_on_short_message():
    # Must not hijack the canonical "ask for industries" flow.
    assert "icp_description" not in extract_slots("find me some companies", {}, None)
    # And not while the user is answering a pending open-vocabulary slot.
    assert "icp_description" not in extract_slots("logistics tech", {}, "industries")


@pytest.mark.asyncio
async def test_controller_retargets_then_asks_titles():
    ctrl = IntakeController()
    complete = {"industries": ["Fintech"], "geo": ["United States"],
                "company_size": {"min": 200, "max": 5000}}
    d = await ctrl.advance(icp_state=complete, target="companies", missing_slots=[],
                           context_summary="", user_text="actually I want prospects not companies",
                           is_first_turn=False)
    assert d.target == "contacts"
    assert d.action == "clarify"
    assert d.data["slot"] == "titles"


@pytest.mark.asyncio
async def test_stub_intake_understanding_is_empty_noop():
    # The stub must return empty JSON so the LLM understanding pass never perturbs the
    # deterministic offline path.
    stub = StubLLMProvider()
    r = await stub.complete(
        [LLMMessage(role="user", content="anything")],
        purpose="intake_understanding",
        variables={"summary": "", "icp_state": {}, "target": "companies"},
    )
    assert r.text.strip() == "{}"
