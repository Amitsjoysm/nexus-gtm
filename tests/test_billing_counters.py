# tests/test_billing_counters.py
from __future__ import annotations

from tests.conftest import make_tenant, tenant_session


async def test_rollup_job_processes_all_tenants():
    from datetime import timezone

    from nexus.core.db import utcnow
    from nexus.models.billing import BillingUsageEvent, BillingUsageRollup
    from nexus.workers.tasks import handle_rollup_usage

    now = utcnow().astimezone(timezone.utc)
    tids = [await make_tenant(slug=f"r{i}") for i in range(2)]
    for tid in tids:
        async with tenant_session(tid) as ts:
            ts.add(BillingUsageEvent(
                capability_id="ai.email_draft", quantity=2, unit="action", source="api",
                idempotency_key=f"k-{tid}", occurred_at=now,
            ))
            await ts.flush()

    res = await handle_rollup_usage({})
    assert res["tenants"] >= 2

    for tid in tids:
        async with tenant_session(tid) as ts:
            rows = [r for r in await ts.list(BillingUsageRollup) if r.period_kind == "period"]
            assert len(rows) == 1 and float(rows[0].quantity) == 2


async def test_rollup_job_is_registered():
    from nexus.workers.tasks import HANDLERS

    assert "rollup_usage" in HANDLERS


async def test_rollup_job_is_safe_to_run_twice():
    from datetime import timezone

    from nexus.core.db import utcnow
    from nexus.models.billing import BillingUsageEvent, BillingUsageRollup
    from nexus.workers.tasks import handle_rollup_usage

    now = utcnow().astimezone(timezone.utc)
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        ts.add(BillingUsageEvent(
            capability_id="verify.email", quantity=5, unit="check", source="worker",
            idempotency_key="dupe-check", occurred_at=now,
        ))
        await ts.flush()

    await handle_rollup_usage({})
    await handle_rollup_usage({})
    async with tenant_session(tid) as ts:
        row = next(r for r in await ts.list(BillingUsageRollup) if r.period_kind == "period")
        assert float(row.quantity) == 5      # not 10
