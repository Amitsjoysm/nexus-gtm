# nexus/integrations/provenance.py
"""Per-field provenance for multi-source data.

Every value the :class:`~nexus.integrations.registry.DataSourceRegistry` merges carries where it
came from and how much we trust it, so the relevance engine and the grounded-send gate can reason
about data quality ("employee_count via InfoJoy 0.95, tech via Apify 0.6, news via Scrapegraph
0.4"). ``Sourced`` is the typed carrier; :func:`merge_sourced` is the highest-confidence-wins rule.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Generic, TypeVar

T = TypeVar("T")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class Sourced(Generic[T]):
    """A value tagged with its origin, a 0–1 confidence, and when it was fetched."""

    value: T
    source: str
    confidence: float = 0.5
    fetched_at: str = field(default_factory=_now_iso)

    def as_dict(self) -> dict:
        return {
            "value": self.value,
            "source": self.source,
            "confidence": self.confidence,
            "fetched_at": self.fetched_at,
        }


def merge_sourced(current: "Sourced[T] | None", incoming: "Sourced[T] | None") -> "Sourced[T] | None":
    """Highest-confidence-wins. Ties keep ``current`` (the earlier / higher-priority source)."""
    if incoming is None:
        return current
    if current is None:
        return incoming
    return incoming if incoming.confidence > current.confidence else current


# ---- where each stored value came from (SDR feedback, 2026-10-26) ---------------------------------
#
# SDRs asked to see the source of every account and contact so they can check it. The rows carried
# one column each (`accounts.source`, `contacts.enrichment_source`) saying how the ROW arrived, and
# the email finder overwrote the contact's. `field_sources` (migration 0073) holds, per field, who
# wrote it, the evidence (a page, a profile, the pages an AI read), and when:
#
#     {"industry": {"source": "linkedin", "ref": "https://www.linkedin.com/company/acme/",
#                   "at": "2026-10-26T09:14:02+00:00"}}
#
# Writers call `record` for what they set. A value written before this existed has no entry, and
# `display_sources` says so ("not_recorded") rather than guessing who wrote it.

#: Account columns, then the enrichment extras kept in `custom_fields`.
ACCOUNT_FIELDS = ("name", "domain", "industry", "country", "region", "postal_code",
                  "employee_count", "annual_revenue", "tech_stack")
ACCOUNT_CUSTOM_FIELDS = ("description", "linkedin_url", "sub_industry", "revenue", "city")
#: "person" is the human: who they are and that they work here. The rest are their details.
CONTACT_FIELDS = ("person", "title", "email", "phone", "linkedin_url")

MAX_REFS = 5


def record(row, fields, source: str, *, ref: str | None = None, refs=None,
           detail: str | None = None, at: datetime | None = None) -> None:
    """Note that ``source`` wrote ``fields`` on ``row`` (an Account, Contact or Company).

    Writes a new dict, so SQLAlchemy sees the JSON change. Never raises for a row without the
    column (a test double), because provenance must not break the write it describes."""
    names = [fields] if isinstance(fields, str) else [f for f in fields or [] if f]
    if not names or not hasattr(row, "field_sources"):
        return
    entry: dict = {"source": source, "at": (at or datetime.now(timezone.utc)).isoformat()}
    if ref:
        entry["ref"] = str(ref)[:500]
    links = [str(r)[:500] for r in (refs or []) if r][:MAX_REFS]
    if links:
        entry["refs"] = links
        entry.setdefault("ref", links[0])
    if detail:
        entry["detail"] = str(detail)[:200]
    current = dict(getattr(row, "field_sources", None) or {})
    for name in names:
        current[name] = dict(entry)
    row.field_sources = current


def _present(row) -> list[str]:
    """The fields of ``row`` that hold a value."""
    from nexus.models.account import Account, Contact

    if isinstance(row, Contact):
        present = ["person"]
        present += [f for f in CONTACT_FIELDS[1:] if getattr(row, f, None)]
        return present
    present = [f for f in ACCOUNT_FIELDS if getattr(row, f, None)]
    if isinstance(row, Account):
        custom = row.custom_fields if isinstance(row.custom_fields, dict) else {}
        present += [f for f in ACCOUNT_CUSTOM_FIELDS if custom.get(f)]
    return present


def record_present(row, source: str, **kwargs) -> list[str]:
    """``record`` every field ``row`` holds a value for: for a row a single source wrote whole (an
    upload, a CRM pull, a provider record, a person typing it). Returns the fields recorded."""
    fields = _present(row)
    record(row, fields, source, **kwargs)
    return fields


def display_sources(row) -> dict:
    """What the API shows: every recorded source, plus ``not_recorded`` for a held value written
    before sources were kept, with how the row itself arrived (``arrived_via``) and when."""
    from nexus.models.account import Contact

    out = {k: dict(v) for k, v in (getattr(row, "field_sources", None) or {}).items()
           if isinstance(v, dict)}
    arrived = (row.enrichment_source if isinstance(row, Contact) else row.source) or "unknown"
    created = getattr(row, "created_at", None)
    for name in _present(row):
        if name not in out:
            out[name] = {"source": "not_recorded", "arrived_via": arrived,
                         "at": created.isoformat() if created else None}
    return out
