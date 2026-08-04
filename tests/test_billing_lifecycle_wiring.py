# tests/test_billing_lifecycle_wiring.py
"""M22, actually connected: proration, trial expiry and pause/resume reaching real money.

``nexus/billing/lifecycle.py`` shipped with tested arithmetic and **no callers**, which is the same
dead-config failure the billing engine exists to prevent — the calculator was right and every
customer was still billed a full month for two days of service. These tests assert the wiring, not
the arithmetic: that a plan change writes adjustments, that an invoice carries them, that a trial
ends, and that a pause both stops the clock and suspends entitlements.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tests.conftest import auth, make_tenant, signup, tenant_session


async def _seed():
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans

    await sync_catalog()
    await sync_plans()


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _subscribe(ts, plan_id: str, *, days_in: int = 10, period_days: int = 30):
    """A subscription mid-period: started ``days_in`` days ago, ``period_days`` long."""
    from nexus.billing.subscriptions import ensure_subscription

    sub = await ensure_subscription(ts, plan_id=plan_id)
    start = _now() - timedelta(days=days_in)
    sub.current_period_start = start
    sub.current_period_end = start + timedelta(days=period_days)
    await ts.flush()
    return sub


# ---- proration reaches the invoice ---------------------------------------------------------------

async def test_a_mid_cycle_plan_change_records_both_proration_lines():
    """Credit for what was left on the old plan, charge for the rest of the new one.

    Both are recorded even when one is zero: an invoice showing only the charge looks like the
    customer was billed twice for one month.
    """
    from nexus.billing.subscriptions import change_plan
    from nexus.models.billing import BillingProrationAdjustment

    await _seed()
    tid = await make_tenant(slug="pr1", name="PR One")
    async with tenant_session(tid) as ts:
        await _subscribe(ts, "starter")
        await change_plan(ts, "growth", actor="admin@nexus.com")
        rows = await ts.list(BillingProrationAdjustment)

    kinds = sorted(r.kind for r in rows)
    assert kinds == ["proration_charge", "proration_credit"], rows
    credit = next(r for r in rows if r.kind == "proration_credit")
    charge = next(r for r in rows if r.kind == "proration_charge")
    # Upgrading: the charge for the richer plan's remaining days exceeds the credit for the old.
    assert charge.amount_cents > 0
    assert credit.amount_cents < 0, "a credit must be signed negative so summing lines is correct"
    assert charge.from_plan_id == "starter" and charge.to_plan_id == "growth"


async def test_changing_to_the_same_plan_prorates_nothing():
    """A no-op write must not manufacture two invoice lines that cancel out and confuse a reader."""
    from nexus.billing.subscriptions import change_plan
    from nexus.models.billing import BillingProrationAdjustment

    await _seed()
    tid = await make_tenant(slug="pr2", name="PR Two")
    async with tenant_session(tid) as ts:
        await _subscribe(ts, "growth")
        await change_plan(ts, "growth", actor="admin@nexus.com")
        assert await ts.list(BillingProrationAdjustment) == []


async def test_a_change_with_no_billing_period_prorates_nothing():
    """Nothing has been paid for yet, so there is nothing to weight."""
    from nexus.billing.subscriptions import change_plan, ensure_subscription
    from nexus.models.billing import BillingProrationAdjustment

    await _seed()
    tid = await make_tenant(slug="pr3", name="PR Three")
    async with tenant_session(tid) as ts:
        sub = await ensure_subscription(ts, plan_id="starter")
        sub.current_period_start = None
        sub.current_period_end = None
        await ts.flush()
        await change_plan(ts, "growth", actor="admin@nexus.com")
        assert await ts.list(BillingProrationAdjustment) == []


async def test_a_provider_driven_subscription_is_not_prorated_by_us():
    """Stripe prorates its own subscription changes.

    Adding our lines on top would bill the difference twice — once on their invoice and once on
    ours — and the customer would be right to dispute it.
    """
    from nexus.billing.subscriptions import change_plan
    from nexus.models.billing import BillingProrationAdjustment

    await _seed()
    tid = await make_tenant(slug="pr4", name="PR Four")
    async with tenant_session(tid) as ts:
        sub = await _subscribe(ts, "starter")
        sub.psp_subscription_id = "sub_live_123"
        await ts.flush()
        await change_plan(ts, "growth", actor="admin@nexus.com")
        assert await ts.list(BillingProrationAdjustment) == []


async def test_proration_appears_on_the_period_invoice():
    from nexus.billing.rating import rate_period
    from nexus.billing.rollups import period_key
    from nexus.billing.subscriptions import change_plan
    from nexus.models.billing import BillingInvoiceLine

    await _seed()
    tid = await make_tenant(slug="pr5", name="PR Five")
    pk = period_key(_now(), "period")
    async with tenant_session(tid) as ts:
        await _subscribe(ts, "starter")
        await change_plan(ts, "growth", actor="admin@nexus.com")
        inv = await rate_period(ts, period_key=pk)
        lines = await ts.list(BillingInvoiceLine, BillingInvoiceLine.invoice_id == inv.id)

    prorations = [ln for ln in lines if ln.kind == "proration"]
    assert len(prorations) == 2, [ln.description for ln in lines]
    assert any(ln.amount_cents < 0 for ln in prorations), "the credit line must reduce the total"


async def test_re_rating_does_not_double_count_proration():
    """``rate_period`` rebuilds lines from scratch; adjustments must be read, never consumed."""
    from nexus.billing.rating import rate_period
    from nexus.billing.rollups import period_key
    from nexus.billing.subscriptions import change_plan

    await _seed()
    tid = await make_tenant(slug="pr6", name="PR Six")
    pk = period_key(_now(), "period")
    async with tenant_session(tid) as ts:
        await _subscribe(ts, "starter")
        await change_plan(ts, "growth", actor="admin@nexus.com")
        first = (await rate_period(ts, period_key=pk)).total_cents
        second = (await rate_period(ts, period_key=pk)).total_cents

    assert first == second


async def test_a_downgrade_credit_never_produces_a_card_charge():
    """A net credit must not be collected as a negative amount."""
    from nexus.billing.rating import rate_period
    from nexus.billing.rollups import period_key
    from nexus.billing.subscriptions import change_plan
    from nexus.models.billing import BillingPlan

    await _seed()
    tid = await make_tenant(slug="pr7", name="PR Seven")
    pk = period_key(_now(), "period")
    async with tenant_session(tid) as ts:
        # Downgrade late in the period: a large credit against a small remaining charge.
        await _subscribe(ts, "growth", days_in=28)
        await change_plan(ts, "starter", actor="admin@nexus.com")
        inv = await rate_period(ts, period_key=pk)
        starter = await ts.session.get(BillingPlan, "starter")
        assert starter is not None
        assert inv.total_cents >= 0, "a negative invoice would be charged as a negative amount"


# ---- trial expiry --------------------------------------------------------------------------------

async def test_an_expired_trial_with_a_payment_method_converts():
    from nexus.billing.lifecycle import run_trial_sweep
    from nexus.billing.subscriptions import ensure_subscription
    from nexus.models.billing import BillingSubscription

    await _seed()
    tid = await make_tenant(slug="tr1", name="TR One")
    async with tenant_session(tid) as ts:
        sub = await ensure_subscription(ts, plan_id="growth")
        sub.status = "trialing"
        sub.trial_end = _now() - timedelta(days=1)
        sub.psp_subscription_id = "sub_live_paid"
        await ts.flush()
        res = await run_trial_sweep(ts)
        assert res["converted"] == 1
        assert (await ts.list(BillingSubscription))[0].status == "active"


async def test_an_expired_trial_without_a_payment_method_is_cancelled():
    """Flipping it to active would manufacture a receivable that can never be collected."""
    from nexus.billing.lifecycle import run_trial_sweep
    from nexus.billing.subscriptions import ensure_subscription
    from nexus.models.billing import BillingSubscription

    await _seed()
    tid = await make_tenant(slug="tr2", name="TR Two")
    async with tenant_session(tid) as ts:
        sub = await ensure_subscription(ts, plan_id="growth")
        sub.status = "trialing"
        sub.trial_end = _now() - timedelta(days=1)
        sub.psp_subscription_id = None
        await ts.flush()
        res = await run_trial_sweep(ts)
        assert res["cancelled"] == 1
        assert (await ts.list(BillingSubscription))[0].status == "canceled"


async def test_a_live_trial_is_left_alone():
    from nexus.billing.lifecycle import run_trial_sweep
    from nexus.billing.subscriptions import ensure_subscription
    from nexus.models.billing import BillingSubscription

    await _seed()
    tid = await make_tenant(slug="tr3", name="TR Three")
    async with tenant_session(tid) as ts:
        sub = await ensure_subscription(ts, plan_id="growth")
        sub.status = "trialing"
        sub.trial_end = _now() + timedelta(days=5)
        await ts.flush()
        assert (await run_trial_sweep(ts))["converted"] == 0
        assert (await ts.list(BillingSubscription))[0].status == "trialing"


async def test_a_trial_with_no_end_date_is_never_swept():
    """An open-ended trial is a deliberate commercial arrangement, not an oversight to clean up."""
    from nexus.billing.lifecycle import run_trial_sweep
    from nexus.billing.subscriptions import ensure_subscription
    from nexus.models.billing import BillingSubscription

    await _seed()
    tid = await make_tenant(slug="tr4", name="TR Four")
    async with tenant_session(tid) as ts:
        sub = await ensure_subscription(ts, plan_id="growth")
        sub.status = "trialing"
        sub.trial_end = None
        await ts.flush()
        await run_trial_sweep(ts)
        assert (await ts.list(BillingSubscription))[0].status == "trialing"


async def test_the_trial_sweep_ignores_non_trialing_subscriptions():
    from nexus.billing.lifecycle import run_trial_sweep
    from nexus.billing.subscriptions import ensure_subscription
    from nexus.models.billing import BillingSubscription

    await _seed()
    tid = await make_tenant(slug="tr5", name="TR Five")
    async with tenant_session(tid) as ts:
        sub = await ensure_subscription(ts, plan_id="growth")
        sub.status = "past_due"
        sub.trial_end = _now() - timedelta(days=30)
        await ts.flush()
        await run_trial_sweep(ts)
        assert (await ts.list(BillingSubscription))[0].status == "past_due"


# ---- pause and resume ----------------------------------------------------------------------------

async def test_pausing_suspends_and_records_when():
    from nexus.billing.subscriptions import pause_subscription
    from nexus.models.billing import BillingSubscription

    await _seed()
    tid = await make_tenant(slug="pa1", name="PA One")
    async with tenant_session(tid) as ts:
        await _subscribe(ts, "growth")
        sub = await pause_subscription(ts, actor="admin@nexus.com")
        assert sub.status == "suspended"
        assert sub.meta.get("paused_at")
        assert (await ts.list(BillingSubscription))[0].status == "suspended"


async def test_pausing_a_past_due_subscription_is_refused():
    """``suspended`` is a status the dunning sweep ignores, so pausing would bury a real debt."""
    from nexus.billing.errors import BillingError
    from nexus.billing.subscriptions import pause_subscription

    await _seed()
    tid = await make_tenant(slug="pa2", name="PA Two")
    async with tenant_session(tid) as ts:
        sub = await _subscribe(ts, "growth")
        sub.status = "past_due"
        await ts.flush()
        try:
            await pause_subscription(ts, actor="admin@nexus.com")
        except BillingError as exc:
            assert "balance" in str(exc).lower()
        else:
            raise AssertionError("pausing a past_due subscription must be refused")


async def test_resuming_pushes_the_period_end_out_by_the_paused_days():
    """Otherwise the customer pays for thirty days and receives sixteen."""
    from nexus.billing.subscriptions import pause_subscription, resume_subscription

    await _seed()
    tid = await make_tenant(slug="pa3", name="PA Three")
    async with tenant_session(tid) as ts:
        sub = await _subscribe(ts, "growth")
        original_end = sub.current_period_end
        await pause_subscription(ts, actor="admin@nexus.com")
        sub.meta = {**sub.meta, "paused_at": (_now() - timedelta(days=7)).isoformat()}
        await ts.flush()
        resumed = await resume_subscription(ts, actor="admin@nexus.com")

    assert resumed.status == "active"
    assert resumed.current_period_end > original_end
    gained = (resumed.current_period_end - original_end).days
    assert gained == 7, f"expected the 7 paused days back, got {gained}"


async def test_resuming_something_that_is_not_paused_is_refused():
    from nexus.billing.errors import BillingError
    from nexus.billing.subscriptions import resume_subscription

    await _seed()
    tid = await make_tenant(slug="pa4", name="PA Four")
    async with tenant_session(tid) as ts:
        await _subscribe(ts, "growth")
        try:
            await resume_subscription(ts, actor="admin@nexus.com")
        except BillingError:
            pass
        else:
            raise AssertionError("resuming an active subscription must be refused")


async def test_a_paused_subscription_suspends_entitlements():
    """A pause that keeps full access is free service — the exact failure trial expiry fixes."""
    from nexus.billing.entitlements import resolve_entitlement
    from nexus.billing.subscriptions import pause_subscription

    await _seed()
    tid = await make_tenant(slug="pa5", name="PA Five")
    async with tenant_session(tid) as ts:
        await _subscribe(ts, "growth")
        before = await resolve_entitlement(ts, "ai.account_qa")
        assert before.mode != "disabled", "precondition: the capability is normally available"
        await pause_subscription(ts, actor="admin@nexus.com")
        after = await resolve_entitlement(ts, "ai.account_qa")

    assert after.mode == "disabled"
    assert after.source == "suspended"


async def test_resuming_restores_entitlements():
    from nexus.billing.entitlements import resolve_entitlement
    from nexus.billing.subscriptions import pause_subscription, resume_subscription

    await _seed()
    tid = await make_tenant(slug="pa6", name="PA Six")
    async with tenant_session(tid) as ts:
        await _subscribe(ts, "growth")
        await pause_subscription(ts, actor="admin@nexus.com")
        await resume_subscription(ts, actor="admin@nexus.com")
        assert (await resolve_entitlement(ts, "ai.account_qa")).mode != "disabled"


# ---- the admin surface ---------------------------------------------------------------------------

async def _platform_token(client, monkeypatch, *, slug: str, email: str):
    from nexus.core.config import get_settings

    await _seed()
    monkeypatch.setattr(get_settings(), "platform_admin_emails", email)
    return await signup(client, slug=slug, email=email, company=slug.upper())


async def test_admin_can_pause_and_resume_a_tenant(client, monkeypatch):
    token = await _platform_token(client, monkeypatch, slug="ap1", email="boss@nexus.com")
    tid = await make_tenant(slug="ap1t", name="AP1 Target")

    await client.post(f"/api/admin/billing/tenants/{tid}/subscription",
                      headers=auth(token), json={"plan_id": "growth"})

    r = await client.post(f"/api/admin/billing/tenants/{tid}/pause", headers=auth(token), json={})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "suspended"

    r = await client.post(f"/api/admin/billing/tenants/{tid}/resume", headers=auth(token), json={})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "active"


async def test_pause_refuses_a_tenant_owner(client):
    """Tenant RBAC grants nothing on the platform surface."""
    token = await signup(client, slug="ap2", email="o@ap2.com", company="AP2")
    r = await client.post("/api/admin/billing/tenants/whatever/pause",
                          headers=auth(token), json={})
    assert r.status_code in (401, 403)


async def test_pause_is_audited(client, monkeypatch):
    from sqlalchemy import select

    from nexus.core.db import get_sessionmaker
    from nexus.models.billing import BillingAuditLog

    token = await _platform_token(client, monkeypatch, slug="ap3", email="boss3@nexus.com")
    tid = await make_tenant(slug="ap3t", name="AP3 Target")
    await client.post(f"/api/admin/billing/tenants/{tid}/subscription",
                      headers=auth(token), json={"plan_id": "growth"})
    await client.post(f"/api/admin/billing/tenants/{tid}/pause", headers=auth(token), json={})

    async with get_sessionmaker()() as session:
        actions = [
            r.action for r in (await session.scalars(select(BillingAuditLog))).all()
        ]
    assert "subscription.pause" in actions


async def test_the_proration_preview_does_not_write(client, monkeypatch):
    """An admin must be able to see the money before committing to it."""
    from sqlalchemy import func, select

    from nexus.core.db import get_sessionmaker
    from nexus.models.billing import BillingProrationAdjustment

    token = await _platform_token(client, monkeypatch, slug="ap4", email="boss4@nexus.com")
    tid = await make_tenant(slug="ap4t", name="AP4 Target")
    await client.post(f"/api/admin/billing/tenants/{tid}/subscription",
                      headers=auth(token), json={"plan_id": "starter"})

    r = await client.get(f"/api/admin/billing/tenants/{tid}/proration-preview?plan_id=growth",
                         headers=auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert {"credit_cents", "charge_cents", "net_cents", "days_remaining"} <= set(body)

    async with get_sessionmaker()() as session:
        count = await session.scalar(select(func.count()).select_from(BillingProrationAdjustment))
    assert count == 0, "a preview must not write adjustments"


async def test_the_tenant_sees_its_own_pending_proration(client, monkeypatch):
    """The customer must be able to see the adjustment before the invoice arrives."""
    token = await _platform_token(client, monkeypatch, slug="ap5", email="boss5@nexus.com")
    from sqlalchemy import select

    from nexus.core.db import get_sessionmaker
    from nexus.models.identity import Tenant

    async with get_sessionmaker()() as s:
        tid = (await s.scalars(select(Tenant.id).where(Tenant.slug == "ap5"))).first()

    await client.post(f"/api/admin/billing/tenants/{tid}/subscription",
                      headers=auth(token), json={"plan_id": "starter"})
    # Give the subscription a live period, so the change has days to weight.
    async with tenant_session(tid) as ts:
        sub = (await ts.list(__import__(
            "nexus.models.billing", fromlist=["BillingSubscription"]
        ).BillingSubscription))[0]
        sub.current_period_start = _now() - timedelta(days=10)
        sub.current_period_end = sub.current_period_start + timedelta(days=30)
        await ts.flush()

    await client.post(f"/api/admin/billing/tenants/{tid}/subscription",
                      headers=auth(token), json={"plan_id": "growth"})

    r = await client.get("/api/billing/usage", headers=auth(token))
    assert r.status_code == 200, r.text
    assert "pending_proration_cents" in r.json()


async def test_a_paused_workspace_still_sees_its_plan_and_status(client, monkeypatch):
    """The page must explain the pause, not go blank.

    ``/billing/usage`` selected only trialing|active|past_due subscriptions, so a paused workspace
    fell through the "no subscription" branch: no plan name, no status, no capabilities. The screen
    read as "No plan assigned" with an empty page — indistinguishable from a broken account, and
    with no way for the customer to learn they had been paused.
    """
    from sqlalchemy import select

    from nexus.core.db import get_sessionmaker
    from nexus.models.identity import Tenant

    token = await _platform_token(client, monkeypatch, slug="ap6", email="boss6@nexus.com")
    async with get_sessionmaker()() as s:
        tid = (await s.scalars(select(Tenant.id).where(Tenant.slug == "ap6"))).first()

    await client.post(f"/api/admin/billing/tenants/{tid}/subscription",
                      headers=auth(token), json={"plan_id": "growth"})
    r = await client.post(f"/api/admin/billing/tenants/{tid}/pause",
                          headers=auth(token), json={})
    assert r.status_code == 200, r.text

    usage = await client.get("/api/billing/usage", headers=auth(token))
    assert usage.status_code == 200, usage.text
    body = usage.json()
    assert body["status"] == "suspended", "the customer must be able to see that they are paused"
    assert body["plan_name"], "a paused workspace still has a plan; blanking it looks like a bug"
