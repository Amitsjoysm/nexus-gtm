"""Usage-event retention (M26).

`billing_usage_events` is append-only, unbounded, and read on the quota hot path — `current_usage`
sums the period rollup plus every event not yet folded into it — so its size is a latency problem,
not only a storage one.

**Deletion here is safe only because of two guarantees**, and both are tested below:

* an event without a `rolled_at` marker is still *uncounted usage*, and deleting one silently
  reduces a customer's bill;
* an event inside the retention window is still working paper for money that can be disputed.

True monthly partitioning is documented in `scripts/partition_usage_events.sql` rather than
implemented as a migration: Postgres cannot ALTER a table into a partitioned one, so it needs a copy
-and-swap under lock — a maintenance window on the table recording what customers are billed for,
and not the "additive only" migration this project requires.
"""
from __future__ import annotations

from datetime import timedelta

from nexus.billing.retention import DEFAULT_RETENTION_DAYS, prune_rolled_usage
from tests.conftest import make_tenant, tenant_session


async def _event(tid: str, *, days_old: int, rolled: bool):
    from nexus.core.db import utcnow
    from nexus.models.billing import BillingUsageEvent

    when = utcnow() - timedelta(days=days_old)
    async with tenant_session(tid) as ts:
        ev = BillingUsageEvent(
            tenant_id=tid, capability_id="ai.email_draft", quantity=1,
            idempotency_key=f"k-{days_old}-{rolled}-{utcnow().timestamp()}",
            rolled_at=when if rolled else None,
            occurred_at=when,
        )
        ts.add(ev)
        await ts.flush()
        return ev.id


async def _count(tid: str) -> int:
    from nexus.models.billing import BillingUsageEvent

    async with tenant_session(tid) as ts:
        return len(await ts.list(BillingUsageEvent, limit=1000))


def test_the_default_window_outlasts_any_dispute():
    """Storage is cheap; being unable to answer "why was I charged for this?" is not."""
    assert DEFAULT_RETENTION_DAYS >= 365


async def test_an_unrolled_event_is_never_deleted():
    """The load-bearing guarantee. An unrolled event is uncounted usage — deleting one silently
    reduces the customer's bill."""
    tid = await make_tenant(slug="ret1")
    await _event(tid, days_old=800, rolled=False)

    report = await prune_rolled_usage(retention_days=365)
    assert report.get("error") is None
    assert await _count(tid) == 1


async def test_a_recent_rolled_event_is_kept():
    """Inside the window it is still working paper for money that can be disputed."""
    tid = await make_tenant(slug="ret2")
    await _event(tid, days_old=10, rolled=True)

    await prune_rolled_usage(retention_days=365)
    assert await _count(tid) == 1


async def test_an_old_rolled_event_is_pruned():
    tid = await make_tenant(slug="ret3")
    await _event(tid, days_old=800, rolled=True)

    report = await prune_rolled_usage(retention_days=365)
    assert report["deleted"] >= 1
    assert await _count(tid) == 0


async def test_a_dry_run_reports_without_deleting():
    """An operator should be able to see the blast radius before authorising it."""
    tid = await make_tenant(slug="ret4")
    await _event(tid, days_old=800, rolled=True)

    report = await prune_rolled_usage(retention_days=365, dry_run=True)
    assert report["eligible"] >= 1
    assert report["deleted"] == 0
    assert await _count(tid) == 1


async def test_the_sweep_is_bounded_per_call():
    """An unbounded DELETE on the largest table in the schema holds a lock for as long as it takes.
    Repeated bounded calls drain the backlog without a long transaction."""
    tid = await make_tenant(slug="ret5")
    for _ in range(3):
        await _event(tid, days_old=800, rolled=True)

    report = await prune_rolled_usage(retention_days=365, limit=2)
    assert report["deleted"] == 2
    assert await _count(tid) == 1


async def test_the_sweep_never_raises(monkeypatch):
    """It runs on the worker; a retention failure must not take down the queue."""
    import nexus.core.db as core_db

    def boom():
        raise RuntimeError("db unreachable")

    # Patched where it is DEFINED, not where it is used: retention.py imports it inside the
    # function, so patching the retention module would have no effect and the test would pass
    # while proving nothing.
    monkeypatch.setattr(core_db, "get_platform_sessionmaker", boom)
    report = await prune_rolled_usage()
    assert "error" in report


def test_the_partitioning_path_is_documented_not_migrated():
    """Converting the billing table in an Alembic revision would need a copy-and-swap under lock —
    not additive, and not replayable onto an empty database."""
    from pathlib import Path

    script = Path("scripts/partition_usage_events.sql")
    assert script.exists()
    text = script.read_text(encoding="utf-8")
    # The two things most likely to be forgotten during the window.
    assert "apply_rls" in text          # RLS is not inherited by the rename
    assert "count(*)" in text           # verify before dropping the original
