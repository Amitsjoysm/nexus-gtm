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
