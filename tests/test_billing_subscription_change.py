# tests/test_billing_subscription_change.py
"""Can a platform admin actually move a tenant between plans, and does it take effect?

Two reported symptoms, which turn out to be different problems:

* "superadmin not able to change subscriptions" — a real 500 on any tenant whose subscription is
  not in `ACTIVE_STATUSES`.
* "billing subscriptions not getting applied" — the plan change lands, but nothing enforces it,
  because `NEXUS_BILLING_ENFORCEMENT` defaults to `shadow`.
"""
from __future__ import annotations

from sqlalchemy import select

from tests.conftest import auth, signup


async def _admin(client, monkeypatch, slug: str):
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.core.config import get_settings
    from nexus.core.db import get_sessionmaker
    from nexus.models.identity import Tenant

    await sync_catalog()
    await sync_plans()
    monkeypatch.setattr(get_settings(), "platform_admin_emails", "boss@nexus.com")
    token = await signup(client, slug=slug, email="boss@nexus.com", company=slug.upper())
    async with get_sessionmaker()() as s:
        tid = (await s.scalars(select(Tenant.id).where(Tenant.slug == slug))).first()
    return token, tid


async def _give_subscription(client, token, tid: str, plan_id: str = "growth") -> None:
    """A fresh signup has no subscription row; create one through the admin endpoint."""
    r = await client.post(f"/api/admin/billing/tenants/{tid}/subscription",
                          headers=auth(token), json={"plan_id": plan_id})
    assert r.status_code == 200, r.text


async def _set_status(tid: str, status: str) -> None:
    from nexus.models.billing import BillingSubscription
    from nexus.workers.tasks import tenant_session

    async with tenant_session(tid) as ts:
        sub = (await ts.list(BillingSubscription, limit=1))[0]
        sub.status = status
        await ts.flush()


async def _plan_of(tid: str) -> tuple[str, str]:
    from nexus.models.billing import BillingSubscription
    from nexus.workers.tasks import tenant_session

    async with tenant_session(tid) as ts:
        sub = (await ts.list(BillingSubscription, limit=1))[0]
        return sub.plan_id, sub.status


# ---- the happy path still works ----------------------------------------------------------------

async def test_an_admin_can_move_an_active_tenant_between_plans(client, monkeypatch):
    token, tid = await _admin(client, monkeypatch, "subc1")
    r = await client.post(f"/api/admin/billing/tenants/{tid}/subscription",
                          headers=auth(token), json={"plan_id": "growth"})
    assert r.status_code == 200, r.text
    assert await _plan_of(tid) == ("growth", "active")


# ---- THE BUG -----------------------------------------------------------------------------------

async def test_an_admin_can_change_the_plan_of_a_SUSPENDED_tenant(client, monkeypatch):
    """`_active` only matches ACTIVE_STATUSES = (trialing, active, past_due).

    For a `suspended` tenant it returns None, so `change_plan` falls through to
    `ensure_subscription`, which returns None because a row already exists — and the endpoint then
    reads `sub.plan_id` off None. A 500.

    This is precisely the tenant an admin most needs to move: somebody suspended for non-payment
    who has just paid, or who is being put onto a smaller plan to recover the account.
    """
    token, tid = await _admin(client, monkeypatch, "subc2")
    await _give_subscription(client, token, tid, "growth")
    await _set_status(tid, "suspended")

    r = await client.post(f"/api/admin/billing/tenants/{tid}/subscription",
                          headers=auth(token), json={"plan_id": "growth"})
    assert r.status_code == 200, f"admin could not move a suspended tenant: {r.status_code} {r.text}"
    plan, status = await _plan_of(tid)
    assert plan == "growth"
    # Taking a new plan reactivates: leaving it suspended would mean the admin's change had no
    # effect the customer could see, which is the reported symptom.
    assert status == "active"


async def test_an_admin_can_change_the_plan_of_a_CANCELED_tenant(client, monkeypatch):
    """A win-back is the other case: a canceled customer coming back onto a paid plan."""
    token, tid = await _admin(client, monkeypatch, "subc3")
    await _give_subscription(client, token, tid, "growth")
    await _set_status(tid, "canceled")

    r = await client.post(f"/api/admin/billing/tenants/{tid}/subscription",
                          headers=auth(token), json={"plan_id": "starter"})
    assert r.status_code == 200, f"admin could not revive a canceled tenant: {r.status_code} {r.text}"
    assert (await _plan_of(tid))[0] == "starter"


async def test_no_second_subscription_row_is_created(client, monkeypatch):
    """'One subscription per tenant' must hold — two rows makes rating ambiguous."""
    from nexus.models.billing import BillingSubscription
    from nexus.workers.tasks import tenant_session

    token, tid = await _admin(client, monkeypatch, "subc4")
    await _give_subscription(client, token, tid, "growth")
    await _set_status(tid, "suspended")
    await client.post(f"/api/admin/billing/tenants/{tid}/subscription",
                      headers=auth(token), json={"plan_id": "growth"})
    async with tenant_session(tid) as ts:
        assert len(await ts.list(BillingSubscription)) == 1


# ---- "not getting applied" is a different thing -------------------------------------------------

async def test_the_plan_change_is_visible_to_the_entitlement_engine(client, monkeypatch):
    """The change lands in the data — proving the write works."""
    token, tid = await _admin(client, monkeypatch, "subc5")
    await client.post(f"/api/admin/billing/tenants/{tid}/subscription",
                      headers=auth(token), json={"plan_id": "free"})

    r = await client.get("/api/billing/entitlements", headers=auth(token))
    body = r.json()
    assert body["plan"] == "free"
    modules = {m["capability_id"]: m for m in body["modules"]}
    # Free disables these in the seed, and the engine resolves that correctly.
    assert modules["module.network"]["included"] is False
    assert modules["module.outreach"]["included"] is False


async def test_but_nothing_is_enforced_under_the_default_shadow_mode(client, monkeypatch):
    """THE answer to 'subscriptions not getting applied'.

    NEXUS_BILLING_ENFORCEMENT defaults to `shadow`: the engine resolves every entitlement and then
    ALLOWS anyway. So a tenant moved to `free` still has full access, and the plan change looks
    like it did nothing. That is the documented, deliberate default — not a bug — but it is
    indistinguishable from a broken plan change unless you know to look at `gating_active`.
    """
    from nexus.core.config import get_settings

    token, tid = await _admin(client, monkeypatch, "subc6")
    await client.post(f"/api/admin/billing/tenants/{tid}/subscription",
                      headers=auth(token), json={"plan_id": "free"})

    assert get_settings().billing_enforcement == "shadow"
    body = (await client.get("/api/billing/entitlements", headers=auth(token))).json()
    assert body["gating_active"] is False          # nothing will actually be refused

    monkeypatch.setattr(get_settings(), "billing_enforcement", "on")
    body = (await client.get("/api/billing/entitlements", headers=auth(token))).json()
    assert body["gating_active"] is True           # the SAME plan now actually gates
