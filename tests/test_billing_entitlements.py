# tests/test_billing_entitlements.py
from __future__ import annotations

from tests.conftest import make_tenant, tenant_session


async def _seed(plan_id: str = "growth"):
    """Catalog + plans + a subscription on `plan_id`. Returns the tenant id."""
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.models.billing import BillingSubscription

    await sync_catalog()
    await sync_plans()
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        ts.add(BillingSubscription(plan_id=plan_id, status="active"))
        await ts.flush()
    return tid


async def test_resolve_uses_plan_entitlement_when_present():
    from nexus.billing.entitlements import resolve_entitlement

    tid = await _seed("free")
    async with tenant_session(tid) as ts:
        ent = await resolve_entitlement(ts, "ai.email_draft")
        assert ent.mode == "metered"
        assert ent.quota == 20          # from the Free plan seed
        assert ent.source == "plan"


async def test_resolve_falls_back_to_catalog_default():
    """A capability the plan says nothing about falls back to the catalog's safe default."""
    from nexus.billing.entitlements import resolve_entitlement

    tid = await _seed("growth")
    async with tenant_session(tid) as ts:
        ent = await resolve_entitlement(ts, "ai.research_brief")
        assert ent.mode == "metered"    # catalog default_mode
        assert ent.quota is None        # unlimited unless a plan says otherwise
        assert ent.source == "catalog"


async def test_unknown_capability_resolves_to_allow():
    """Shadow-safety: an unregistered capability must never block a feature."""
    from nexus.billing.entitlements import resolve_entitlement

    tid = await _seed()
    async with tenant_session(tid) as ts:
        ent = await resolve_entitlement(ts, "totally.unknown")
        assert ent.mode == "shadow"
        assert ent.source == "unknown"


async def test_no_subscription_resolves_to_catalog_default():
    """A tenant with no subscription row (pre-billing) is never blocked."""
    from nexus.billing.entitlements import resolve_entitlement
    from nexus.billing.catalog import sync_catalog

    await sync_catalog()
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        ent = await resolve_entitlement(ts, "ai.email_draft")
        assert ent.source in ("catalog", "unknown")
        assert ent.mode != "disabled"


async def test_unlimited_plan_class_overrides_everything():
    """legacy-unlimited tenants keep every capability regardless of plan entitlement rows."""
    from nexus.billing.entitlements import resolve_entitlement

    tid = await _seed("legacy-unlimited")
    async with tenant_session(tid) as ts:
        ent = await resolve_entitlement(ts, "ai.email_draft")
        assert ent.mode == "unlimited"
        assert ent.quota is None
        assert ent.source == "plan_class"
