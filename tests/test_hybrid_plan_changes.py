"""Hybrid plan changes for a workspace paying through Stripe (owner decisions 2026-10-22).

- UPGRADES stay in the app (`POST /billing/switch-plan`): charged in full now, applied only if paid,
  credits at once.
- DOWNGRADES go to Stripe's billing portal (`POST /billing/downgrade`) and take effect AT PERIOD
  END: Stripe holds the change on a subscription schedule, and the plan moves when the price does.
- A downgrade is a move to the same interval at a lower price, or from yearly to monthly billing.
  A move to Free is the portal's cancel flow. Everything else is an upgrade.
- The plan follows the Stripe PRICE billed, not `metadata.plan_id`: the portal changes the price
  and leaves the metadata as it was.
- An upgrade made inside the portal earns PRORATED credits (the new plan's extra credits times the
  part of the period left), granted only once Stripe's proration invoice is paid, and only once.
- A renewal credits the plan whose price is on the PAID invoice, so an `invoice.paid` arriving
  before `customer.subscription.updated` at a downgrade boundary cannot credit the old plan.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from tests.conftest import auth
from tests.test_billing_checkout_api import _rep_token, _seeded_tenant
from tests.test_one_stripe_subscription import _follow
from tests.test_stripe_invoice_credits import _send, _state

DAY = 86400


def _plan(price: int, interval: str = "month", credits: int = 0, pid: str = "p"):
    return SimpleNamespace(id=pid, base_price_cents=price, interval=interval,
                           included_credits=credits)


# ---- the rule ---------------------------------------------------------------------------------

def test_the_next_phase_is_read_on_stripes_clock_not_ours():
    """Under a test clock Stripe's time runs ahead of ours; the schedule's own current phase says
    which phase is next."""
    from nexus.billing.plan_changes import next_scheduled_phase

    now = int(time.time())
    ahead = now + 400 * DAY                       # Stripe's clock, a year past ours
    schedule = {"status": "active",
                "current_phase": {"start_date": ahead - 30 * DAY, "end_date": ahead},
                "phases": [{"start_date": ahead - 30 * DAY, "items": [{"price": "p_now"}]},
                           {"start_date": ahead, "items": [{"price": "p_next"}]}]}
    assert next_scheduled_phase(schedule, now)["items"][0]["price"] == "p_next"
    released = {**schedule, "status": "released"}
    assert next_scheduled_phase(released, now) is None
    last = {**schedule, "current_phase": {"start_date": ahead, "end_date": ahead + 30 * DAY}}
    assert next_scheduled_phase(last, now) is None


def test_which_way_a_plan_change_goes():
    from nexus.billing.plan_changes import is_downgrade

    launch, accelerate = _plan(9900), _plan(19900)
    launch_year, accelerate_year = _plan(95000, "year"), _plan(191000, "year")
    free = _plan(0)

    assert is_downgrade(accelerate, launch)                 # same interval, lower price
    assert is_downgrade(accelerate_year, launch_year)
    assert is_downgrade(launch_year, launch)                # yearly to monthly: shortening
    assert is_downgrade(accelerate_year, launch)
    assert is_downgrade(launch_year, accelerate)            # shortening, even to a dearer tier
    assert is_downgrade(launch, free)

    assert not is_downgrade(launch, accelerate)
    assert not is_downgrade(launch, launch_year)            # monthly to yearly is an upgrade
    assert not is_downgrade(accelerate, launch_year)
    assert not is_downgrade(launch, _plan(9900))            # same price: not a downgrade
    assert not is_downgrade(None, launch)


# ---- helpers ----------------------------------------------------------------------------------

async def _set_meta(tid: str, **values):
    from sqlalchemy import select

    from nexus.core.db import get_platform_sessionmaker
    from nexus.core.tenancy import apply_rls
    from nexus.models.billing import BillingSubscription

    async with get_platform_sessionmaker()() as s:
        await apply_rls(s, tid)
        sub = (await s.scalars(select(BillingSubscription).where(
            BillingSubscription.tenant_id == tid))).first()
        for key, value in values.items():
            if key == "meta":
                sub.meta = {**(sub.meta or {}), **value}
            else:
                setattr(sub, key, value)
        await s.commit()


async def _price_plans(**prices: str):
    """Record a Stripe price on each plan, as `plan_price_id` would after creating it."""
    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.billing import BillingPlan

    async with get_platform_sessionmaker()() as s:
        for plan_id, price in prices.items():
            plan = await s.get(BillingPlan, plan_id.replace("_", "-"))
            plan.meta = {**(plan.meta or {}), "product_id": f"prod_{price}", "price_id": price,
                         "price_account": "noop", "price_active": True,
                         "price_amount_cents": int(plan.base_price_cents),
                         "price_currency": plan.currency, "price_interval": plan.interval}
        await s.commit()


async def _plan_credits(plan_id: str) -> int:
    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.billing import BillingPlan

    async with get_platform_sessionmaker()() as s:
        return int((await s.get(BillingPlan, plan_id)).included_credits)


def _subscription(tid: str, psp: str, price: str, *, meta_plan: str, start: int, end: int,
                  **extra) -> dict:
    return {"id": psp, "object": "subscription", "customer": f"cus_{tid[:6]}",
            "status": "active", "metadata": {"tenant_id": tid, "plan_id": meta_plan},
            "items": {"object": "list", "data": [
                {"id": "si_1", "price": {"id": price}, "quantity": 1}]},
            "current_period_start": start, "current_period_end": end, **extra}


def _invoice(tid: str, psp: str, invoice_id: str, reason: str, price: str = "", *,
             dahlia: bool = False) -> dict:
    line = ({"pricing": {"price_details": {"price": price}}} if dahlia
            else {"price": {"id": price}}) if price else {}
    return {"id": invoice_id, "object": "invoice", "subscription": psp,
            "customer": f"cus_{tid[:6]}", "status": "paid", "amount_paid": 4200,
            "billing_reason": reason, "metadata": {},
            "lines": {"object": "list", "data": [line] if line else []}}


async def _downgrade(client, token: str, plan_id: str, **body):
    return await client.post("/api/billing/downgrade", headers=auth(token),
                             json={"plan_id": plan_id, **body})


async def _switch(client, token: str, plan_id: str):
    return await client.post("/api/billing/switch-plan", headers=auth(token),
                             json={"plan_id": plan_id})


# ---- switch-plan keeps upgrades, refuses downgrades -------------------------------------------

async def test_the_in_app_switch_refuses_a_downgrade_and_names_the_way(client, noop_provider):
    token, tid = await _seeded_tenant(client, slug="hy1", plan_id="accelerate")
    await _follow(tid, "noop_sub_hy1", plan_id="accelerate")

    r = await _switch(client, token, "launch")
    assert r.status_code == 409, r.text
    assert "Downgrade to Launch" in r.json()["detail"]
    assert "period" in r.json()["detail"]
    r = await _switch(client, token, "free")
    assert r.status_code == 409 and "Downgrade to Free" in r.json()["detail"]
    assert noop_provider.switches == []

    # Yearly to monthly is a downgrade too, even onto a dearer tier.
    await _follow(tid, "noop_sub_hy1", plan_id="launch-annual")
    r = await _switch(client, token, "accelerate")
    assert r.status_code == 409 and "Downgrade to Accelerate" in r.json()["detail"]

    # Monthly to yearly is an upgrade and still switches in place.
    await _follow(tid, "noop_sub_hy1", plan_id="launch")
    assert (await _switch(client, token, "launch-annual")).status_code == 200


# ---- POST /billing/downgrade --------------------------------------------------------------------

async def test_a_downgrade_opens_stripe_on_the_confirm_page_and_changes_nothing_here(
        client, noop_provider):
    token, tid = await _seeded_tenant(client, slug="hy2", plan_id="accelerate")
    await _follow(tid, "noop_sub_hy2", plan_id="accelerate")
    sub_before, credits_before, _ = await _state(tid)

    r = await _downgrade(client, token, "launch", return_url="/settings/billing?moved=1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["flow"] == "schedule_change" and body["plan_id"] == "launch"
    assert body["url"].startswith("https://")

    flow = noop_provider.portal_flows[-1]
    assert flow["kind"] == "subscription_update_confirm"
    assert flow["subscription_id"] == "noop_sub_hy2"
    assert flow["price_id"] == "noop_price_launch"
    assert flow["return_url"].endswith("/settings/billing?moved=1")
    products = {p["product"]: p["prices"] for p in flow["products"]}
    assert products["noop_prod_launch"] == ["noop_price_launch"]
    assert flow["interval"] == "month"

    # A redirect, not a write: Stripe's events move the plan, at period end.
    sub, credits, _ = await _state(tid)
    assert (sub.plan_id, credits) == ("accelerate", credits_before)
    assert noop_provider.switches == []


async def test_the_portal_offers_only_prices_of_the_subscriptions_own_interval(
        client, noop_provider):
    """Stripe's portal applies an interval change AT ONCE, crediting the unused part of the year
    to the customer's balance, whatever `schedule_at_period_end` says (seen live 2026-10-22). So
    the downgrade page must not offer another interval at all."""
    token, tid = await _seeded_tenant(client, slug="hy17", plan_id="accelerate")
    await _follow(tid, "noop_sub_hy17", plan_id="accelerate")
    await _price_plans(launch_annual="price_la17", accelerate_annual="price_aa17",
                       accelerate="price_a17")
    assert (await _downgrade(client, token, "launch")).status_code == 200
    offered = {price for p in noop_provider.portal_flows[-1]["products"] for price in p["prices"]}
    assert "price_a17" in offered
    assert not offered & {"price_la17", "price_aa17"}


async def test_yearly_to_monthly_is_held_by_a_schedule_the_app_makes(client, noop_provider):
    token, tid = await _seeded_tenant(client, slug="hy18", plan_id="launch-annual")
    await _follow(tid, "noop_sub_hy18", plan_id="launch-annual")
    _, credits_before, _ = await _state(tid)

    r = await _downgrade(client, token, "accelerate")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["flow"] == "scheduled" and body["url"] == "" and body["plan_id"] == "accelerate"
    assert noop_provider.portal_flows == []
    held = noop_provider.scheduled_changes[-1]
    assert held["subscription_id"] == "noop_sub_hy18"
    assert held["price_id"] == "noop_price_accelerate"

    sub, credits, _ = await _state(tid)
    assert sub.plan_id == "launch-annual" and credits == credits_before
    assert sub.meta["scheduled_change"]["plan_id"] == "accelerate"
    # Shown at once, and nothing else can be chosen until it is kept or applied.
    usage = (await client.get("/api/billing/usage", headers=auth(token))).json()
    assert usage["scheduled_change"]["plan_name"] == "Accelerate"
    assert (await _switch(client, token, "accelerate-annual")).status_code == 409


async def test_the_stripe_adapter_holds_a_change_until_period_end(monkeypatch):
    from nexus.billing.payments import StripePaymentProvider

    calls: list = []
    now = int(time.time())
    end = now + 300 * DAY

    async def fake_request(self, method, path, *, form=None, idempotency_key=""):
        calls.append((method, path, dict(form or {})))
        if method == "GET" and path == "/subscriptions/sub_1":
            return {"id": "sub_1", "schedule": None, "current_period_end": end,
                    "items": {"data": [{"id": "si_1", "price": {"id": "price_year"},
                                        "quantity": 1}]}}
        if path == "/subscription_schedules":
            return {"id": "sub_sched_9", "phases": [
                {"start_date": now - 65 * DAY, "end_date": end,
                 "items": [{"price": "price_year", "quantity": 1}]}]}
        return {"id": "sub_sched_9", "status": "active"}

    monkeypatch.setattr(StripePaymentProvider, "_request", fake_request)
    out = await StripePaymentProvider("sk_test_hold").schedule_price_change_at_period_end(
        subscription_id="sub_1", price_id="price_month", plan_id="accelerate")
    assert out == {"schedule": "sub_sched_9", "at": end}
    created = next(c for c in calls if c[1] == "/subscription_schedules")
    assert created[2] == {"from_subscription": "sub_1"}
    update = next(c for c in calls if c[1] == "/subscription_schedules/sub_sched_9")[2]
    assert update["phases[0][items][0][price]"] == "price_year"
    assert update["phases[0][start_date]"] == str(now - 65 * DAY)
    assert update["phases[0][end_date]"] == str(end)
    assert update["phases[1][items][0][price]"] == "price_month"
    assert update["phases[1][iterations]"] == "1"
    assert update["end_behavior"] == "release"
    # Naming the plan in phase metadata would make the boundary grant credits twice.
    assert not any("metadata" in key for key in update)


async def test_a_downgrade_to_free_is_the_portal_cancel_flow(client, noop_provider):
    token, tid = await _seeded_tenant(client, slug="hy3")
    await _follow(tid, "noop_sub_hy3")

    r = await _downgrade(client, token, "free")
    assert r.status_code == 200, r.text
    assert r.json()["flow"] == "cancel"
    flow = noop_provider.portal_flows[-1]
    assert flow["kind"] == "subscription_cancel" and flow["subscription_id"] == "noop_sub_hy3"
    assert (await _state(tid))[0].plan_id == "launch"


async def test_the_downgrade_route_refuses_an_upgrade(client, noop_provider):
    token, tid = await _seeded_tenant(client, slug="hy4")
    await _follow(tid, "noop_sub_hy4")
    for target in ("accelerate", "launch-annual"):
        r = await _downgrade(client, token, target)
        assert r.status_code == 409, r.text
        assert "Switch to" in r.json()["detail"]
    assert noop_provider.portal_flows == []


async def test_the_downgrade_route_keeps_the_switch_guards(client, noop_provider):
    token, tid = await _seeded_tenant(client, slug="hy5", plan_id="accelerate")
    r = await _downgrade(client, token, "launch")
    assert r.status_code == 409 and "checkout" in r.json()["detail"].lower()   # not on Stripe

    await _follow(tid, "noop_sub_hy5", plan_id="accelerate")
    assert (await _downgrade(client, token, "nope")).status_code == 404
    assert (await _downgrade(client, token, "accelerate")).status_code == 409   # already on it
    assert (await _downgrade(client, token, "trial")).status_code == 409        # not on sale
    assert (await client.post("/api/billing/downgrade", headers=auth(_rep_token(tid)),
                              json={"plan_id": "launch"})).status_code == 403

    await _set_meta(tid, cancel_at_period_end=True)
    r = await _downgrade(client, token, "launch")
    assert r.status_code == 409 and "set to end" in r.json()["detail"]
    await _set_meta(tid, cancel_at_period_end=False, meta={"collection": "send_invoice"})
    r = await _downgrade(client, token, "launch")
    assert r.status_code == 409 and "invoice" in r.json()["detail"]
    await _set_meta(tid, meta={"collection": ""})
    await _follow(tid, "noop_sub_hy5", plan_id="accelerate", status="past_due")
    r = await _downgrade(client, token, "launch")
    assert r.status_code == 409 and "overdue" in r.json()["detail"]
    assert noop_provider.portal_flows == []


async def test_nothing_is_switched_while_a_downgrade_is_scheduled(client, noop_provider):
    token, tid = await _seeded_tenant(client, slug="hy6", plan_id="accelerate")
    await _follow(tid, "noop_sub_hy6", plan_id="accelerate")
    await _set_meta(tid, meta={"scheduled_change": {
        "plan_id": "launch", "at": "2026-11-22T00:00:00+00:00", "schedule": "sub_sched_6"}})

    r = await _switch(client, token, "accelerate-annual")
    assert r.status_code == 409, r.text
    # Stripe's Manage billing page shows a scheduled change but cannot cancel it (seen live,
    # 2026-10-22), so the sentence names the app's own Keep button.
    assert "scheduled" in r.json()["detail"] and "Keep Accelerate" in r.json()["detail"]
    r = await _downgrade(client, token, "launch")
    assert r.status_code == 409 and "scheduled" in r.json()["detail"]
    assert noop_provider.switches == [] and noop_provider.portal_flows == []


async def _keep(client, token: str):
    return await client.post("/api/billing/scheduled-change/cancel", headers=auth(token), json={})


async def test_keep_my_plan_cancels_the_scheduled_downgrade_at_stripe(client, noop_provider):
    token, tid = await _seeded_tenant(client, slug="hy16", plan_id="accelerate")
    await _follow(tid, "noop_sub_hy16", plan_id="accelerate")
    r = await _keep(client, token)
    assert r.status_code == 409 and "Nothing is scheduled" in r.json()["detail"]

    await _set_meta(tid, meta={"scheduled_change": {
        "plan_id": "launch", "at": "2026-11-22T00:00:00+00:00", "schedule": "sub_sched_16"}})
    assert (await client.post("/api/billing/scheduled-change/cancel",
                              headers=auth(_rep_token(tid)), json={})).status_code == 403
    r = await _keep(client, token)
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "kept", "plan_id": "accelerate"}
    assert noop_provider.released == ["noop_sub_hy16"]
    sub, _, _ = await _state(tid)
    assert sub.plan_id == "accelerate" and "scheduled_change" not in sub.meta

    # And the switch is open again.
    assert (await _switch(client, token, "accelerate-annual")).status_code == 200


async def test_the_stripe_adapter_releases_the_schedule_and_only_that(monkeypatch):
    from nexus.billing.payments import StripePaymentProvider

    calls: list = []
    monkeypatch.setattr(StripePaymentProvider, "_request",
                        _scheduled_stripe(calls, next_phase_in=5 * DAY))
    out = await StripePaymentProvider("sk_test_keep").release_subscription_schedule(
        subscription_id="sub_1")
    assert out == {"released": True, "schedule": "sub_sched_1"}
    assert [c for c in calls if c[0] == "POST"] == [
        ("POST", "/subscription_schedules/sub_sched_1/release")]


def _scheduled_stripe(calls: list, *, next_phase_in: int):
    now = int(time.time())

    async def fake_request(self, method, path, *, form=None, idempotency_key=""):
        calls.append((method, path))
        if path == "/subscriptions/sub_1" and method == "GET":
            return {"id": "sub_1", "schedule": "sub_sched_1",
                    "items": {"data": [{"id": "si_1", "price": {"id": "price_a"}}]}}
        if path == "/subscription_schedules/sub_sched_1":
            return {"id": "sub_sched_1", "status": "active", "phases": [
                {"start_date": now - 10 * DAY, "items": [{"price": "price_a"}]},
                {"start_date": now + next_phase_in, "items": [{"price": "price_c"}]}]}
        return {"id": "sub_1", "current_period_start": now, "current_period_end": now + DAY,
                "latest_invoice": {"id": "in_1", "payment_intent": {"status": "succeeded"}}}
    return fake_request


async def test_the_stripe_adapter_refuses_to_switch_while_a_downgrade_is_held(monkeypatch):
    from nexus.billing.payments import ScheduledChangePending, StripePaymentProvider

    calls: list = []
    monkeypatch.setattr(StripePaymentProvider, "_request",
                        _scheduled_stripe(calls, next_phase_in=5 * DAY))
    with pytest.raises(ScheduledChangePending):
        await StripePaymentProvider("sk_test_sched").switch_subscription_now(
            subscription_id="sub_1", price_id="price_b", plan_id="b", idempotency_key="k")
    assert ("POST", "/subscriptions/sub_1") not in calls


async def test_a_schedule_with_nothing_left_to_change_is_released_before_a_switch(monkeypatch):
    """Once the downgrade has landed the schedule stays attached for its last phase; it must not
    block an upgrade for a whole period."""
    from nexus.billing.payments import StripePaymentProvider

    calls: list = []
    monkeypatch.setattr(StripePaymentProvider, "_request",
                        _scheduled_stripe(calls, next_phase_in=-5 * DAY))
    out = await StripePaymentProvider("sk_test_released").switch_subscription_now(
        subscription_id="sub_1", price_id="price_b", plan_id="b", idempotency_key="k")
    assert out["applied"] is True
    posts = [c[1] for c in calls if c[0] == "POST"]
    assert posts == ["/subscription_schedules/sub_sched_1/release", "/subscriptions/sub_1"]


# ---- the webhook follows the billed price -------------------------------------------------------

async def test_a_downgrade_landing_at_period_end_moves_the_plan_without_granting(
        client, monkeypatch, noop_provider):
    _, tid = await _seeded_tenant(client, slug="hy7", plan_id="accelerate")
    await _follow(tid, "sub_hy7", plan_id="accelerate")
    await _price_plans(launch="price_launch7", accelerate="price_accel7")
    await _set_meta(tid, meta={"scheduled_change": {"plan_id": "launch", "at": "x",
                                                    "schedule": "sub_sched_7"}})
    _, before, _ = await _state(tid)

    now = int(time.time())
    # The portal leaves `metadata.plan_id` on the old plan; the price is what Stripe bills.
    await _send(client, monkeypatch, "customer.subscription.updated", _subscription(
        tid, "sub_hy7", "price_launch7", meta_plan="accelerate", start=now, end=now + 30 * DAY))
    sub, after, _ = await _state(tid)
    assert sub.plan_id == "launch" and sub.interval == "month"
    assert after == before                    # the renewal invoice credits the new period
    assert "scheduled_change" not in (sub.meta or {})
    assert sub.meta.get("changed_via") == "stripe_portal"


async def test_a_portal_upgrade_is_credited_pro_rata_once_its_invoice_is_paid(
        client, monkeypatch, noop_provider):
    _, tid = await _seeded_tenant(client, slug="hy8")
    await _follow(tid, "sub_hy8")
    await _price_plans(launch="price_launch8", accelerate="price_accel8")
    _, before, _ = await _state(tid)

    now = int(time.time())
    await _send(client, monkeypatch, "customer.subscription.updated", _subscription(
        tid, "sub_hy8", "price_accel8", meta_plan="launch", start=now - 15 * DAY,
        end=now + 15 * DAY, latest_invoice="in_up8"))
    sub, held, _ = await _state(tid)
    assert sub.plan_id == "accelerate"
    assert held == before                     # nothing until the proration invoice is paid
    pending = sub.meta["portal_upgrade_pending"]
    extra = await _plan_credits("accelerate") - await _plan_credits("launch")
    assert pending["invoice"] == "in_up8" and abs(pending["credits"] - extra / 2) <= extra * 0.01

    paid = _invoice(tid, "sub_hy8", "in_up8", "subscription_update", "price_accel8")
    await _send(client, monkeypatch, "invoice.paid", paid)
    sub, after, _ = await _state(tid)
    assert after == before + pending["credits"]
    assert "portal_upgrade_pending" not in sub.meta

    await _send(client, monkeypatch, "invoice.paid", paid)              # redelivered
    await _send(client, monkeypatch, "customer.subscription.updated", _subscription(
        tid, "sub_hy8", "price_accel8", meta_plan="launch", start=now - 15 * DAY,
        end=now + 15 * DAY, latest_invoice="in_up8"))
    assert (await _state(tid))[1] == after                              # once only


async def test_a_portal_upgrade_paid_before_its_subscription_event_is_credited_then(
        client, monkeypatch, noop_provider):
    _, tid = await _seeded_tenant(client, slug="hy9")
    await _follow(tid, "sub_hy9")
    await _price_plans(launch="price_launch9", accelerate="price_accel9")
    _, before, _ = await _state(tid)

    paid = _invoice(tid, "sub_hy9", "in_up9", "subscription_update", "price_accel9")
    await _send(client, monkeypatch, "invoice.paid", paid)
    assert (await _state(tid))[1] == before

    now = int(time.time())
    await _send(client, monkeypatch, "customer.subscription.updated", _subscription(
        tid, "sub_hy9", "price_accel9", meta_plan="launch", start=now - 10 * DAY,
        end=now + 20 * DAY, latest_invoice="in_up9"))
    sub, after, _ = await _state(tid)
    assert sub.plan_id == "accelerate" and after > before
    assert "portal_upgrade_pending" not in sub.meta

    await _send(client, monkeypatch, "invoice.paid", paid)
    assert (await _state(tid))[1] == after


async def test_a_change_made_in_the_app_is_not_read_as_a_portal_change(
        client, monkeypatch, noop_provider):
    """`switch-plan` and the console move our row and write `metadata.plan_id`; their echo from
    Stripe changes nothing and grants nothing."""
    _, tid = await _seeded_tenant(client, slug="hy10", plan_id="accelerate")
    await _follow(tid, "sub_hy10", plan_id="accelerate")
    await _price_plans(accelerate="price_accel10")
    _, before, _ = await _state(tid)
    now = int(time.time())
    await _send(client, monkeypatch, "customer.subscription.updated", _subscription(
        tid, "sub_hy10", "price_accel10", meta_plan="accelerate", start=now, end=now + 30 * DAY))
    sub, after, _ = await _state(tid)
    assert (sub.plan_id, after) == ("accelerate", before)
    assert "portal_upgrade_pending" not in (sub.meta or {})


async def test_a_scheduled_downgrade_is_shown_and_cleared_when_released(
        client, monkeypatch, noop_provider):
    token, tid = await _seeded_tenant(client, slug="hy11", plan_id="accelerate")
    await _follow(tid, "sub_hy11", plan_id="accelerate")
    await _price_plans(launch="price_launch11", accelerate="price_accel11")
    now = int(time.time())
    end = now + 20 * DAY
    noop_provider.schedules["sub_sched_11"] = {
        "id": "sub_sched_11", "object": "subscription_schedule", "status": "active",
        "subscription": "sub_hy11", "current_phase": {"start_date": now - 10 * DAY,
                                                      "end_date": end},
        "phases": [
            {"start_date": now - 10 * DAY, "end_date": end,
             "items": [{"price": "price_accel11", "quantity": 1}]},
            {"start_date": end, "end_date": end + 30 * DAY,
             "items": [{"price": "price_launch11", "quantity": 1}]},
        ]}

    await _send(client, monkeypatch, "customer.subscription.updated", _subscription(
        tid, "sub_hy11", "price_accel11", meta_plan="accelerate", start=now - 10 * DAY, end=end,
        schedule="sub_sched_11"))
    sub, _, _ = await _state(tid)
    assert sub.plan_id == "accelerate"
    change = sub.meta["scheduled_change"]
    assert change["plan_id"] == "launch" and change["schedule"] == "sub_sched_11"

    usage = (await client.get("/api/billing/usage", headers=auth(token))).json()
    assert usage["scheduled_change"]["plan_id"] == "launch"
    assert usage["scheduled_change"]["plan_name"] == "Launch"
    assert usage["scheduled_change"]["at"].startswith(
        time.strftime("%Y-%m-%d", time.gmtime(end)))

    # Released in Stripe: the subscription no longer carries the schedule.
    await _send(client, monkeypatch, "customer.subscription.updated", _subscription(
        tid, "sub_hy11", "price_accel11", meta_plan="accelerate", start=now - 10 * DAY, end=end,
        schedule=None))
    sub, _, _ = await _state(tid)
    assert "scheduled_change" not in sub.meta
    usage = (await client.get("/api/billing/usage", headers=auth(token))).json()
    assert usage["scheduled_change"] is None


@pytest.mark.parametrize("dahlia", [False, True])
async def test_a_renewal_credits_the_plan_on_the_paid_invoice(
        client, monkeypatch, noop_provider, dahlia):
    """At a downgrade boundary Stripe can deliver the renewal's `invoice.paid` before the
    `customer.subscription.updated` that moves the price. The invoice names the price it billed."""
    _, tid = await _seeded_tenant(client, slug=f"hy12{int(dahlia)}", plan_id="accelerate")
    await _follow(tid, "sub_hy12", plan_id="accelerate")
    await _price_plans(launch="price_launch12", accelerate="price_accel12")
    _, before, _ = await _state(tid)

    await _send(client, monkeypatch, "invoice.paid", _invoice(
        tid, "sub_hy12", "in_cycle12", "subscription_cycle", "price_launch12", dahlia=dahlia))
    assert (await _state(tid))[1] == before + await _plan_credits("launch")


def _boundary_invoice(tid: str, psp: str, invoice_id: str, price: str, start: int, *,
                      proration: bool = False) -> dict:
    """The invoice Stripe raises when a schedule's next phase starts (measured 2026-10-22): its
    billing reason is `subscription_update`, not `subscription_cycle`."""
    return {"id": invoice_id, "object": "invoice", "subscription": psp,
            "customer": f"cus_{tid[:6]}", "status": "paid", "amount_paid": 19900,
            "billing_reason": "subscription_update", "metadata": {},
            "lines": {"object": "list", "data": [
                {"price": {"id": price}, "proration": proration,
                 "period": {"start": start, "end": start + 30 * DAY}}]}}


async def test_the_first_invoice_of_a_landed_downgrade_credits_the_new_plan_once(
        client, monkeypatch, noop_provider):
    _, tid = await _seeded_tenant(client, slug="hy19", plan_id="launch-annual")
    await _follow(tid, "sub_hy19", plan_id="launch-annual")
    await _price_plans(launch_annual="price_la19", accelerate="price_a19")
    _, before, _ = await _state(tid)
    now = int(time.time())

    await _send(client, monkeypatch, "customer.subscription.updated", _subscription(
        tid, "sub_hy19", "price_a19", meta_plan="launch-annual", start=now, end=now + 30 * DAY))
    sub, held, _ = await _state(tid)
    assert sub.plan_id == "accelerate" and held == before

    paid = _boundary_invoice(tid, "sub_hy19", "in_b19", "price_a19", now)
    await _send(client, monkeypatch, "invoice.paid", paid)
    sub, after, _ = await _state(tid)
    assert after == before + await _plan_credits("accelerate")
    assert "landed_change" not in sub.meta
    await _send(client, monkeypatch, "invoice.paid", paid)
    assert (await _state(tid))[1] == after


async def test_the_first_invoice_arriving_before_the_price_moves_credits_the_new_plan(
        client, monkeypatch, noop_provider):
    from datetime import datetime, timezone

    _, tid = await _seeded_tenant(client, slug="hy20", plan_id="launch-annual")
    await _follow(tid, "sub_hy20", plan_id="launch-annual")
    await _price_plans(launch_annual="price_la20", accelerate="price_a20")
    now = int(time.time())
    await _set_meta(tid, meta={"scheduled_change": {
        "plan_id": "accelerate", "schedule": "sub_sched_20",
        "at": datetime.fromtimestamp(now, tz=timezone.utc).isoformat()}})
    _, before, _ = await _state(tid)

    await _send(client, monkeypatch, "invoice.paid",
                _boundary_invoice(tid, "sub_hy20", "in_b20", "price_a20", now))
    assert (await _state(tid))[1] == before + await _plan_credits("accelerate")
    await _send(client, monkeypatch, "customer.subscription.updated", _subscription(
        tid, "sub_hy20", "price_a20", meta_plan="launch-annual", start=now, end=now + 30 * DAY))
    sub, after, _ = await _state(tid)
    assert sub.plan_id == "accelerate"
    assert after == before + await _plan_credits("accelerate")        # nothing more


async def test_other_plan_change_invoices_are_not_read_as_a_landed_downgrade(
        client, monkeypatch, noop_provider):
    """An in-app switch's invoice and a proration invoice are `subscription_update` too; neither
    starts the period of a downgrade we saw scheduled, so neither is credited as one."""
    _, tid = await _seeded_tenant(client, slug="hy21")
    await _follow(tid, "sub_hy21")
    await _price_plans(launch="price_l21", accelerate="price_a21")
    _, before, _ = await _state(tid)
    now = int(time.time())
    await _send(client, monkeypatch, "invoice.paid",
                _boundary_invoice(tid, "sub_hy21", "in_s21", "price_a21", now))
    await _send(client, monkeypatch, "invoice.paid",
                _boundary_invoice(tid, "sub_hy21", "in_p21", "price_l21", now, proration=True))
    assert (await _state(tid))[1] == before


async def test_a_renewal_without_a_known_price_still_credits_the_plan_held(
        client, monkeypatch, noop_provider):
    _, tid = await _seeded_tenant(client, slug="hy13", plan_id="accelerate")
    await _follow(tid, "sub_hy13", plan_id="accelerate")
    _, before, _ = await _state(tid)
    await _send(client, monkeypatch, "invoice.paid", _invoice(
        tid, "sub_hy13", "in_cycle13", "subscription_cycle", "price_unknown"))
    assert (await _state(tid))[1] == before + await _plan_credits("accelerate")


async def test_reconciliation_reads_the_plan_from_the_billed_price(client, noop_provider):
    from nexus.billing.reconcile import compare_subscription
    from nexus.workers.tasks import tenant_session

    _, tid = await _seeded_tenant(client, slug="hy14")
    await _follow(tid, "noop_sub_hy14")
    await _price_plans(launch="price_launch14")
    noop_provider.subscriptions["noop_sub_hy14"] = _subscription(
        tid, "noop_sub_hy14", "price_launch14", meta_plan="accelerate", start=0, end=0)
    async with tenant_session(tid) as ts:
        sub, *_ = await _state(tid)
        report = await compare_subscription(ts, sub)
    assert "plan_id" not in report["drift"], report


# ---- the Stripe adapter ---------------------------------------------------------------------------

def _fake_stripe(calls: list, *, listed=None, subscription=None):
    async def fake_request(self, method, path, *, form=None, idempotency_key=""):
        calls.append((method, path, dict(form or {})))
        if method == "GET" and path.startswith("/billing_portal/configurations"):
            return {"data": list(listed or [])}
        if method == "GET" and path.startswith("/subscriptions/"):
            return subscription or {"id": "sub_1", "items": {"data": [
                {"id": "si_1", "price": {"id": "price_accel"}, "quantity": 1}]}}
        if path == "/billing_portal/configurations":
            return {"id": "bpc_new"}
        if path.startswith("/billing_portal/configurations/"):
            return {"id": path.rsplit("/", 1)[1]}
        if path.startswith("/subscription_schedules/"):
            return {"id": path.rsplit("/", 1)[1], "phases": []}
        return {"id": "bps_1", "url": "https://billing.stripe.com/p/session/bps_1"}
    return fake_request


PRODUCTS = [{"product": "prod_launch", "prices": ["price_launch"]},
            {"product": "prod_accel", "prices": ["price_accel"]}]


async def test_a_downgrade_opens_the_portal_with_its_own_configuration(monkeypatch):
    from nexus.billing import payments
    from nexus.billing.payments import DOWNGRADE_CONFIG_TAG, StripePaymentProvider

    payments._portal_configurations.clear()
    payments._downgrade_configurations.clear()
    calls: list = []
    monkeypatch.setattr(StripePaymentProvider, "_request", _fake_stripe(calls))
    p = StripePaymentProvider("sk_test_downgrade")

    out = await p.create_plan_change_portal_session(
        customer_id="cus_1", subscription_id="sub_1", price_id="price_launch",
        products=PRODUCTS, return_url="https://app.example/settings/billing")
    assert out["url"].startswith("https://billing.stripe.com/")

    config = next(c[2] for c in calls if c[:2] == ("POST", "/billing_portal/configurations"))
    # One configuration per interval: each offers only its own interval's prices.
    assert config["metadata[nexus]"] == f"{DOWNGRADE_CONFIG_TAG}_month"
    assert config["features[subscription_update][enabled]"] == "true"
    assert config["features[subscription_update][default_allowed_updates][0]"] == "price"
    assert config["features[subscription_update][proration_behavior]"] == "always_invoice"
    conditions = {v for k, v in config.items() if "schedule_at_period_end" in k}
    assert conditions == {"decreasing_item_amount", "shortening_interval"}
    assert config["features[subscription_update][products][0][product]"] == "prod_launch"
    assert config["features[subscription_update][products][0][prices][0]"] == "price_launch"
    assert config["features[subscription_update][products][1][product]"] == "prod_accel"
    assert config["features[subscription_cancel][mode]"] == "at_period_end"

    session = next(c[2] for c in calls if c[1] == "/billing_portal/sessions")
    assert session["configuration"] == "bpc_new" and session["customer"] == "cus_1"
    assert session["flow_data[type]"] == "subscription_update_confirm"
    assert session["flow_data[subscription_update_confirm][subscription]"] == "sub_1"
    assert session["flow_data[subscription_update_confirm][items][0][id]"] == "si_1"
    assert session["flow_data[subscription_update_confirm][items][0][price]"] == "price_launch"
    assert session["flow_data[after_completion][type]"] == "redirect"
    assert (session["flow_data[after_completion][redirect][return_url]"]
            == "https://app.example/settings/billing")

    # The same price list reuses the configuration; a changed one updates it in place.
    await p.create_plan_change_portal_session(
        customer_id="cus_1", subscription_id="sub_1", price_id="price_launch",
        products=PRODUCTS, return_url="")
    assert sum(1 for c in calls if c[:2] == ("POST", "/billing_portal/configurations")) == 1
    await p.create_plan_change_portal_session(
        customer_id="cus_1", subscription_id="sub_1", price_id="price_launch",
        products=PRODUCTS[:1], return_url="")
    updates = [c for c in calls if c[0] == "POST"
               and c[1] == "/billing_portal/configurations/bpc_new"]
    assert len(updates) == 1
    assert "features[subscription_update][products][1][product]" not in updates[0][2]


async def test_an_existing_downgrade_configuration_is_found_and_kept_current(monkeypatch):
    from nexus.billing import payments
    from nexus.billing.payments import DOWNGRADE_CONFIG_TAG, StripePaymentProvider

    payments._downgrade_configurations.clear()
    calls: list = []
    listed = [{"id": "bpc_ours", "metadata": {"nexus": f"{DOWNGRADE_CONFIG_TAG}_month"},
               "features": {
        "subscription_update": {"enabled": True, "products": [
            {"product": "prod_launch", "prices": ["price_launch"]}]}}}]
    monkeypatch.setattr(StripePaymentProvider, "_request", _fake_stripe(calls, listed=listed))
    await StripePaymentProvider("sk_test_found").create_plan_change_portal_session(
        customer_id="cus_1", subscription_id="sub_1", price_id="price_launch",
        products=PRODUCTS, return_url="")
    assert ("POST", "/billing_portal/configurations") not in [c[:2] for c in calls]
    assert [c[1] for c in calls if c[0] == "POST"][0] == "/billing_portal/configurations/bpc_ours"


async def test_a_subscription_with_several_items_is_not_sent_to_the_portal(monkeypatch):
    from nexus.billing import payments
    from nexus.billing.payments import PaymentError, StripePaymentProvider

    payments._downgrade_configurations.clear()
    calls: list = []
    two = {"id": "sub_1", "items": {"data": [{"id": "si_1"}, {"id": "si_2"}]}}
    monkeypatch.setattr(StripePaymentProvider, "_request",
                        _fake_stripe(calls, subscription=two))
    with pytest.raises(PaymentError):
        await StripePaymentProvider("sk_test_two").create_plan_change_portal_session(
            customer_id="cus_1", subscription_id="sub_1", price_id="price_launch",
            products=PRODUCTS, return_url="")
    assert not [c for c in calls if c[1] == "/billing_portal/sessions"]


async def test_the_free_downgrade_cancels_through_the_no_switch_configuration(monkeypatch):
    from nexus.billing import payments
    from nexus.billing.payments import StripePaymentProvider

    payments._portal_configurations.clear()
    calls: list = []
    monkeypatch.setattr(StripePaymentProvider, "_request", _fake_stripe(calls))
    await StripePaymentProvider("sk_test_cancel").create_cancel_portal_session(
        customer_id="cus_1", subscription_id="sub_1", return_url="https://app.example/b")

    config = next(c[2] for c in calls if c[:2] == ("POST", "/billing_portal/configurations"))
    assert config["features[subscription_update][enabled]"] == "false"
    session = next(c[2] for c in calls if c[1] == "/billing_portal/sessions")
    assert session["configuration"] == "bpc_new"
    assert session["flow_data[type]"] == "subscription_cancel"
    assert session["flow_data[subscription_cancel][subscription]"] == "sub_1"
    assert session["flow_data[after_completion][type]"] == "redirect"


async def test_a_schedule_is_read_back_from_stripe(monkeypatch):
    from nexus.billing.payments import StripePaymentProvider

    calls: list = []
    monkeypatch.setattr(StripePaymentProvider, "_request", _fake_stripe(calls))
    out = await StripePaymentProvider("sk_test_read").get_subscription_schedule("sub_sched_1")
    assert out["id"] == "sub_sched_1"
    assert calls == [("GET", "/subscription_schedules/sub_sched_1", {})]


# ---- the price list ----------------------------------------------------------------------------

async def test_the_price_list_marks_downgrades(client, noop_provider):
    token, tid = await _seeded_tenant(client, slug="hy15", plan_id="accelerate")
    await _follow(tid, "noop_sub_hy15", plan_id="accelerate")
    plans = {p["id"]: p for p in (await client.get("/api/billing/plans",
                                                   headers=auth(token))).json()}
    assert plans["launch"]["downgrade"] is True and plans["free"]["downgrade"] is True
    assert plans["accelerate-annual"]["downgrade"] is False
    assert plans["accelerate"]["downgrade"] is False                     # the current plan


def test_the_price_list_sends_downgrades_to_stripe():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "frontend" / "src"
    picker = (root / "pages" / "billing" / "PlanPicker.tsx").read_text(encoding="utf-8")
    api = (root / "lib" / "api.ts").read_text(encoding="utf-8")
    types = (root / "lib" / "types.ts").read_text(encoding="utf-8")
    assert "/billing/downgrade" in api and "billingDowngrade" in picker
    assert "/billing/scheduled-change/cancel" in api and "billingKeepPlan" in picker
    assert "Downgrade to" in picker and "plan.downgrade" in picker
    # Yearly to monthly is confirmed here, because no Stripe page confirms it.
    assert '"scheduled"' in picker and "DowngradeDialog" in picker
    assert "scheduled_change" in picker and "Moves to" in picker
    assert "scheduled_change" in types and "downgrade: boolean" in types
    # The confirmation says when it takes effect before the customer leaves for Stripe.
    assert "period ends" in picker
