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
