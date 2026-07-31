# nexus/billing/retention.py
"""Usage-event retention: keep the hot path fast without losing the audit trail.

``billing_usage_events`` is append-only and grows without bound. It is also read on the quota hot
path — ``current_usage`` sums the period rollup plus the events not yet folded into it — so its size
is a latency problem, not just a storage one.

**Why this is not a partitioning migration.** The plan asked for monthly partitions. Postgres cannot
`ALTER` an existing table into a partitioned one: it requires creating a partitioned table, copying
every row, and swapping under a lock. That is a maintenance window on the table that records what
customers are billed for, and it is emphatically not the "additive only" migration this project
requires. `scripts/partition_usage_events.sql` documents that path for whoever schedules the
window; this module does the part that is safe to run continuously.

**What is safe: pruning events the rollups have already absorbed.** A rolled-up event has served its
purpose — the rollup is the billing record, and the event is the working paper behind it. Two
guarantees make deletion safe rather than destructive:

* Only events with a ``rolled_at`` marker are eligible. An unrolled event is still uncounted usage,
  and deleting one silently reduces a customer's bill.
* Only events older than the retention window, which must exceed any period still open for dispute.
  Money that can still be argued about needs its working papers.

The default is deliberately long. Storage is cheap; being unable to answer "why was I charged for
this?" is not.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("nexus.billing.retention")

# Twelve months. Longer than any billing period, longer than a typical dispute window, and long
# enough that a customer querying last year's invoice still gets a line-by-line answer.
DEFAULT_RETENTION_DAYS = 365


async def prune_rolled_usage(
    *, retention_days: int = DEFAULT_RETENTION_DAYS, limit: int = 5000, dry_run: bool = False
) -> dict:
    """Delete rolled-up usage events older than the retention window.

    Bounded by ``limit`` per call: an unbounded DELETE on the largest table in the schema takes a
    lock for as long as it takes, and the sweep runs on a schedule anyway. Repeated calls drain the
    backlog without ever holding a long transaction.

    Returns a report rather than raising: this runs on the worker, and a retention failure must not
    take down the queue.
    """
    from datetime import timedelta

    from sqlalchemy import delete, func, select

    from nexus.core.db import get_platform_sessionmaker, utcnow
    from nexus.models.billing import BillingUsageEvent

    # `occurred_at`, not `created_at`: the former is when the billable action happened (the
    # billing fact, and the column the hot-path indexes cover), the latter is merely when the row
    # was inserted. Backfilled rows would otherwise be judged by their insert time.
    cutoff = utcnow() - timedelta(days=max(1, retention_days))
    report = {"cutoff": cutoff.isoformat(), "eligible": 0, "deleted": 0, "dry_run": dry_run}
    try:
        # Cross-tenant maintenance, so the owner role: under the RLS-bound role this would match
        # zero rows and silently report a clean sweep on a table that never shrank.
        async with get_platform_sessionmaker()() as session:
            eligible = await session.scalar(
                select(func.count())
                .select_from(BillingUsageEvent)
                .where(
                    BillingUsageEvent.rolled_at.is_not(None),
                    BillingUsageEvent.occurred_at < cutoff,
                )
            )
            report["eligible"] = int(eligible or 0)
            if dry_run or not report["eligible"]:
                return report

            ids = (
                await session.scalars(
                    select(BillingUsageEvent.id)
                    .where(
                        BillingUsageEvent.rolled_at.is_not(None),
                        BillingUsageEvent.occurred_at < cutoff,
                    )
                    .limit(limit)
                )
            ).all()
            if ids:
                await session.execute(
                    delete(BillingUsageEvent).where(BillingUsageEvent.id.in_(list(ids)))
                )
                await session.commit()
                report["deleted"] = len(ids)
                logger.info(
                    "pruned %s usage events older than %s (%s eligible)",
                    len(ids), cutoff.date(), report["eligible"],
                )
    except Exception:
        logger.warning("usage retention sweep failed", exc_info=True)
        report["error"] = "sweep failed; see logs"
    return report
