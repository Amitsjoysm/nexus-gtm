# tests/test_billing_usage.py
from __future__ import annotations

from tests.conftest import make_tenant, tenant_session


def test_usage_models_registered():
    import nexus.models as m

    assert hasattr(m, "BillingUsageEvent")
    assert hasattr(m, "BillingUsageRollup")


async def test_usage_event_round_trip_is_tenant_scoped():
    from nexus.core.db import utcnow
    from nexus.models.billing import BillingUsageEvent

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        ts.add(
            BillingUsageEvent(
                capability_id="ai.email_draft", quantity=1, unit="action",
                source="api", idempotency_key="req-1", occurred_at=utcnow(),
                attrs={"tokens_in": 1500},
            )
        )
        await ts.flush()
        rows = await ts.list(BillingUsageEvent)
        assert len(rows) == 1
        assert rows[0].tenant_id == tid
        assert rows[0].attrs["tokens_in"] == 1500
        assert rows[0].quantity == 1


async def test_record_usage_is_idempotent():
    """The same idempotency key must never produce a second billable row — queue retries and
    duplicated webhooks are routine in production."""
    from nexus.billing.usage import record_usage
    from nexus.models.billing import BillingUsageEvent

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        first = await record_usage(
            ts, capability_id="ai.email_draft", quantity=1,
            idempotency_key="run-7:step-2", unit="action",
        )
        second = await record_usage(
            ts, capability_id="ai.email_draft", quantity=1,
            idempotency_key="run-7:step-2", unit="action",
        )
        assert first is True and second is False        # second was a no-op
        assert len(await ts.list(BillingUsageEvent)) == 1


async def test_record_usage_without_key_autogenerates():
    from nexus.billing.usage import record_usage
    from nexus.models.billing import BillingUsageEvent

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        await record_usage(ts, capability_id="api.request", quantity=1)
        await record_usage(ts, capability_id="api.request", quantity=1)
        # No key supplied -> each call is a distinct event (blanket meters are high-volume).
        assert len(await ts.list(BillingUsageEvent)) == 2


async def test_record_usage_never_raises_on_bad_input():
    """Metering must never take the product down (docs/billing/01 §6)."""
    from nexus.billing.usage import record_usage

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        assert await record_usage(ts, capability_id="", quantity=1) is False
