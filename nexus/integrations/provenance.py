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
