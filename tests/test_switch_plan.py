"""A workspace paying through Stripe changes plan on the subscription it has (2026-10-12).

Checkout now refuses a workspace that already pays through Stripe, because a second Checkout
made a second subscription and Stripe billed both. This is what that workspace does instead:
`POST /billing/switch-plan`, which changes the price of the subscription it already has.

The terms, chosen so that every credit handed out is matched by a payment:
- the new plan starts TODAY and is charged in full now (`billing_cycle_anchor=now`, no proration);
  the rest of the current period is not refunded, and the credits already held stay;
- it applies only if that payment succeeds (`payment_behavior=pending_if_incomplete`): a decline
  changes nothing, and a bank asking the customer to confirm sends them to Stripe's invoice page;
- the new plan's credits arrive once, keyed by the invoice that paid for them;
- only an `active`, card-paid subscription not already set to end, to a plan on sale in the same
  currency.
"""
from __future__ import annotations

from tests.conftest import auth
from tests.test_billing_checkout_api import _rep_token, _seeded_tenant
from tests.test_one_stripe_subscription import _follow
from tests.test_stripe_invoice_credits import _send, _state


async def _switch(client, token: str, plan_id: str):
    return await client.post("/api/billing/switch-plan", headers=auth(token),
                             json={"plan_id": plan_id})


async def _credits(plan_id: str) -> float:
    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.billing import BillingPlan

    async with get_platform_sessionmaker()() as s:
        return float((await s.get(BillingPlan, plan_id)).included_credits)


# Offline subscriptions carry offline ids: a `sub_` id with no Stripe credential in force is refused,
# because switching it here would change nothing at Stripe.


async def test_a_paying_workspace_switches_plan_now_and_is_credited_once(
        client, monkeypatch, noop_provider):
    token, tid = await _seeded_tenant(client, slug="sw1")
    await _follow(tid, "noop_sub_sw1")
    _, before, _ = await _state(tid)

    r = await _switch(client, token, "accelerate")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "switched" and body["plan_id"] == "accelerate"
    assert body["charged_cents"] > 0

    change = noop_provider.switches[-1]
    assert change["subscription_id"] == "noop_sub_sw1" and change["plan_id"] == "accelerate"
    assert change["idempotency_key"].startswith("switch:noop_sub_sw1:accelerate:")

    sub, after, _ = await _state(tid)
    assert sub.plan_id == "accelerate" and sub.psp_subscription_id == "noop_sub_sw1"
    assert after == before + await _credits("accelerate")

    # Stripe's own word on the change arrives afterwards and grants nothing more.
    await _send(client, monkeypatch, "customer.subscription.updated", {
        "id": "noop_sub_sw1", "object": "subscription", "customer": f"cus_{tid[:6]}",
        "status": "active", "latest_invoice": body["invoice_id"],
        "metadata": {"tenant_id": tid, "plan_id": "accelerate"}})
    assert (await _state(tid))[1] == after


async def test_a_declined_card_changes_nothing(client, noop_provider):
    token, tid = await _seeded_tenant(client, slug="sw2")
    await _follow(tid, "noop_sub_sw2")
    _, before, _ = await _state(tid)
    noop_provider.switch_outcome = "declined"

    r = await _switch(client, token, "accelerate")
    assert r.status_code == 402, r.text
    assert "Nothing changed" in r.json()["detail"]
    sub, after, _ = await _state(tid)
    assert (sub.plan_id, after) == ("launch", before)


async def test_a_bank_asking_to_confirm_sends_the_customer_to_stripe(client, noop_provider):
    token, tid = await _seeded_tenant(client, slug="sw3")
    await _follow(tid, "noop_sub_sw3")
    _, before, _ = await _state(tid)
    noop_provider.switch_outcome = "requires_action"

    r = await _switch(client, token, "accelerate")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "confirm_payment" and r.json()["hosted_invoice_url"]
    sub, after, _ = await _state(tid)
    assert (sub.plan_id, after) == ("launch", before)


async def test_what_cannot_be_switched_says_why(client, noop_provider):
    from nexus.core.db import get_platform_sessionmaker
    from nexus.core.tenancy import apply_rls
    from nexus.models.billing import BillingSubscription

    token, tid = await _seeded_tenant(client, slug="sw4", plan_id="free")
    r = await _switch(client, token, "launch")
    assert r.status_code == 409 and "checkout" in r.json()["detail"].lower()

    await _follow(tid, "noop_sub_sw4")
    assert (await _switch(client, token, "launch")).status_code == 409        # already on it
    assert (await _switch(client, token, "free")).status_code == 409          # not a switch
    assert (await _switch(client, token, "nope")).status_code == 404

    async with get_platform_sessionmaker()() as s:
        await apply_rls(s, tid)
        from sqlalchemy import select

        sub = (await s.scalars(select(BillingSubscription).where(
            BillingSubscription.tenant_id == tid))).first()
        sub.cancel_at_period_end = True
        await s.commit()
    r = await _switch(client, token, "accelerate")
    assert r.status_code == 409 and "set to end" in r.json()["detail"]

    await _follow(tid, "noop_sub_sw4", status="past_due")
    r = await _switch(client, token, "accelerate")
    assert r.status_code == 409 and "overdue" in r.json()["detail"]
    assert noop_provider.switches == []


async def test_a_rep_cannot_switch_the_plan(client, noop_provider):
    _, tid = await _seeded_tenant(client, slug="sw5")
    await _follow(tid, "noop_sub_sw5")
    r = await _switch(client, _rep_token(tid), "accelerate")
    assert r.status_code == 403


async def test_usage_says_the_workspace_switches_in_place(client, noop_provider):
    token, tid = await _seeded_tenant(client, slug="sw6", plan_id="free")
    usage = (await client.get("/api/billing/usage", headers=auth(token))).json()
    assert usage["switch_in_place"] is False
    await _follow(tid, "noop_sub_sw6")
    usage = (await client.get("/api/billing/usage", headers=auth(token))).json()
    assert usage["switch_in_place"] is True


def test_the_price_list_switches_a_paying_workspace_instead_of_checking_out():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "frontend" / "src"
    picker = (root / "pages" / "billing" / "PlanPicker.tsx").read_text(encoding="utf-8")
    api = (root / "lib" / "api.ts").read_text(encoding="utf-8")
    assert "billingSwitchPlan" in picker and "switch_in_place" in picker
    assert "/billing/switch-plan" in api
    # The confirmation states the terms before money moves.
    assert "starts today" in picker and "not refunded" in picker


async def test_a_stripe_subscription_with_no_stripe_credential_is_not_switched(
        client, noop_provider):
    token, tid = await _seeded_tenant(client, slug="sw7")
    await _follow(tid, "sub_live7")
    r = await _switch(client, token, "accelerate")
    assert r.status_code == 409 and "credential" in r.json()["detail"]
    assert noop_provider.switches == []
