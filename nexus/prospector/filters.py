"""The validated shape of every Prospector filter, and the vendor payload built from it.

Validation is the first line of the cost guard as well as of security: every list is capped at 50
values and every value at 120 characters, booleans must be real booleans (a JSON "false" is a truthy
string in Python), and unknown fields are refused rather than ignored, so a typo cannot silently widen
a search that the workspace pays for row by row.

``api_payload`` sends only the filters that carry a value and never a sort field: the API fixes the
order itself and ignores empties, and "send only the filters you need" is its documented rule.
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StrictBool, StringConstraints

from nexus.prospector import fields as catalog
from nexus.prospector.errors import ProspectorError

MAX_VALUES = 50
MAX_CHARS = 120


def _clean_list(raw):
    """Trimmed, blank-free, case-insensitively distinct, in the order given."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        return raw          # let the type check refuse it
    seen: dict[str, str] = {}
    for item in raw:
        if not isinstance(item, str):
            return raw      # a non-string is the type check's to refuse
        value = " ".join(item.split())
        if value and value.lower() not in seen:
            seen[value.lower()] = value
    return list(seen.values())


def _clean_text(raw):
    if isinstance(raw, str):
        value = " ".join(raw.split())
        return value or None
    return raw


Text = Annotated[str, StringConstraints(min_length=1, max_length=MAX_CHARS)]
TextList = Annotated[list[Text], BeforeValidator(_clean_list), Field(max_length=MAX_VALUES)]
OptionalText = Annotated[Text | None, BeforeValidator(_clean_text)]
IdStr = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,64}$")]


class SearchFilters(BaseModel):
    """Every filter the provider can apply. Keys match ``fields.py``."""

    model_config = ConfigDict(extra="forbid")

    first_name: OptionalText = None
    last_name: OptionalText = None
    titles: TextList = []
    departments: TextList = []
    seniorities: TextList = []
    company_names: TextList = []
    company_domains: TextList = []
    person_countries: TextList = []
    person_states: TextList = []
    person_cities: TextList = []
    person_zips: TextList = []
    company_countries: TextList = []
    company_states: TextList = []
    company_cities: TextList = []
    company_zips: TextList = []
    employee_sizes: TextList = []
    revenue_ranges: TextList = []
    industries: TextList = []
    exclude_industries: TextList = []
    sub_industries: TextList = []
    exclude_sub_industries: TextList = []
    sic_codes: TextList = []
    keywords: TextList = []
    exclude_keywords: TextList = []
    technologies: TextList = []
    has_direct_phone: StrictBool | None = None
    has_linkedin: StrictBool | None = None

    def filled(self) -> dict[str, object]:
        """The filters that carry a value, in declaration order."""
        out: dict[str, object] = {}
        for key in type(self).model_fields:
            value = getattr(self, key)
            if value is None or value == [] or value == "":
                continue
            out[key] = value
        return out

    def active_keys(self) -> list[str]:
        """Names only: what is safe to log (values can be people's names)."""
        return list(self.filled())

    def api_payload(self, view: str) -> dict[str, object]:
        """The provider's ``Filters`` object for this view. Refuses a filter the view cannot take."""
        payload: dict[str, object] = {}
        for key, value in self.filled().items():
            section = catalog.section_of(key)
            spec = catalog.field(key)
            if view not in section.views:
                raise ProspectorError(
                    422, f"{section.label} is not a filter for {view}. Clear it or switch views.")
            api_name = spec.api.get(view, "")
            if not api_name:
                raise ProspectorError(
                    422, f"{section.label}: {spec.label} cannot be searched at the data provider "
                         "yet. It works on the Saved tab.")
            payload[api_name] = spec.provider_values(value) if isinstance(value, list) else value
        return payload

    def canonical(self, view: str) -> dict[str, object]:
        """A stable form for cache keys: the payload with list values sorted."""
        out = {}
        for name, value in sorted(self.api_payload(view).items()):
            out[name] = sorted(value, key=str.lower) if isinstance(value, list) else value
        return out


EmailStatus = Literal["valid", "catch_all", "risky", "unknown", "invalid", "none"]


def _signal_kind(value: str) -> str:
    from nexus.models.signal import SIGNAL_KINDS

    if value not in SIGNAL_KINDS:
        raise ValueError(f"unknown signal kind {value!r}")
    return value


SignalKind = Annotated[str, StringConstraints(max_length=32), BeforeValidator(_signal_kind)]


class WorkspaceFilters(BaseModel):
    """NEXUS-only filters (D14): they can match only records the workspace holds."""

    model_config = ConfigDict(extra="forbid")

    email_statuses: list[EmailStatus] = Field(default_factory=list, max_length=6)
    list_ids: list[IdStr] = Field(default_factory=list, max_length=MAX_VALUES)
    signal_kinds: list[SignalKind] = Field(default_factory=list, max_length=16)
    funding: StrictBool = False
    job_postings: StrictBool = False
    signal_days: Literal[30, 90, 180] = 90

    def active(self) -> bool:
        return bool(self.email_statuses or self.list_ids or self.signal_kinds or self.funding
                    or self.job_postings)
