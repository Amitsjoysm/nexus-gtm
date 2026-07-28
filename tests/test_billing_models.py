# tests/test_billing_models.py
from __future__ import annotations

from tests.conftest import make_tenant, tenant_session


def test_models_importable_and_registered():
    import nexus.models as m

    for name in (
        "BillingCapability", "BillingPlan", "BillingPlanEntitlement",
        "BillingSubscription", "PlatformAdmin",
    ):
        assert hasattr(m, name), f"{name} not exported from nexus.models"


async def test_capability_and_plan_round_trip():
    """Platform-global config tables are NOT tenant-scoped: they carry no tenant_id and are
    readable by every tenant (the catalog is the same for the whole platform)."""
    from nexus.core.db import get_sessionmaker
    from nexus.models.billing import BillingCapability, BillingPlan, BillingPlanEntitlement

    async with get_sessionmaker()() as s:
        cap = BillingCapability(
            id="ai.email_draft", category="ai", sub_category="outreach",
            name="AI email draft", description="Personalized outreach draft",
            unit="action", meter_kind="counter", default_mode="metered",
        )
        plan = BillingPlan(
            id="growth", name="Growth", plan_class="standard", status="active",
            base_price_cents=7900, currency="USD", interval="month",
            included_credits=2000, seat_price_cents=0,
        )
        s.add_all([cap, plan])
        await s.flush()
        s.add(BillingPlanEntitlement(
            plan_id="growth", capability_id="ai.email_draft", mode="metered",
            quota=500, soft_limit_pct=80, reset_policy="monthly_anniversary",
            overage_price_credits=2,
        ))
        await s.commit()

    async with get_sessionmaker()() as s:
        got = await s.get(BillingCapability, "ai.email_draft")
        assert got.unit == "action" and got.default_mode == "metered"
        p = await s.get(BillingPlan, "growth")
        assert p.included_credits == 2000 and p.plan_class == "standard"


async def test_subscription_is_tenant_scoped():
    """billing_subscriptions carries tenant_id -> automatically covered by apply_rls.py."""
    from nexus.core.db import get_sessionmaker
    from nexus.models.billing import BillingPlan, BillingSubscription

    tid = await make_tenant()
    async with get_sessionmaker()() as s:
        s.add(BillingPlan(id="legacy-unlimited", name="Legacy Unlimited",
                          plan_class="unlimited", status="active", base_price_cents=0,
                          currency="USD", interval="month", included_credits=0,
                          seat_price_cents=0))
        await s.commit()

    async with tenant_session(tid) as ts:
        ts.add(BillingSubscription(plan_id="legacy-unlimited", status="active"))
        await ts.flush()
        rows = await ts.list(BillingSubscription)
        assert len(rows) == 1
        assert rows[0].tenant_id == tid          # stamped by the tenancy layer
        assert rows[0].status == "active"
