# tests/test_price_list.py
"""A superadmin chooses which plans the customer price list shows, at most four cards
(asked 2026-10-09: "superadmin has option to show or hide which plans visible to all, not more
than 4").

A card is a tier: a plan sold monthly and annually is one card, the way PlanPicker draws it, so
the seeded ladder (Free, Launch, Accelerate) is three. Hiding is presentation: a hidden plan stays
on sale for existing subscribers, console Checkout links and plan changes. Hold is still how a plan
stops being sold.
"""
from __future__ import annotations

from tests.conftest import auth
from tests.test_admin_permissions import _bootstrap_admin, _make_role
from tests.test_plan_authoring import _plan, _superadmin


def _listed(client, token):
    return client.get("/api/billing/plans", headers=auth(token))


def _show(client, token, plan_id, shown):
    return client.put(f"/api/admin/billing/plans/{plan_id}/price-list", headers=auth(token),
                      json={"shown": shown})


async def _admin_rows(client, token) -> dict:
    rows = (await client.get("/api/admin/billing/plans", headers=auth(token))).json()
    return {r["id"]: r for r in rows}


async def _publish(client, token, plan_id: str, **over):
    r = await client.post("/api/admin/billing/plans", headers=auth(token),
                          json=_plan(plan_id=plan_id, name=plan_id.title(), **over))
    assert r.status_code == 201, r.text
    return await client.put(f"/api/admin/billing/plans/{plan_id}/status", headers=auth(token),
                            json={"status": "active"})


# ---- nothing changes until someone hides a plan ----------------------------------------------------

async def test_every_plan_on_sale_is_shown_until_someone_hides_one(client, monkeypatch):
    token = await _superadmin(client, monkeypatch, slug="pl1", email="boss@pl1.com")
    ids = {p["id"] for p in (await _listed(client, token)).json()}
    assert ids == {"free", "launch", "launch-annual", "accelerate", "accelerate-annual"}
    rows = await _admin_rows(client, token)
    assert rows["launch"]["shown"] is True and rows["launch"]["on_price_list"] is True
    assert rows["launch"]["price_list_family"] == "launch"
    assert rows["launch-annual"]["price_list_family"] == "launch"
    assert rows["enterprise"]["on_price_list"] is False


# ---- hide and show -------------------------------------------------------------------------------

async def test_a_hidden_plan_leaves_the_price_list_and_comes_back(client, monkeypatch):
    token = await _superadmin(client, monkeypatch, slug="pl2", email="boss@pl2.com")
    r = await _show(client, token, "accelerate-annual", False)
    assert r.status_code == 200, r.text
    listed = (await _listed(client, token)).json()
    assert "accelerate-annual" not in {p["id"] for p in listed}
    accelerate = next(p for p in listed if p["id"] == "accelerate")
    assert accelerate["counterpart_id"] is None          # sold monthly only now, on its own card
    assert (await _show(client, token, "accelerate-annual", True)).status_code == 200
    assert "accelerate-annual" in {p["id"] for p in (await _listed(client, token)).json()}


async def test_a_hidden_plan_is_still_on_sale(client, monkeypatch):
    """Hiding is the price list's business; Hold is how a plan stops being sold."""
    from nexus.billing.payments import NoopPaymentProvider, set_payment_provider

    set_payment_provider(NoopPaymentProvider())
    try:
        token = await _superadmin(client, monkeypatch, slug="pl3", email="boss@pl3.com")
        assert (await _show(client, token, "accelerate", False)).status_code == 200
        r = await client.post("/api/billing/checkout", headers=auth(token),
                              json={"plan_id": "accelerate"})
        assert r.status_code == 200, r.text
        rows = await _admin_rows(client, token)
        assert rows["accelerate"]["status"] == "active" and rows["accelerate"]["shown"] is False
    finally:
        set_payment_provider(None)


async def test_showing_and_hiding_is_audited(client, monkeypatch):
    from sqlalchemy import select

    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.billing import BillingAuditLog

    token = await _superadmin(client, monkeypatch, slug="pl4", email="boss@pl4.com")
    await _show(client, token, "launch-annual", False)
    async with get_platform_sessionmaker()() as s:
        entry = (await s.scalars(select(BillingAuditLog).where(
            BillingAuditLog.action == "plan.price_list"))).first()
    assert entry is not None and entry.target == "launch-annual"
    assert (entry.before or {}).get("shown") is True and (entry.after or {}).get("shown") is False


# ---- at most four cards --------------------------------------------------------------------------

async def test_a_fourth_card_fits_and_a_fifth_is_refused(client, monkeypatch):
    token = await _superadmin(client, monkeypatch, slug="pl5", email="boss@pl5.com")
    r = await _publish(client, token, "scale")
    assert r.status_code == 200, r.text
    assert r.json()["shown"] is True                     # free, launch, accelerate, scale
    r = await _publish(client, token, "summit", base_price_cents=39900)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "active" and r.json()["shown"] is False
    assert "price list" in r.json()["note"]
    assert "summit" not in {p["id"] for p in (await _listed(client, token)).json()}
    r = await _show(client, token, "summit", True)
    assert r.status_code == 409 and "4" in r.json()["detail"]


async def test_an_annual_beside_its_monthly_costs_no_card(client, monkeypatch):
    token = await _superadmin(client, monkeypatch, slug="pl6", email="boss@pl6.com")
    assert (await _publish(client, token, "scale")).json()["shown"] is True
    r = await _publish(client, token, "scale-annual", base_price_cents=239000, interval="year")
    assert r.status_code == 200, r.text
    assert r.json()["shown"] is True                     # joins scale's card: still four
    assert len({p["family"] for p in (await _listed(client, token)).json()}) == 4


async def test_hiding_one_makes_room_for_another(client, monkeypatch):
    token = await _superadmin(client, monkeypatch, slug="pl7", email="boss@pl7.com")
    await _publish(client, token, "scale")
    await _publish(client, token, "summit", base_price_cents=39900)
    assert (await _show(client, token, "scale", False)).status_code == 200
    assert (await _show(client, token, "summit", True)).status_code == 200


async def test_splitting_a_pair_cannot_sneak_past_the_limit(client, monkeypatch):
    """Hiding a monthly plan leaves its annual as a card of its own; that still counts."""
    token = await _superadmin(client, monkeypatch, slug="pl8", email="boss@pl8.com")
    await _publish(client, token, "scale")
    assert (await _show(client, token, "launch", False)).status_code == 200   # launch-annual stays
    rows = await _admin_rows(client, token)
    assert rows["launch-annual"]["price_list_family"] == "launch-annual"
    r = await _publish(client, token, "summit", base_price_cents=39900)
    assert r.json()["shown"] is False                                       # still four cards


async def test_a_draft_can_be_hidden_ahead_of_publishing(client, monkeypatch):
    token = await _superadmin(client, monkeypatch, slug="pl9", email="boss@pl9.com")
    await client.post("/api/admin/billing/plans", headers=auth(token), json=_plan())
    assert (await _show(client, token, "scale", False)).status_code == 200
    r = await client.put("/api/admin/billing/plans/scale/status", headers=auth(token),
                         json={"status": "active"})
    assert r.json()["shown"] is False
    assert "scale" not in {p["id"] for p in (await _listed(client, token)).json()}


async def test_only_plans_that_can_be_listed_have_a_switch(client, monkeypatch):
    token = await _superadmin(client, monkeypatch, slug="pl10", email="boss@pl10.com")
    r = await _show(client, token, "enterprise", False)
    assert r.status_code == 400 and "price list" in r.json()["detail"]
    assert (await _show(client, token, "nope", False)).status_code == 404


# ---- who --------------------------------------------------------------------------------------------

async def test_only_a_superadmin_shows_or_hides_plans(client, monkeypatch):
    boss = await _bootstrap_admin(client, monkeypatch, "pl11")
    finance = await _make_role(client, boss, "cfo@pl11.com", "finance", "pl11f")
    assert (await _show(client, finance, "launch", False)).status_code == 403


# ---- the two sides read the same limit ----------------------------------------------------------

def test_the_console_states_the_server_limit():
    import pathlib
    import re

    from nexus.billing.price_list import MAX_CARDS

    src = pathlib.Path("frontend/src/pages/AdminBillingPage.tsx").read_text(encoding="utf-8")
    found = re.search(r"const PRICE_LIST_LIMIT = (\d+);", src)
    assert found and int(found.group(1)) == MAX_CARDS


def test_the_plans_tab_shows_and_hides_with_the_count_in_view():
    import pathlib

    src = pathlib.Path("frontend/src/pages/AdminBillingPage.tsx").read_text(encoding="utf-8")
    assert 'key: "price_list"' in src and "api.setPlanOnPriceList(" in src
    assert "{cardsShown} of {PRICE_LIST_LIMIT}" in src
    assert "Hide one first." in src                       # a full list says why the box is off
    assert "res.note" in src                              # publishing hidden says so


def test_the_customer_list_follows_admin_changes_without_a_reload():
    import pathlib

    src = pathlib.Path("frontend/src/pages/billing/PlanPicker.tsx").read_text(encoding="utf-8")
    assert 'addEventListener("visibilitychange", refresh)' in src
    assert "plans.loading && !plans.data" in src           # a refresh never blanks the cards
    assert "data-count={Math.min(families.length, 5)}" in src
    css = pathlib.Path("frontend/src/pages/billing/PlanPicker.module.css").read_text(
        encoding="utf-8")
    assert '.grid[data-count="4"]' in css


def test_editing_one_interval_names_the_other():
    import pathlib

    src = pathlib.Path("frontend/src/pages/admin/PlanDialog.tsx").read_text(encoding="utf-8")
    assert "plan.counterpart_id" in src and "edit both when the tier" in src
