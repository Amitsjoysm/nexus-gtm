# tests/test_plan_authoring.py
"""Creating a plan the public price list will actually sell.

Until this existed, a ninth public tier needed a `plans.py` edit and a deploy. `CustomPlanDialog`
could build a bespoke per-tenant deal, but a custom plan is excluded from `GET /billing/plans` and
refused by checkout with a 409 — so there was no path from "we want to sell a new tier" to a
customer buying it, short of shipping code.
"""
from __future__ import annotations

import pytest

from tests.conftest import auth, signup


async def _superadmin(client, monkeypatch, *, slug: str, email: str) -> str:
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.billing.rates import sync_rates
    from nexus.core.config import get_settings

    await sync_catalog()
    await sync_plans()
    # Rate cards AND cost rates: the margin warning divides one by the other, and with no costs
    # seeded it has nothing to compare against and correctly says nothing.
    await sync_rates()
    monkeypatch.setattr(get_settings(), "platform_admin_emails", email)
    return await signup(client, slug=slug, email=email, company=slug.upper())


def _plan(**over) -> dict:
    body = {
        "plan_id": "scale", "name": "Scale", "base_plan_id": "growth",
        "base_price_cents": 24900, "included_credits": 12000,
    }
    body.update(over)
    return body


# ---- creating ------------------------------------------------------------------------------------

async def test_a_new_plan_is_a_draft_and_not_yet_on_the_price_list(client, monkeypatch):
    """The ladder must not gain a half-configured tier the moment a form is submitted."""
    token = await _superadmin(client, monkeypatch, slug="pa1", email="boss@pa1.com")
    r = await client.post("/api/admin/billing/plans", headers=auth(token), json=_plan())
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "draft"
    assert body["sellable"] is False
    assert body["plan_class"] == "standard"

    listed = (await client.get("/api/billing/plans", headers=auth(token))).json()
    assert not any(p["id"] == "scale" for p in listed)


async def test_activating_puts_it_on_the_customer_price_list(client, monkeypatch):
    """The whole point: a new sellable tier without a deploy."""
    token = await _superadmin(client, monkeypatch, slug="pa2", email="boss@pa2.com")
    await client.post("/api/admin/billing/plans", headers=auth(token), json=_plan())

    r = await client.put("/api/admin/billing/plans/scale/status", headers=auth(token),
                         json={"status": "active"})
    assert r.status_code == 200, r.text
    assert r.json()["sellable"] is True

    listed = (await client.get("/api/billing/plans", headers=auth(token))).json()
    row = next(p for p in listed if p["id"] == "scale")
    assert row["name"] == "Scale"
    assert row["base_price_cents"] == 24900


async def test_holding_takes_it_off_the_list_without_retiring_it(client, monkeypatch):
    """The control an operator wants when a price is wrong while customers are mid-purchase:
    stop offering it, leave existing subscribers alone."""
    token = await _superadmin(client, monkeypatch, slug="pa3", email="boss@pa3.com")
    await client.post("/api/admin/billing/plans", headers=auth(token),
                      json=_plan(status="active"))
    assert any(p["id"] == "scale"
               for p in (await client.get("/api/billing/plans", headers=auth(token))).json())

    await client.put("/api/admin/billing/plans/scale/status", headers=auth(token),
                     json={"status": "draft"})
    listed = (await client.get("/api/billing/plans", headers=auth(token))).json()
    assert not any(p["id"] == "scale" for p in listed)

    # And it is still there to bring back, not retired.
    admin_view = (await client.get("/api/admin/billing/plans", headers=auth(token))).json()
    assert next(p for p in admin_view if p["id"] == "scale")["status"] == "draft"


async def test_it_can_be_created_active_in_one_step(client, monkeypatch):
    """Draft is the default, not a mandate — an operator adding a tier they have already agreed
    should not need two calls."""
    token = await _superadmin(client, monkeypatch, slug="pa4", email="boss@pa4.com")
    r = await client.post("/api/admin/billing/plans", headers=auth(token),
                          json=_plan(status="active"))
    assert r.json()["sellable"] is True


# ---- entitlements --------------------------------------------------------------------------------

async def test_entitlements_are_cloned_from_the_base_plan(client, monkeypatch):
    """Never started empty. `resolve_entitlement` falls back to permissive catalog defaults for
    anything a plan does not list, so a blank new plan would silently grant nearly everything —
    the opposite of what a cheaper tier is for."""
    token = await _superadmin(client, monkeypatch, slug="pa5", email="boss@pa5.com")
    r = await client.post("/api/admin/billing/plans", headers=auth(token), json=_plan())
    assert r.json()["entitlements_cloned"] > 0


async def test_an_override_turns_a_module_off_for_the_new_tier(client, monkeypatch):
    """What actually makes a cheaper plan cheaper."""
    token = await _superadmin(client, monkeypatch, slug="pa6", email="boss@pa6.com")
    r = await client.post(
        "/api/admin/billing/plans", headers=auth(token),
        json=_plan(plan_id="lite", name="Lite", base_price_cents=900, included_credits=200,
                   status="active",
                   entitlement_overrides={"module.network": {"mode": "disabled"}}),
    )
    assert r.status_code == 201, r.text
    assert r.json()["overrides_applied"] == 1

    listed = (await client.get("/api/billing/plans", headers=auth(token))).json()
    lite = next(p for p in listed if p["id"] == "lite")
    base = next(p for p in listed if p["id"] == "growth")
    # Asserted against the BASE plan rather than by module name: the picker renders display names
    # ("Relationship graph module"), not capability ids, and hard-coding one couples this test to
    # marketing copy. What matters is that the override removed something the base plan had.
    assert len(lite["excludes"]) > len(base["excludes"])
    assert len(lite["includes"]) < len(base["includes"])


# ---- what it refuses -----------------------------------------------------------------------------

async def test_a_body_cannot_choose_its_own_plan_class(client, monkeypatch):
    """`plan_class` is decided by the service. An endpoint that accepted one would be a way to
    mint an `unlimited` or `internal` plan by typing a string — the migration keystone and the
    staff tier respectively."""
    token = await _superadmin(client, monkeypatch, slug="pa7", email="boss@pa7.com")
    r = await client.post("/api/admin/billing/plans", headers=auth(token),
                          json=_plan(plan_class="unlimited"))
    assert r.status_code == 422


async def test_reserved_ids_are_refused(client, monkeypatch):
    """`custom-` is the prefix minted per tenant; colliding would repoint a negotiated deal at a
    public tier."""
    token = await _superadmin(client, monkeypatch, slug="pa8", email="boss@pa8.com")
    for bad in ("free", "enterprise", "legacy-unlimited", "custom-acme"):
        r = await client.post("/api/admin/billing/plans", headers=auth(token),
                              json=_plan(plan_id=bad))
        assert r.status_code == 400, bad


async def test_creating_over_an_existing_plan_is_refused(client, monkeypatch):
    """Silently repricing a plan customers are subscribed to, because someone reused an id, is
    not a create."""
    token = await _superadmin(client, monkeypatch, slug="pa9", email="boss@pa9.com")
    r = await client.post("/api/admin/billing/plans", headers=auth(token),
                          json=_plan(plan_id="growth"))
    assert r.status_code == 400
    assert "already exists" in r.text


async def test_an_unknown_base_plan_is_refused(client, monkeypatch):
    token = await _superadmin(client, monkeypatch, slug="pa10", email="boss@pa10.com")
    r = await client.post("/api/admin/billing/plans", headers=auth(token),
                          json=_plan(base_plan_id="nope"))
    assert r.status_code == 400


async def test_a_tenant_owner_cannot_create_a_plan(client):
    """Setting a price customers pay is platform power."""
    token = await signup(client, slug="pa11", email="o@pa11.com", company="PA11")
    assert (await client.post("/api/admin/billing/plans", headers=auth(token),
                              json=_plan())).status_code == 403


async def test_a_non_standard_plan_cannot_be_published_to_the_price_list(client, monkeypatch):
    """Activating `legacy-unlimited` would put the migration keystone on sale."""
    token = await _superadmin(client, monkeypatch, slug="pa12", email="boss@pa12.com")
    r = await client.put("/api/admin/billing/plans/legacy-unlimited/status",
                         headers=auth(token), json={"status": "active"})
    assert r.status_code == 400
    assert "only standard plans are listed" in r.text


async def test_retired_is_not_reachable_from_the_status_switch(client, monkeypatch):
    """A different decision with a different blast radius; it has its own home in the editor."""
    token = await _superadmin(client, monkeypatch, slug="pa13", email="boss@pa13.com")
    await client.post("/api/admin/billing/plans", headers=auth(token), json=_plan())
    r = await client.put("/api/admin/billing/plans/scale/status", headers=auth(token),
                         json={"status": "retired"})
    assert r.status_code == 400


async def test_an_annual_interval_is_accepted(client, monkeypatch):
    token = await _superadmin(client, monkeypatch, slug="pa14", email="boss@pa14.com")
    r = await client.post("/api/admin/billing/plans", headers=auth(token),
                          json=_plan(plan_id="scale-annual", interval="year",
                                     base_price_cents=249000, included_credits=144000))
    assert r.status_code == 201, r.text
    assert r.json()["interval"] == "year"


async def test_a_nonsense_interval_is_refused(client, monkeypatch):
    token = await _superadmin(client, monkeypatch, slug="pa15", email="boss@pa15.com")
    r = await client.post("/api/admin/billing/plans", headers=auth(token),
                          json=_plan(interval="fortnight"))
    assert r.status_code == 400


# ---- the margin warning --------------------------------------------------------------------------

async def test_a_thin_margin_warns_but_still_creates(client, monkeypatch):
    """Deliberately not a refusal, unlike `rates.validate_rate`. A rate card below cost loses money
    on every call; a PLAN below the cost of its own credits is a normal commercial decision, and a
    hard floor would refuse the `free` tier that already exists."""
    token = await _superadmin(client, monkeypatch, slug="pa16", email="boss@pa16.com")
    r = await client.post("/api/admin/billing/plans", headers=auth(token),
                          json=_plan(plan_id="giveaway", name="Giveaway",
                                     base_price_cents=100, included_credits=500000))
    assert r.status_code == 201, r.text
    assert r.json()["warning"], "a 500k-credit plan at $1 should say something"


async def test_a_healthy_margin_says_nothing(client, monkeypatch):
    """A warning on every create would be noise, and noise is how a real one gets ignored."""
    token = await _superadmin(client, monkeypatch, slug="pa17", email="boss@pa17.com")
    r = await client.post("/api/admin/billing/plans", headers=auth(token),
                          json=_plan(plan_id="fat", base_price_cents=500000,
                                     included_credits=100))
    assert r.json()["warning"] == ""


# ---- housekeeping --------------------------------------------------------------------------------

async def test_the_id_is_normalised(client, monkeypatch):
    """Ids appear in URLs, Stripe metadata and audit rows."""
    token = await _superadmin(client, monkeypatch, slug="pa18", email="boss@pa18.com")
    r = await client.post("/api/admin/billing/plans", headers=auth(token),
                          json=_plan(plan_id="  Scale Annual!! "))
    assert r.json()["plan_id"] == "scale-annual"


async def test_creating_a_plan_is_audited(client, monkeypatch):
    """Setting a price is exactly the action that gets questioned later."""
    from sqlalchemy import select

    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.billing import BillingAuditLog

    token = await _superadmin(client, monkeypatch, slug="pa19", email="boss@pa19.com")
    await client.post("/api/admin/billing/plans", headers=auth(token), json=_plan())
    await client.put("/api/admin/billing/plans/scale/status", headers=auth(token),
                     json={"status": "active"})

    async with get_platform_sessionmaker()() as s:
        actions = [r.action for r in (await s.scalars(select(BillingAuditLog))).all()]
    assert "plan.create" in actions
    assert "plan.status" in actions


async def test_no_stripe_object_is_created_for_a_plan_nobody_has_bought(client, monkeypatch):
    """Matches the seeded plans: `create_checkout` calls `ensure_plan_price` on first purchase and
    caches the id. Publishing a price for a draft would litter the Stripe account with products for
    tiers that were never sold."""
    from nexus.billing.payments import NoopPaymentProvider, set_payment_provider

    provider = NoopPaymentProvider()
    set_payment_provider(provider)
    try:
        token = await _superadmin(client, monkeypatch, slug="pa20", email="boss@pa20.com")
        await client.post("/api/admin/billing/plans", headers=auth(token),
                          json=_plan(status="active"))
        assert provider.prices == {}
    finally:
        set_payment_provider(None)


async def test_the_sort_order_places_it_by_price(client, monkeypatch):
    """So a new tier lands in the right place on the ladder without the operator having to know
    what sort_order means."""
    token = await _superadmin(client, monkeypatch, slug="pa21", email="boss@pa21.com")
    cheap = (await client.post("/api/admin/billing/plans", headers=auth(token),
                               json=_plan(plan_id="cheap", base_price_cents=1000))).json()
    dear = (await client.post("/api/admin/billing/plans", headers=auth(token),
                              json=_plan(plan_id="dear", base_price_cents=90000))).json()
    assert cheap["sort_order"] < dear["sort_order"]


async def test_negative_money_is_refused(client, monkeypatch):
    token = await _superadmin(client, monkeypatch, slug="pa22", email="boss@pa22.com")
    r = await client.post("/api/admin/billing/plans", headers=auth(token),
                          json=_plan(base_price_cents=-1))
    assert r.status_code == 400


@pytest.mark.parametrize("bad", ["", "   ", "!!!"])
async def test_an_empty_id_is_refused(client, monkeypatch, bad):
    token = await _superadmin(client, monkeypatch, slug=f"pa23{abs(hash(bad)) % 99}",
                              email=f"boss{abs(hash(bad)) % 99}@pa23.com")
    r = await client.post("/api/admin/billing/plans", headers=auth(token),
                          json=_plan(plan_id=bad))
    assert r.status_code == 400
