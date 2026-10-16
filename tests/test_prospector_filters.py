"""Prospector's filter catalog and the strict models that validate every search.

Every filter a rep sets is validated before anything is sent to the data provider: unknown fields,
oversized lists, oversized values and stringly-typed booleans are refused. The payload carries only
the filters that have a value (the API ignores empties, and sending only what is needed is its own
documented rule) and never a sort field, because the API fixes the order itself.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from nexus.prospector.errors import ProspectorError

DOCUMENTED_CONTACT_FILTERS = {
    "FirstName", "LastName", "Titles", "Departments", "SeniorityLevels", "CompanyNames",
    "CompanyUrls", "IndustryTypes", "ExcludeIndustryTypes", "SubIndustries", "ExcludeSubIndustries",
    "SicCodes", "Countries", "States", "Cities", "ZipCodes", "RevenueRanges", "EmployeeSizeRanges",
    "Technologies", "Keywords", "ExcludeKeywords", "HasDirectPhone",
}
DOCUMENTED_COMPANY_FILTERS = {
    "CompanyNames", "CompanyUrls", "IndustryTypes", "ExcludeIndustryTypes", "SubIndustries",
    "ExcludeSubIndustries", "SicCodes", "Countries", "States", "Cities", "ZipCodes",
    "RevenueRanges", "EmployeeSizeRanges", "Technologies", "Keywords", "ExcludeKeywords",
    "HasLinkedIn",
}


def _filters(**values):
    from nexus.prospector.filters import SearchFilters

    return SearchFilters(**values)


def test_empty_values_are_dropped_and_no_sort_is_ever_sent():
    f = _filters(titles=["VP Sales", " "], company_names=[],
                 person_countries=["United States"], has_direct_phone=True)

    payload = f.api_payload("contacts")

    assert payload == {"Titles": ["VP Sales"], "Countries": ["United States"],
                       "HasDirectPhone": True}
    assert not any("sort" in k.lower() for k in payload)


def test_values_are_trimmed_and_duplicates_kept_out():
    f = _filters(titles=["  CFO ", "CFO", "cfo"])
    assert f.titles == ["CFO"]


def test_every_api_name_the_catalog_uses_is_a_documented_filter():
    from nexus.prospector.fields import SECTIONS

    for section in SECTIONS:
        for spec in section.fields:
            for view, api_name in spec.api.items():
                if not api_name:
                    continue
                documented = (DOCUMENTED_CONTACT_FILTERS if view == "contacts"
                              else DOCUMENTED_COMPANY_FILTERS)
                assert api_name in documented, f"{spec.key} -> {api_name} on {view}"


def test_a_list_longer_than_fifty_is_refused():
    with pytest.raises(ValidationError):
        _filters(titles=[f"title {i}" for i in range(51)])


def test_a_value_longer_than_120_characters_is_refused():
    with pytest.raises(ValidationError):
        _filters(company_names=["x" * 121])


def test_unknown_fields_are_refused():
    with pytest.raises(ValidationError):
        _filters(sort_by="name")


def test_a_boolean_must_be_a_real_boolean():
    with pytest.raises(ValidationError):
        _filters(has_direct_phone="false")


def test_a_companies_search_refuses_a_contact_only_filter():
    f = _filters(titles=["CFO"])
    with pytest.raises(ProspectorError) as err:
        f.api_payload("companies")
    assert err.value.status == 422
    assert "Job titles" in err.value.detail


def test_a_contacts_search_refuses_the_companies_only_linkedin_filter():
    with pytest.raises(ProspectorError):
        _filters(has_linkedin=True).api_payload("contacts")


def test_companies_location_goes_to_the_documented_fields():
    payload = _filters(company_cities=["Austin"], company_states=["TX"]).api_payload("companies")
    assert payload == {"Cities": ["Austin"], "States": ["TX"]}


def test_exclusions_travel_as_their_own_filters():
    payload = _filters(industries=["Software"], exclude_industries=["Staffing"],
                       keywords=["payments"], exclude_keywords=["crypto"]).api_payload("companies")
    assert payload == {"IndustryTypes": ["Software"], "ExcludeIndustryTypes": ["Staffing"],
                       "Keywords": ["payments"], "ExcludeKeywords": ["crypto"]}


def test_active_keys_names_only_what_is_set():
    assert _filters(titles=["CFO"], has_direct_phone=True).active_keys() == \
        ["titles", "has_direct_phone"]


def test_the_catalog_marks_workspace_only_sections():
    from nexus.prospector.fields import catalog

    contacts = {s["key"]: s for s in catalog("contacts")["sections"]}
    companies = {s["key"]: s for s in catalog("companies")["sections"]}

    for key in ("email_status", "lists", "signals", "funding", "job_postings"):
        assert contacts[key]["workspace_only"] is True, key
    assert "email_status" not in companies
    assert {"location", "company_location"} <= set(contacts)
    assert "location" not in companies and "company_location" in companies
    assert "phone" in contacts and "linkedin" in companies
    # Every section the API serves is offered in rail order with a label and its fields.
    assert all(s["label"] and s["fields"] for s in contacts.values())


def test_the_catalog_says_which_fields_work_where():
    from nexus.prospector.fields import catalog

    fields = {f["key"]: f for s in catalog("companies")["sections"] for f in s["fields"]}
    assert fields["sic_codes"]["api"] is True and fields["sic_codes"]["saved"] is False
    assert fields["company_cities"]["api"] is True and fields["company_cities"]["saved"] is True


def test_workspace_filters_are_strict():
    from nexus.prospector.filters import WorkspaceFilters

    w = WorkspaceFilters(email_statuses=["valid", "none"], funding=True, signal_days=30)
    assert w.funding is True and w.signal_days == 30
    with pytest.raises(ValidationError):
        WorkspaceFilters(email_statuses=["deliverable"])
    with pytest.raises(ValidationError):
        WorkspaceFilters(signal_days=45)
    with pytest.raises(ValidationError):
        WorkspaceFilters(list_ids=["../etc"])
