# tests/test_billing_reconcile.py
"""Reconciliation reports drift; it never repairs it.

Webhooks keep our state and the provider's in step, but a delivery can fail past its retry
budget or an endpoint can be misconfigured for a window. Those gaps are otherwise invisible
until a customer complains about being billed for a plan they cancelled.
"""
from __future__ import annotations

from tests.conftest import make_tenant, tenant_session


async def _tenant_with_remote_sub(slug: str, *, local_status="active", plan_id="growth"):
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.models.billing import BillingSubscription

    await sync_catalog()
    await sync_plans()
    tid = await make_tenant(slug=slug, name=slug)
    async with tenant_session(tid) as ts:
        ts.add(BillingSubscription(
            plan_id=plan_id, status=local_status,
            psp_customer_id="cus_x", psp_subscription_id=f"sub_{slug}",
        ))
        await ts.flush()
    return tid


async def test_agreement_reports_no_drift():
    from nexus.billing.payments import NoopPaymentProvider, set_payment_provider
    from nexus.billing.reconcile import reconcile_tenant

    provider = NoopPaymentProvider()
    provider.subscriptions["sub_rec1"] = {"status": "active", "metadata": {"plan_id": "growth"}}
    set_payment_provider(provider)
    try:
        tid = await _tenant_with_remote_sub("rec1")
        async with tenant_session(tid) as ts:
            res = await reconcile_tenant(ts)
        assert res["checked"] == 1
        assert res["drifted"] == 0
    finally:
        set_payment_provider(None)


async def test_status_disagreement_is_reported():
    """The case that matters: they cancelled in the portal, we still think they are active."""
    from nexus.billing.payments import NoopPaymentProvider, set_payment_provider
    from nexus.billing.reconcile import reconcile_tenant

    provider = NoopPaymentProvider()
    provider.subscriptions["sub_rec2"] = {"status": "canceled", "metadata": {"plan_id": "growth"}}
    set_payment_provider(provider)
    try:
        tid = await _tenant_with_remote_sub("rec2", local_status="active")
        async with tenant_session(tid) as ts:
            res = await reconcile_tenant(ts)
        assert res["drifted"] == 1
        drift = res["findings"][0]["drift"]["status"]
        assert drift["local"] == "active"
        assert drift["remote_mapped"] == "canceled"
    finally:
        set_payment_provider(None)


async def test_reconciliation_does_not_repair():
    """Deliberate: which side is right depends on what the customer agreed to, and an automated
    writer would resolve that confidently, wrongly, and over the top of the evidence."""
    from nexus.billing.payments import NoopPaymentProvider, set_payment_provider
    from nexus.billing.reconcile import reconcile_tenant
    from nexus.models.billing import BillingSubscription

    provider = NoopPaymentProvider()
    provider.subscriptions["sub_rec3"] = {"status": "canceled", "metadata": {}}
    set_payment_provider(provider)
    try:
        tid = await _tenant_with_remote_sub("rec3", local_status="active")
        async with tenant_session(tid) as ts:
            await reconcile_tenant(ts)
            sub = await ts.first(BillingSubscription)
            assert sub.status == "active"      # untouched
    finally:
        set_payment_provider(None)


async def test_plan_disagreement_is_reported():
    from nexus.billing.payments import NoopPaymentProvider, set_payment_provider
    from nexus.billing.reconcile import reconcile_tenant

    provider = NoopPaymentProvider()
    provider.subscriptions["sub_rec4"] = {
        "status": "active", "metadata": {"plan_id": "professional"},
    }
    set_payment_provider(provider)
    try:
        tid = await _tenant_with_remote_sub("rec4", plan_id="growth")
        async with tenant_session(tid) as ts:
            res = await reconcile_tenant(ts)
        assert res["findings"][0]["drift"]["plan_id"] == {
            "local": "growth", "remote": "professional",
        }
    finally:
        set_payment_provider(None)


async def test_missing_remote_subscription_is_itself_a_finding():
    from nexus.billing.payments import NoopPaymentProvider, set_payment_provider
    from nexus.billing.reconcile import reconcile_tenant

    set_payment_provider(NoopPaymentProvider())      # nothing staged -> unknown
    try:
        tid = await _tenant_with_remote_sub("rec5")
        async with tenant_session(tid) as ts:
            res = await reconcile_tenant(ts)
        assert res["drifted"] == 1
        assert res["findings"][0]["drift"]["remote"] == "missing"
    finally:
        set_payment_provider(None)


async def test_enterprise_subscriptions_are_skipped_not_flagged():
    """Admin-administered deals never had a provider object. Flagging them would bury real
    findings in noise."""
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.payments import NoopPaymentProvider, set_payment_provider
    from nexus.billing.plans import sync_plans
    from nexus.billing.reconcile import reconcile_tenant
    from nexus.models.billing import BillingSubscription

    set_payment_provider(NoopPaymentProvider())
    try:
        await sync_catalog()
        await sync_plans()
        tid = await make_tenant(slug="rec6", name="rec6")
        async with tenant_session(tid) as ts:
            ts.add(BillingSubscription(plan_id="growth", status="active"))  # no psp id
            await ts.flush()
            res = await reconcile_tenant(ts)
        assert res["checked"] == 0
        assert res["skipped"] == 1
        assert res["drifted"] == 0
    finally:
        set_payment_provider(None)


async def test_an_unmapped_remote_status_is_not_drift():
    """Webhooks deliberately leave `incomplete` alone, so reporting it would flag our own
    intentional behaviour as a defect."""
    from nexus.billing.payments import NoopPaymentProvider, set_payment_provider
    from nexus.billing.reconcile import reconcile_tenant

    provider = NoopPaymentProvider()
    provider.subscriptions["sub_rec7"] = {"status": "incomplete", "metadata": {}}
    set_payment_provider(provider)
    try:
        tid = await _tenant_with_remote_sub("rec7", local_status="active")
        async with tenant_session(tid) as ts:
            res = await reconcile_tenant(ts)
        assert res["drifted"] == 0
    finally:
        set_payment_provider(None)


async def test_the_sweep_handler_runs_over_every_tenant():
    from nexus.billing.payments import NoopPaymentProvider, set_payment_provider
    from nexus.workers.tasks import handle_billing_reconcile

    provider = NoopPaymentProvider()
    provider.subscriptions["sub_rec8"] = {"status": "active", "metadata": {"plan_id": "growth"}}
    set_payment_provider(provider)
    try:
        await _tenant_with_remote_sub("rec8")
        res = await handle_billing_reconcile({})
        assert res["tenants"] >= 1
        assert res["checked"] >= 1
    finally:
        set_payment_provider(None)
