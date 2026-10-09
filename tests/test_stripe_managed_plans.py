# tests/test_stripe_managed_plans.py
"""Plans live in Stripe, and only a superadmin creates, edits, publishes, holds or syncs them
(decided 2026-10-09)."""
from __future__ import annotations

import pytest

from tests.conftest import auth
from tests.stripe_fakes import TwoAccountStripe
from tests.test_admin_permissions import _bootstrap_admin, _make_role
from tests.test_plan_authoring import _plan, _superadmin


@pytest.fixture
def stripe():
    from nexus.billing.payments import set_payment_provider

    fake = TwoAccountStripe()
    set_payment_provider(fake)
    yield fake
    set_payment_provider(None)


async def _scale(client, token, **over):
    r = await client.post("/api/admin/billing/plans", headers=auth(token), json=_plan(**over))
    assert r.status_code == 201, r.text
    return r


async def _plans(client, token) -> dict:
    rows = (await client.get("/api/admin/billing/plans", headers=auth(token))).json()
    return {p["id"]: p for p in rows}


async def test_finance_cannot_create_edit_publish_or_sync_plans(client, monkeypatch):
    boss = await _bootstrap_admin(client, monkeypatch, "smp1")
    finance = await _make_role(client, boss, "cfo@smp1.com", "finance", "smp1f")
    h = auth(finance)
    assert (await client.post("/api/admin/billing/plans", headers=h,
                              json=_plan(plan_id="fin"))).status_code == 403
    assert (await client.patch("/api/admin/billing/plans/launch", headers=h,
                               json={"base_price_cents": 1})).status_code == 403
    assert (await client.put("/api/admin/billing/plans/launch/status", headers=h,
                             json={"status": "draft"})).status_code == 403
    assert (await client.post("/api/admin/billing/plans/stripe-sync",
                              headers=h)).status_code == 403
    # What Finance keeps: rate cards are still theirs.
    kept = await client.put("/api/admin/billing/rates/ai.research", headers=h,
                            json={"credits_per_unit": 50})
    assert kept.status_code != 403, kept.text


async def test_a_draft_costs_nothing_at_stripe_and_publishing_creates_the_price(
        client, monkeypatch, stripe):
    token = await _superadmin(client, monkeypatch, slug="smp2", email="boss@smp2.com")
    await _scale(client, token)
    assert stripe.ensured == []
    r = await client.put("/api/admin/billing/plans/scale/status", headers=auth(token),
                         json={"status": "active"})
    assert r.status_code == 200, r.text
    assert stripe.ensured[-1]["plan_id"] == "scale"
    scale = (await _plans(client, token))["scale"]
    assert scale["stripe_price_id"].startswith("price_acct_old")
    assert scale["stripe_account"] == "acct_old"


async def test_when_stripe_refuses_the_plan_stays_unpublished(client, monkeypatch, stripe):
    from nexus.billing.payments import PaymentError

    token = await _superadmin(client, monkeypatch, slug="smp3", email="boss@smp3.com")
    await _scale(client, token)

    async def refuse(**kw):
        raise PaymentError("stripe /prices -> 400: Invalid currency")

    monkeypatch.setattr(stripe, "ensure_plan_price", refuse)
    r = await client.put("/api/admin/billing/plans/scale/status", headers=auth(token),
                         json={"status": "active"})
    assert r.status_code == 502 and "Invalid currency" in r.text
    assert (await _plans(client, token))["scale"]["status"] == "draft"


async def test_a_new_price_on_a_plan_on_sale_creates_a_new_stripe_price(
        client, monkeypatch, stripe):
    token = await _superadmin(client, monkeypatch, slug="smp4", email="boss@smp4.com")
    await _scale(client, token, status="active")
    old = stripe.prices["scale"]["price_id"]
    r = await client.patch("/api/admin/billing/plans/scale", headers=auth(token),
                           json={"base_price_cents": 29900})
    assert r.status_code == 200, r.text
    assert stripe.prices["scale"]["amount_cents"] == 29900
    assert stripe.price_active[old] is False


async def test_renaming_renames_the_stripe_product(client, monkeypatch, stripe):
    token = await _superadmin(client, monkeypatch, slug="smp5", email="boss@smp5.com")
    await _scale(client, token, status="active")
    r = await client.patch("/api/admin/billing/plans/scale", headers=auth(token),
                           json={"name": "Scale Pro"})
    assert r.status_code == 200, r.text
    assert stripe.renames[-1]["name"] == "Scale Pro"


async def test_holding_archives_the_price(client, monkeypatch, stripe):
    token = await _superadmin(client, monkeypatch, slug="smp6", email="boss@smp6.com")
    await _scale(client, token, status="active")
    price = stripe.prices["scale"]["price_id"]
    r = await client.put("/api/admin/billing/plans/scale/status", headers=auth(token),
                         json={"status": "draft"})
    assert r.status_code == 200, r.text
    assert stripe.price_active[price] is False


async def test_sync_creates_what_is_missing_and_reports_it(client, monkeypatch, stripe):
    token = await _superadmin(client, monkeypatch, slug="smp7", email="boss@smp7.com")
    r = await client.post("/api/admin/billing/plans/stripe-sync", headers=auth(token))
    assert r.status_code == 200, r.text
    assert r.json()["account"] == "acct_old"
    rows = {row["plan_id"]: row for row in r.json()["plans"]}
    assert rows["launch"]["outcome"] == "created"
    assert rows["accelerate"]["outcome"] == "created"
    assert "free" not in rows
    again = (await client.post("/api/admin/billing/plans/stripe-sync",
                               headers=auth(token))).json()["plans"]
    assert {row["outcome"] for row in again} == {"reused"}


async def test_sync_without_stripe_says_so(client, monkeypatch):
    token = await _superadmin(client, monkeypatch, slug="smp8", email="boss@smp8.com")
    r = await client.post("/api/admin/billing/plans/stripe-sync", headers=auth(token))
    assert r.status_code == 409 and "Payments tab" in r.text
