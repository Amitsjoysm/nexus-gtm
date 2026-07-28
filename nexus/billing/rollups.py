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


async def rebuild_rollups(ts, *, since: datetime | None = None) -> dict:
    """Fold this tenant's usage events into rollups. Idempotent (upsert by natural key).

    Safe to re-run over any window: each (capability, grain, key) bucket is recomputed from the
    events themselves rather than incremented, so a retry or overlapping window can never
    double-count. Returns ``{"events": n, "buckets": m}``.
    """
    from nexus.models.billing import BillingUsageEvent, BillingUsageRollup

    stmt = ts.select(BillingUsageEvent)
    if since is not None:
        stmt = stmt.where(BillingUsageEvent.occurred_at >= since)
    events = list((await ts.session.scalars(stmt)).all())
    if not events:
        return {"events": 0, "buckets": 0}

    # (capability, kind, key) -> [quantity, count, cost]
    agg: dict[tuple[str, str, str], list[float]] = {}
    for ev in events:
        for kind in PERIOD_KINDS:
            k = (ev.capability_id, kind, period_key(ev.occurred_at, kind))
            slot = agg.setdefault(k, [0.0, 0.0, 0.0])
            slot[0] += float(ev.quantity or 0)
            slot[1] += 1
            slot[2] += float(ev.unit_cost_usd or 0) * float(ev.quantity or 0)

    existing = {
        (r.capability_id, r.period_kind, r.period_key): r
        for r in (await ts.session.scalars(ts.select(BillingUsageRollup))).all()
    }
    for (cap, kind, key), (qty, cnt, cost) in agg.items():
        row = existing.get((cap, kind, key))
        if row is None:
            ts.add(
                BillingUsageRollup(
                    capability_id=cap, period_kind=kind, period_key=key,
                    quantity=qty, event_count=int(cnt), cost_usd=cost,
                )
            )
        else:
            row.quantity = qty
            row.event_count = int(cnt)
            row.cost_usd = cost
    await ts.flush()
    return {"events": len(events), "buckets": len(agg)}
