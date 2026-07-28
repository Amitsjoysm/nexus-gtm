# tests/test_billing_rollups.py
from __future__ import annotations

from datetime import datetime, timezone


def test_period_keys_are_stable_and_sortable():
    from nexus.billing.rollups import period_key

    ts = datetime(2026, 7, 28, 14, 37, 12, tzinfo=timezone.utc)
    assert period_key(ts, "hour") == "2026-07-28T14"
    assert period_key(ts, "day") == "2026-07-28"
    assert period_key(ts, "period") == "2026-07"
    # Lexical sort == chronological sort (so range scans work without parsing).
    earlier = period_key(datetime(2026, 7, 28, 9, 0, tzinfo=timezone.utc), "hour")
    assert earlier < period_key(ts, "hour")


def test_period_key_normalises_naive_datetimes_to_utc():
    """SQLite returns naive datetimes; a naive value must not shift the bucket."""
    from nexus.billing.rollups import period_key

    naive = datetime(2026, 7, 28, 14, 37, 12)
    assert period_key(naive, "hour") == "2026-07-28T14"


from tests.conftest import make_tenant, tenant_session


async def _event(ts, cap: str, qty: float, when):
    from nexus.models.billing import BillingUsageEvent

    ts.add(
        BillingUsageEvent(
            capability_id=cap, quantity=qty, unit="action", source="api",
            idempotency_key=f"{cap}:{qty}:{when.isoformat()}", occurred_at=when,
        )
    )
    await ts.flush()


async def test_rebuild_rollups_aggregates_all_grains():
    from datetime import datetime, timezone

    from nexus.billing.rollups import rebuild_rollups
    from nexus.models.billing import BillingUsageRollup

    t0 = datetime(2026, 7, 28, 14, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 7, 28, 15, 0, tzinfo=timezone.utc)
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        await _event(ts, "ai.email_draft", 2, t0)
        await _event(ts, "ai.email_draft", 3, t1)

        res = await rebuild_rollups(ts)
        assert res["events"] == 2

        rows = await ts.list(BillingUsageRollup)
        by = {(r.period_kind, r.period_key): r for r in rows}
        assert float(by[("hour", "2026-07-28T14")].quantity) == 2
        assert float(by[("hour", "2026-07-28T15")].quantity) == 3
        assert float(by[("day", "2026-07-28")].quantity) == 5
        assert float(by[("period", "2026-07")].quantity) == 5
        assert by[("period", "2026-07")].event_count == 2


async def test_rebuild_rollups_is_idempotent():
    """Re-running must not double-count — rollups are upserted, not appended."""
    from datetime import datetime, timezone

    from nexus.billing.rollups import rebuild_rollups
    from nexus.models.billing import BillingUsageRollup

    when = datetime(2026, 7, 28, 14, 0, tzinfo=timezone.utc)
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        await _event(ts, "verify.email", 10, when)
        await rebuild_rollups(ts)
        await rebuild_rollups(ts)
        rows = [r for r in await ts.list(BillingUsageRollup) if r.period_kind == "period"]
        assert len(rows) == 1
        assert float(rows[0].quantity) == 10        # not 20


async def test_rebuild_rollups_sums_cost():
    from datetime import datetime, timezone

    from nexus.billing.rollups import rebuild_rollups
    from nexus.models.billing import BillingUsageEvent, BillingUsageRollup

    when = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        ts.add(BillingUsageEvent(
            capability_id="ai.tokens", quantity=1000, unit="token", source="api",
            idempotency_key="k1", occurred_at=when, unit_cost_usd=0.0000012,
        ))
        await ts.flush()
        await rebuild_rollups(ts)
        row = next(r for r in await ts.list(BillingUsageRollup) if r.period_kind == "period")
        assert float(row.cost_usd) > 0


async def test_current_usage_prefers_rollup_and_adds_unrolled_events():
    """Authoritative counter = rolled-up total + any events not yet folded in. Never double
    counts, never misses recent usage."""
    from datetime import timezone

    from nexus.billing.entitlements import current_usage
    from nexus.billing.rollups import rebuild_rollups
    from nexus.core.db import utcnow

    now = utcnow().astimezone(timezone.utc)
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        await _event(ts, "verify.email", 4, now)
        await rebuild_rollups(ts)               # 4 is now in the period rollup
        assert await current_usage(ts, "verify.email") == 4

        await _event(ts, "verify.email", 3, now)   # not yet rolled up
        assert await current_usage(ts, "verify.email") == 7

        await rebuild_rollups(ts)                  # folding it in must not double-count
        assert await current_usage(ts, "verify.email") == 7


async def test_current_usage_counts_an_event_that_ties_the_rollup_timestamp():
    """Regression: a quota read must not depend on comparing two Python-stamped clocks.

    Windows ticks at ~15ms, so a genuinely-new event routinely lands on the same timestamp as
    the rollup written just before it. A watermark comparison drops that event from the count,
    which under enforcement hands the tenant free quota. The marker cannot tie.
    """
    from nexus.billing.entitlements import current_usage
    from nexus.billing.rollups import rebuild_rollups
    from nexus.core.db import utcnow
    from nexus.models.billing import BillingUsageEvent, BillingUsageRollup

    now = utcnow()
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        await _event(ts, "verify.email", 4, now)
        await rebuild_rollups(ts)
        rollup = await ts.first(
            BillingUsageRollup,
            BillingUsageRollup.capability_id == "verify.email",
            BillingUsageRollup.period_kind == "period",
        )

        await _event(ts, "verify.email", 3, now)
        # Force the exact tie a coarse clock produces in the wild.
        fresh = [e for e in await ts.list(BillingUsageEvent) if float(e.quantity) == 3][0]
        fresh.created_at = rollup.updated_at
        await ts.flush()

        assert await current_usage(ts, "verify.email") == 7


async def test_current_usage_is_exact_when_the_rollup_worker_never_runs():
    """Liveness safety: with no rollup at all, every event is unrolled and summed live.

    The read degrades to slower, never to undercounting — the only safe direction for a limit.
    """
    from nexus.billing.entitlements import current_usage
    from nexus.core.db import utcnow

    now = utcnow()
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        await _event(ts, "verify.email", 5, now)
        await _event(ts, "verify.email", 6, now)
        assert await current_usage(ts, "verify.email") == 11


async def test_partial_window_rebuild_does_not_truncate_the_period_rollup():
    """``since`` is snapped down to the period start.

    Buckets are recomputed by assignment, so a window covering only part of a bucket would
    erase the rest. Without the snap, a caller passing "the last hour" would silently truncate
    the whole month's period rollup to that hour.
    """
    from nexus.billing.rollups import rebuild_rollups
    from nexus.models.billing import BillingUsageRollup

    early = datetime(2026, 7, 2, 9, 0, tzinfo=timezone.utc)
    late = datetime(2026, 7, 28, 9, 0, tzinfo=timezone.utc)
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        await _event(ts, "verify.email", 10, early)
        await _event(ts, "verify.email", 5, late)
        await rebuild_rollups(ts)
        await rebuild_rollups(ts, since=late)       # deliberately narrow window

        row = await ts.first(
            BillingUsageRollup,
            BillingUsageRollup.capability_id == "verify.email",
            BillingUsageRollup.period_kind == "period",
            BillingUsageRollup.period_key == "2026-07",
        )
        assert float(row.quantity) == 15


async def test_rollup_sweep_is_scoped_to_the_current_period():
    """The heartbeat sweep must not re-read a tenant's whole event history every tick."""
    from nexus.workers.tasks import handle_rollup_usage

    old = datetime(2024, 1, 5, 12, 0, tzinfo=timezone.utc)
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        await _event(ts, "verify.email", 9, old)

    # Only a closed-period event exists, so the sweep has nothing to do this tick.
    assert (await handle_rollup_usage({}))["tenants"] == 0
