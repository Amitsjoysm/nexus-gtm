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
