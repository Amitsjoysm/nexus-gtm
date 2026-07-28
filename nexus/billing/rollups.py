# nexus/billing/rollups.py
"""Usage rollups: fold the append-only event stream into queryable aggregates.

Rollups are DERIVED state — they can be rebuilt from ``billing_usage_events`` at any time, which
is what makes the reconciliation job safe. Three grains are kept:

  hour   -> ops dashboards, anomaly detection
  day    -> admin usage explorer, cost reports
  period -> the billing month; this is the grain quota checks read

Period keys are lexically sortable so range scans need no date parsing
(docs/billing/03-Metering-Architecture.md §2).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger("nexus.billing.rollups")

PERIOD_KINDS = ("hour", "day", "period")


def period_key(when: datetime, kind: str) -> str:
    """Bucket a timestamp into a stable, lexically sortable key."""
    if when.tzinfo is None:  # SQLite hands back naive values; treat them as UTC
        when = when.replace(tzinfo=timezone.utc)
    when = when.astimezone(timezone.utc)
    if kind == "hour":
        return when.strftime("%Y-%m-%dT%H")
    if kind == "day":
        return when.strftime("%Y-%m-%d")
    if kind == "period":
        return when.strftime("%Y-%m")
    raise ValueError(f"unknown period kind: {kind}")
