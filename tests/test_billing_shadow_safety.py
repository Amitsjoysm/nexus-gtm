# tests/test_billing_shadow_safety.py
"""The non-negotiable safety contract of the billing platform.

If any of these fail, the platform is capable of breaking a customer feature and must not ship.
"""
from __future__ import annotations

from tests.conftest import make_tenant, tenant_session


async def test_unknown_capability_always_allowed_even_when_enforcing(monkeypatch):
    from nexus.billing.entitlements import check_and_meter
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "billing_enforcement", "on")
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        res = await check_and_meter(ts, capability_id="feature.nobody.registered")
        assert res.allowed is True


async def test_tenant_without_subscription_is_never_blocked(monkeypatch):
    """Every pre-billing tenant has no subscription row. They must keep working."""
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.entitlements import check_and_meter
    from nexus.core.config import get_settings

    await sync_catalog()
    monkeypatch.setattr(get_settings(), "billing_enforcement", "on")
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        for cap in ("ai.email_draft", "network.search", "outreach.email_send",
                    "discovery.account_added", "verify.email"):
            assert (await check_and_meter(ts, capability_id=cap)).allowed is True, cap


async def test_engine_failure_degrades_to_allow(monkeypatch):
    """A broken entitlement engine must not take the product down."""
    import nexus.billing.entitlements as ent_mod
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "billing_enforcement", "on")

    async def boom(*a, **k):
        raise RuntimeError("engine exploded")

    monkeypatch.setattr(ent_mod, "resolve_entitlement", boom)
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        res = await ent_mod.check_and_meter(ts, capability_id="ai.email_draft")
        assert res.allowed is True
        assert res.recorded is False


async def test_default_enforcement_setting_is_shadow():
    """Ship dark: the default must never enforce."""
    from nexus.core.config import Settings

    assert Settings().billing_enforcement == "shadow"
