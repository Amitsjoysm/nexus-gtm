"""A new monthly plan can bring its annual twin, at a chosen % off (2026-10-12).

Asked: "when a new plan is created it should also create the annual plan, with a % off option on
annual subscriptions." The seeded ladder sells each tier twice, monthly and annual, as two rows that
pair by name (`X` and `X-annual`, `plans.interval_pairs`), so the price list shows one card with a
Monthly/Annual switch. A plan made in Admin had no annual unless someone made a second plan by hand,
with the right id, price, credits and position.

The twin follows the seeded rules: twelve months less the discount, to the nearest whole unit
($99 -> $950 at 20%); credits exactly twelve months' worth, because the discount is on price and
never on credits; the same base entitlements, seats, trial and status; sorted beside the monthly.
"""
from __future__ import annotations

import pytest

from tests.conftest import auth
from tests.test_plan_authoring import _plan, _superadmin


async def _row(plan_id):
    from nexus.core.db import get_sessionmaker
    from nexus.models.billing import BillingPlan

    async with get_sessionmaker()() as s:
        return await s.get(BillingPlan, plan_id)


async def _entitlements(plan_id) -> dict:
    from sqlalchemy import select

    from nexus.core.db import get_sessionmaker
    from nexus.models.billing import BillingPlanEntitlement

    async with get_sessionmaker()() as s:
        rows = (await s.scalars(select(BillingPlanEntitlement).where(
            BillingPlanEntitlement.plan_id == plan_id))).all()
        return {r.capability_id: (r.mode, r.quota) for r in rows}


def test_the_annual_price_is_twelve_months_less_the_discount_to_the_whole_unit():
    from nexus.billing.plan_authoring import annual_price_cents

    assert annual_price_cents(9900, 20) == 95000        # the seeded Launch annual
    assert annual_price_cents(19900, 20) == 191000      # the seeded Accelerate annual
    assert annual_price_cents(24900, 15) == 254000      # 2539.80 rounds up
    assert annual_price_cents(1000, 0) == 12000
    assert annual_price_cents(0, 20) == 0


async def test_a_new_monthly_plan_brings_its_annual_twin(client, monkeypatch):
    token = await _superadmin(client, monkeypatch, slug="tw1", email="boss@tw1.com")
    r = await client.post("/api/admin/billing/plans", headers=auth(token),
                          json=_plan(annual_discount_pct=20, seat_price_cents=1000, max_seats=8,
                                     trial_days=7))
    assert r.status_code == 201, r.text
    assert r.json()["annual"]["plan_id"] == "scale-annual"

    monthly, annual = await _row("scale"), await _row("scale-annual")
    assert (annual.interval, annual.status, annual.plan_class) == ("year", "draft", "standard")
    assert annual.name == "Scale (annual)"
    assert annual.base_price_cents == 239000          # 249 x 12 x 0.8 = 2390.40
    assert annual.seat_price_cents == 9600            # the seat price takes the same discount
    assert annual.included_credits == 144000          # 12 x 12,000: no discount on credits
    assert (annual.max_seats, annual.trial_days) == (8, 7)
    assert annual.sort_order == monthly.sort_order + 1
    assert annual.meta.get("annual_of") == "scale" and annual.meta.get("annual_discount_pct") == 20
    assert await _entitlements("scale-annual") == await _entitlements("scale")


async def test_published_together_they_are_one_card_and_both_exist_at_stripe(client, monkeypatch):
    from nexus.billing.payments import NoopPaymentProvider, set_payment_provider

    provider = NoopPaymentProvider()
    set_payment_provider(provider)
    try:
        token = await _superadmin(client, monkeypatch, slug="tw2", email="boss@tw2.com")
        r = await client.post("/api/admin/billing/plans", headers=auth(token),
                              json=_plan(status="active", annual_discount_pct=20))
        assert r.status_code == 201, r.text
        assert {"scale", "scale-annual"} <= set(provider.prices)
        listed = {p["id"]: p for p in (await client.get("/api/billing/plans",
                                                        headers=auth(token))).json()}
        assert listed["scale"]["counterpart_id"] == "scale-annual"
        assert listed["scale-annual"]["counterpart_id"] == "scale"
        assert listed["scale"]["family"] == listed["scale-annual"]["family"]
    finally:
        set_payment_provider(None)


async def test_without_the_option_only_the_monthly_plan_is_made(client, monkeypatch):
    token = await _superadmin(client, monkeypatch, slug="tw3", email="boss@tw3.com")
    r = await client.post("/api/admin/billing/plans", headers=auth(token), json=_plan())
    assert r.status_code == 201 and "annual" not in r.json()
    assert await _row("scale-annual") is None


async def test_an_annual_id_already_taken_refuses_the_whole_create(client, monkeypatch):
    token = await _superadmin(client, monkeypatch, slug="tw4", email="boss@tw4.com")
    await client.post("/api/admin/billing/plans", headers=auth(token),
                      json=_plan(plan_id="scale-annual", interval="year"))
    r = await client.post("/api/admin/billing/plans", headers=auth(token),
                          json=_plan(annual_discount_pct=20))
    assert r.status_code == 400 and "scale-annual" in r.json()["detail"]
    assert await _row("scale") is None


@pytest.mark.parametrize("over", [
    {"interval": "year", "annual_discount_pct": 20},   # an annual plan has no annual twin
    {"annual_discount_pct": -5},
    {"annual_discount_pct": 91},
])
async def test_a_twin_that_makes_no_sense_is_refused(client, monkeypatch, over):
    token = await _superadmin(client, monkeypatch, slug="tw5", email="boss@tw5.com")
    r = await client.post("/api/admin/billing/plans", headers=auth(token), json=_plan(**over))
    assert r.status_code in (400, 422), r.text
    assert await _row("scale") is None


async def test_creating_the_pair_is_audited_with_both(client, monkeypatch):
    from sqlalchemy import select

    from nexus.core.db import get_sessionmaker
    from nexus.models.billing import BillingAuditLog

    token = await _superadmin(client, monkeypatch, slug="tw6", email="boss@tw6.com")
    await client.post("/api/admin/billing/plans", headers=auth(token),
                      json=_plan(annual_discount_pct=25))
    async with get_sessionmaker()() as s:
        row = (await s.scalars(select(BillingAuditLog).where(
            BillingAuditLog.action == "plan.create"))).first()
    assert row.after["annual"]["plan_id"] == "scale-annual"


def test_the_dialog_offers_it_and_previews_the_server_price():
    """The preview must be the server's arithmetic, or the dialog quotes one price and the plan is
    made at another."""
    from pathlib import Path

    page = (Path(__file__).resolve().parents[1]
            / "frontend/src/pages/admin/PlanDialog.tsx").read_text(encoding="utf-8")
    assert "annual_discount_pct" in page
    assert "+ 5000) / 10000" in page
