# tests/test_admin_subscription_crud.py
"""Full lifecycle control over a subscription from the Control plane.

Create and change already existed, as did pause and resume. Cancel had a service function since M6
and **no endpoint**, so the one lifecycle step a support admin most often performs was the one they
could not — and the workaround, moving the customer to `free`, leaves the subscription `active` on
a $0 plan, which reads as a live customer in revenue, in the directory, and in every count that
filters on status.
"""
from __future__ import annotations

from tests.conftest import auth, signup


async def _superadmin(client, monkeypatch, *, slug: str, email: str) -> str:
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "platform_admin_emails", email)
    return await signup(client, slug=slug, email=email, company=slug.upper())


async def _customer(client, token, *, slug: str, email: str, company: str,
                    plan: str = "growth") -> str:
    """A workspace with a subscription, created through the same endpoint an operator would use.

    A bare signup has no subscription row offline, and every test here is about editing one.
    The catalog and plans are seeded first: `billing_plans` is a TABLE, so "growth" does not exist
    until it is synced, and the endpoint correctly 404s on a plan it cannot find.
    """
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans

    await sync_catalog()
    await sync_plans()
    await signup(client, slug=slug, email=email, company=company)
    rows = (await client.get(f"/api/admin/billing/customers?q={company}",
                             headers=auth(token))).json()
    tenant_id = rows[0]["tenant_id"]
    r = await client.post(f"/api/admin/billing/tenants/{tenant_id}/subscription",
                          headers=auth(token), json={"plan_id": plan})
    assert r.status_code == 200, r.text
    return tenant_id


async def test_reading_one_subscription_returns_the_full_terms(client, monkeypatch):
    """The list view omits trial end, period end and the PSP linkage — exactly the fields an
    operator opens a customer to look at."""
    token = await _superadmin(client, monkeypatch, slug="sc1", email="boss@sc1.com")
    tid = await _customer(client, token, slug="subco", email="o@subco.com", company="Sub Co")

    r = await client.get(f"/api/admin/billing/tenants/{tid}/subscription", headers=auth(token))
    assert r.status_code == 200, r.text
    sub = r.json()["subscription"]
    assert sub is not None
    for field in ("plan_id", "status", "current_period_end", "trial_end",
                  "cancel_at_period_end", "psp_subscription_id"):
        assert field in sub


async def test_a_workspace_with_no_subscription_is_not_a_404(client, monkeypatch):
    """"No subscription" is a real, actionable state — it is precisely who an operator is about to
    put on a plan. A 404 would send them looking for a missing workspace instead."""
    token = await _superadmin(client, monkeypatch, slug="sc2", email="boss@sc2.com")
    r = await client.get("/api/admin/billing/tenants/no-such-tenant/subscription",
                         headers=auth(token))
    assert r.status_code == 200
    assert r.json()["subscription"] is None


async def test_cancelling_at_period_end_keeps_access_bought_and_paid_for(client, monkeypatch):
    """The default, because the customer paid through the period. Ending access the moment they
    ask to cancel takes back something they already bought."""
    token = await _superadmin(client, monkeypatch, slug="sc3", email="boss@sc3.com")
    tid = await _customer(client, token, slug="cancelco", email="o@cancelco.com",
                          company="Cancel Co")

    r = await client.post(f"/api/admin/billing/tenants/{tid}/subscription/cancel",
                          headers=auth(token), json={"reason": "downgrading"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cancel_at_period_end"] is True
    # Still active until the period ends — that is the whole point of the default.
    assert body["status"] != "canceled"


async def test_an_immediate_cancellation_has_to_be_asked_for(client, monkeypatch):
    token = await _superadmin(client, monkeypatch, slug="sc4", email="boss@sc4.com")
    tid = await _customer(client, token, slug="nowco", email="o@nowco.com", company="Now Co")

    body = (await client.post(f"/api/admin/billing/tenants/{tid}/subscription/cancel",
                              headers=auth(token),
                              json={"at_period_end": False, "reason": "fraud"})).json()
    assert body["status"] == "canceled"
    assert body["cancel_at_period_end"] is False


async def test_a_cancellation_is_audited_with_the_reason(client, monkeypatch):
    """Cancelling someone's service is exactly the action that gets questioned later."""
    from sqlalchemy import select

    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.billing import BillingAuditLog

    token = await _superadmin(client, monkeypatch, slug="sc5", email="boss@sc5.com")
    tid = await _customer(client, token, slug="auditco", email="o@auditco.com",
                          company="Audit Co")
    await client.post(f"/api/admin/billing/tenants/{tid}/subscription/cancel",
                      headers=auth(token), json={"reason": "customer emailed 2026-08-25"})

    async with get_platform_sessionmaker()() as s:
        rows = list((await s.scalars(select(BillingAuditLog))).all())
    entry = next(r for r in rows if r.action == "subscription.cancel")
    assert entry.subject_tenant_id == tid
    assert "customer emailed" in (entry.note or "")


async def test_patching_extends_a_trial_without_touching_the_plan(client, monkeypatch):
    """Extending a trial after a support conversation previously needed a database session."""
    token = await _superadmin(client, monkeypatch, slug="sc6", email="boss@sc6.com")
    tid = await _customer(client, token, slug="trialco", email="o@trialco.com",
                          company="Trial Co")

    before = (await client.get(f"/api/admin/billing/tenants/{tid}/subscription",
                               headers=auth(token))).json()["subscription"]
    r = await client.patch(f"/api/admin/billing/tenants/{tid}/subscription", headers=auth(token),
                           json={"trial_end": "2026-12-31T00:00:00Z", "reason": "extended"})
    assert r.status_code == 200, r.text
    assert r.json()["changed"] == ["trial_end"]

    after = (await client.get(f"/api/admin/billing/tenants/{tid}/subscription",
                              headers=auth(token))).json()["subscription"]
    assert after["trial_end"].startswith("2026-12-31")
    # The plan is untouched: repricing a customer because a form posted every field it had loaded
    # is the accident that keeps `plan_id` out of this endpoint entirely.
    assert after["plan_id"] == before["plan_id"]


async def test_a_plan_change_cannot_be_smuggled_through_the_patch(client, monkeypatch):
    """`plan_id` is absent from the schema and `extra="forbid"` rejects it. Changing a plan runs
    proration — arithmetic with consequences — so it keeps its own endpoint."""
    token = await _superadmin(client, monkeypatch, slug="sc7", email="boss@sc7.com")
    tid = await _customer(client, token, slug="planco", email="o@planco.com", company="Plan Co")

    r = await client.patch(f"/api/admin/billing/tenants/{tid}/subscription", headers=auth(token),
                           json={"plan_id": "growth"})
    assert r.status_code == 422


async def test_a_status_outside_the_vocabulary_is_refused(client, monkeypatch):
    """Rating and entitlements switch on `status`. A value outside SUBSCRIPTION_STATUSES is not a
    stricter setting — it is a subscription neither system can reason about."""
    token = await _superadmin(client, monkeypatch, slug="sc8", email="boss@sc8.com")
    tid = await _customer(client, token, slug="statusco", email="o@statusco.com",
                          company="Status Co")

    r = await client.patch(f"/api/admin/billing/tenants/{tid}/subscription", headers=auth(token),
                           json={"status": "vip"})
    assert r.status_code == 400
    assert "status must be one of" in r.text


async def test_an_empty_patch_is_refused(client, monkeypatch):
    """Rather than reporting success for a request that changed nothing."""
    token = await _superadmin(client, monkeypatch, slug="sc9", email="boss@sc9.com")
    tid = await _customer(client, token, slug="emptyco", email="o@emptyco.com",
                          company="Empty Co")

    r = await client.patch(f"/api/admin/billing/tenants/{tid}/subscription",
                           headers=auth(token), json={})
    assert r.status_code == 400


async def test_negative_seats_are_refused(client, monkeypatch):
    token = await _superadmin(client, monkeypatch, slug="sc10", email="boss@sc10.com")
    tid = await _customer(client, token, slug="seatco", email="o@seatco.com", company="Seat Co")

    r = await client.patch(f"/api/admin/billing/tenants/{tid}/subscription",
                           headers=auth(token), json={"seats_included": -1})
    assert r.status_code == 400


async def test_a_tenant_owner_cannot_cancel_or_patch(client):
    """Subscription writes are platform power. A workspace owner cancelling their own billing row
    directly would leave them with the product and no subscription."""
    token = await signup(client, slug="sc11", email="o@sc11.com", company="SC11")
    assert (await client.post("/api/admin/billing/tenants/x/subscription/cancel",
                              headers=auth(token))).status_code == 403
    assert (await client.patch("/api/admin/billing/tenants/x/subscription",
                               headers=auth(token), json={"status": "active"})).status_code == 403
