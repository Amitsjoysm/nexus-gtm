"""What a rep can filter on, per view, and where each filter works.

One declaration feeds three readers: ``filters.py`` turns a request into the vendor payload with it,
``saved.py`` decides which filters the Saved tab can apply to workspace records, and
``GET /prospector/filters`` serves it to the screen. The frontend therefore never hard-codes a
vocabulary or an API name; a second copy there would drift, and the first thing to drift would be a
filter the screen offers and the server ignores.

Rules carried here (spec §8, decisions D14-D16):

* **Sections, not raw API fields.** "Job titles" is three API filters (titles, departments,
  seniority); "Industry & keywords" carries every Exclude form. AND across sections, OR within one.
* **Workspace-only sections** (Email status, Lists, Signals, Funding, Job postings) exist only in
  NEXUS data, so they can only match records the workspace holds: they work on the Saved tab and show
  disabled elsewhere. Email Opened is absent on purpose: NEXUS does not track opens.
* **Two location levels in the Contacts view** (D15): the person's and the company's. Their API
  names are what the provider actually honours, recorded from the first real calls; an empty name
  means "not searchable at the provider yet", and the section is disabled on Total/Net new rather
  than sent under a guessed name that the API might silently ignore.
* ``options`` are the provider's observed vocabulary for small bounded sets. Empty means free text.
"""
from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Literal

VIEWS = ("contacts", "companies")
SCOPES = ("total", "net_new", "saved")
BOTH = VIEWS

Kind = Literal["text", "text_list", "enum_list", "bool", "id_list", "window"]


@dataclass(frozen=True, slots=True)
class FieldSpec:
    key: str
    label: str
    kind: Kind
    #: view -> the provider's filter name. "" (or absent) = not searchable at the provider.
    api: dict[str, str] = dc_field(default_factory=dict)
    options: tuple[str, ...] = ()
    #: Whether the Saved tab can apply it to workspace records.
    saved: bool = True


@dataclass(frozen=True, slots=True)
class SectionSpec:
    key: str
    label: str
    views: tuple[str, ...]
    fields: tuple[FieldSpec, ...]
    workspace_only: bool = False
    hint: str = ""


def _both(name: str) -> dict[str, str]:
    return {"contacts": name, "companies": name}


_LOCATION_HINT = "City, state, country or postal code."

SECTIONS: tuple[SectionSpec, ...] = (
    SectionSpec("name", "Name", ("contacts",), (
        FieldSpec("first_name", "First name", "text", {"contacts": "FirstName"}),
        FieldSpec("last_name", "Last name", "text", {"contacts": "LastName"}),
    )),
    SectionSpec("job_titles", "Job titles", ("contacts",), (
        FieldSpec("titles", "Job titles", "text_list", {"contacts": "Titles"}),
        FieldSpec("departments", "Departments", "text_list", {"contacts": "Departments"}),
        FieldSpec("seniorities", "Seniority", "enum_list", {"contacts": "SeniorityLevels"}),
    )),
    SectionSpec("company", "Company", BOTH, (
        FieldSpec("company_names", "Company names", "text_list", _both("CompanyNames")),
        FieldSpec("company_domains", "Company websites", "text_list", _both("CompanyUrls")),
    )),
    # The person's location. Provisional until the first real calls say which set the contacts
    # endpoint's documented location filters match (spec §11).
    SectionSpec("location", "Location", ("contacts",), (
        FieldSpec("person_countries", "Countries", "text_list", {"contacts": "Countries"}),
        FieldSpec("person_states", "States", "text_list", {"contacts": "States"}),
        FieldSpec("person_cities", "Cities", "text_list", {"contacts": "Cities"}),
        FieldSpec("person_zips", "Postal codes", "text_list", {"contacts": "ZipCodes"}),
    ), hint=_LOCATION_HINT),
    # The company's location. On the Companies view these are the documented filters; on the
    # Contacts view the provider's name for the company-level set is not known until observed.
    SectionSpec("company_location", "Company location", BOTH, (
        FieldSpec("company_countries", "Countries", "text_list",
                  {"contacts": "", "companies": "Countries"}),
        FieldSpec("company_states", "States", "text_list", {"contacts": "", "companies": "States"}),
        FieldSpec("company_cities", "Cities", "text_list", {"contacts": "", "companies": "Cities"}),
        FieldSpec("company_zips", "Postal codes", "text_list",
                  {"contacts": "", "companies": "ZipCodes"}),
    ), hint=_LOCATION_HINT),
    SectionSpec("employees", "Employees", BOTH, (
        FieldSpec("employee_sizes", "Company size", "enum_list", _both("EmployeeSizeRanges")),
    )),
    SectionSpec("revenue", "Revenue", BOTH, (
        FieldSpec("revenue_ranges", "Annual revenue", "enum_list", _both("RevenueRanges")),
    )),
    SectionSpec("industry", "Industry & keywords", BOTH, (
        FieldSpec("industries", "Industries", "text_list", _both("IndustryTypes")),
        FieldSpec("exclude_industries", "Exclude industries", "text_list",
                  _both("ExcludeIndustryTypes")),
        FieldSpec("sub_industries", "Sub-industries", "text_list", _both("SubIndustries")),
        FieldSpec("exclude_sub_industries", "Exclude sub-industries", "text_list",
                  _both("ExcludeSubIndustries")),
        FieldSpec("sic_codes", "SIC codes", "text_list", _both("SicCodes"), saved=False),
        FieldSpec("keywords", "Keywords", "text_list", _both("Keywords")),
        FieldSpec("exclude_keywords", "Exclude keywords", "text_list", _both("ExcludeKeywords")),
    )),
    SectionSpec("technologies", "Technologies", BOTH, (
        FieldSpec("technologies", "Technologies used", "text_list", _both("Technologies")),
    )),
    SectionSpec("phone", "Phone", ("contacts",), (
        FieldSpec("has_direct_phone", "Has a direct phone", "bool",
                  {"contacts": "HasDirectPhone"}),
    )),
    SectionSpec("linkedin", "LinkedIn", ("companies",), (
        FieldSpec("has_linkedin", "Has a LinkedIn page", "bool", {"companies": "HasLinkedIn"}),
    )),
    # ---- NEXUS-only: they match records the workspace holds, so they work on the Saved tab only.
    SectionSpec("email_status", "Email status", ("contacts",), (
        FieldSpec("email_statuses", "Verification", "enum_list",
                  options=("valid", "catch_all", "risky", "unknown", "invalid", "none")),
    ), workspace_only=True),
    SectionSpec("lists", "Lists", BOTH, (
        FieldSpec("list_ids", "On a list", "id_list"),
    ), workspace_only=True),
    SectionSpec("signals", "Signals", BOTH, (
        FieldSpec("signal_kinds", "Signal kinds", "enum_list"),
        FieldSpec("signal_days", "Within", "window", options=("30", "90", "180")),
    ), workspace_only=True),
    SectionSpec("funding", "Funding", BOTH, (
        FieldSpec("funding", "Raised funding recently", "bool"),
    ), workspace_only=True),
    SectionSpec("job_postings", "Job postings", BOTH, (
        FieldSpec("job_postings", "Hiring recently", "bool"),
    ), workspace_only=True),
)

_BY_KEY: dict[str, tuple[SectionSpec, FieldSpec]] = {
    f.key: (s, f) for s in SECTIONS for f in s.fields
}


def field(key: str) -> FieldSpec:
    return _BY_KEY[key][1]


def section_of(key: str) -> SectionSpec:
    return _BY_KEY[key][0]


def sections_for(view: str) -> list[SectionSpec]:
    return [s for s in SECTIONS if view in s.views]


def catalog(view: str) -> dict:
    """The rail for one view, in order, as the screen draws it."""
    from nexus.models.signal import SIGNAL_KINDS

    sections = []
    for section in sections_for(view):
        fields_out = []
        for spec in section.fields:
            options = spec.options
            if spec.key == "signal_kinds":
                options = tuple(sorted(SIGNAL_KINDS))
            fields_out.append({
                "key": spec.key, "label": spec.label, "kind": spec.kind,
                "options": list(options),
                # Total and Net new can use it only when the provider has a name for it.
                "api": bool(spec.api.get(view)) and not section.workspace_only,
                "saved": spec.saved,
            })
        sections.append({"key": section.key, "label": section.label, "hint": section.hint,
                         "workspace_only": section.workspace_only, "fields": fields_out})
    return {"view": view, "sections": sections}
