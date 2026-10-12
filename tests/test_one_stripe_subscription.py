"""A workspace pays for ONE Stripe subscription, and only one that has actually started (2026-10-12).

Found auditing the payment paths for production. A workspace already paying for Launch could press
"Continue to checkout" on Accelerate: Checkout created a SECOND subscription, our row moved to it,
and Stripe went on billing both. Events about the old one still reached the row through
`metadata.tenant_id`, flipping the plan back, and its eventual `deleted` cancelled a workspace that
was still paying.

The rules pinned here:
- Checkout refuses a workspace that already pays through Stripe, with a sentence.
- Opening Checkout again expires the session opened before, so two tabs cannot both be paid.
- The return addresses must be on this app (no open redirect off a Stripe page).
- A Stripe subscription moves our row only once it has STARTED: Checkout paid, an asynchronous
  payment succeeded, or the subscription is active or trialing. Before that nothing changes, so an
  unpaid bank debit or an abandoned attempt can neither grant a plan nor cancel the free one.
- A started subscription that replaces a live one ends the old one at Stripe (a job, retried), and
  the old one's later events are ignored.
"""
from __future__ import annotations

import time

import pytest

from tests.conftest import auth
from tests.test_billing_checkout_api import _seeded_tenant
from tests.test_stripe_invoice_credits import _send, _state


async def _follow(tid: str, psp: str, *, plan_id: str = "launch", status: str = "active"):
    """Make the workspace's subscription follow the Stripe subscription ``psp``."""
    from sqlalchemy import select

    from nexus.core.db import get_platform_sessionmaker
    from nexus.core.tenancy import apply_rls
    from nexus.models.billing import BillingSubscription

    async with get_platform_sessionmaker()() as s:
        await apply_rls(s, tid)
        sub = (await s.scalars(select(BillingSubscription).where(
            BillingSubscription.tenant_id == tid))).first()
        sub.plan_id, sub.status = plan_id, status
        sub.psp_subscription_id, sub.psp_customer_id = psp, f"cus_{tid[:6]}"
        await s.commit()


def _sub(tid: str, psp: str, status: str, plan_id: str = "launch") -> dict:
    now = int(time.time())
    return {"id": psp, "object": "subscription", "customer": f"cus_{tid[:6]}", "status": status,
            "metadata": {"tenant_id": tid, "plan_id": plan_id},
            "current_period_start": now, "current_period_end": now + 30 * 86400}


def _session(tid: str, psp: str, payment_status: str, plan_id: str = "launch") -> dict:
    return {"id": f"cs_{psp}", "object": "checkout.session", "customer": f"cus_{tid[:6]}",
            "subscription": psp, "payment_status": payment_status, "mode": "subscription",
            "metadata": {"tenant_id": tid, "plan_id": plan_id}}


# ---- checkout ---------------------------------------------------------------------------------

async def test_a_workspace_paying_through_stripe_cannot_open_a_second_checkout(
        client, noop_provider):
    token, tid = await _seeded_tenant(client, slug="one1")
    await _follow(tid, "sub_live1")
    r = await client.post("/api/billing/checkout", headers=auth(token),
                          json={"plan_id": "accelerate"})
    assert r.status_code == 409, r.text
    assert "twice" in r.json()["detail"] and "Nothing was charged" in r.json()["detail"]
    assert noop_provider.checkout_sessions == []


async def test_a_workspace_whose_stripe_subscription_ended_can_buy_again(client, noop_provider):
    token, tid = await _seeded_tenant(client, slug="one2", plan_id="free")
    await _follow(tid, "sub_ended2", status="canceled")
    r = await client.post("/api/billing/checkout", headers=auth(token),
                          json={"plan_id": "launch"})
    assert r.status_code == 200, r.text


async def test_opening_checkout_again_expires_the_session_opened_before(client, noop_provider):
    token, _ = await _seeded_tenant(client, slug="one3", plan_id="free")
    first = await client.post("/api/billing/checkout", headers=auth(token),
                              json={"plan_id": "launch"})
    second = await client.post("/api/billing/checkout", headers=auth(token),
                               json={"plan_id": "accelerate"})
    assert first.status_code == 200 and second.status_code == 200, second.text
    assert noop_provider.expired_sessions == [first.json()["id"]]


async def test_a_session_that_already_completed_refuses_a_new_checkout(client, noop_provider):
    token, _ = await _seeded_tenant(client, slug="one4", plan_id="free")
    first = await client.post("/api/billing/checkout", headers=auth(token),
                              json={"plan_id": "launch"})
    noop_provider.completed_sessions.add(first.json()["id"])
    second = await client.post("/api/billing/checkout", headers=auth(token),
                               json={"plan_id": "launch"})
    assert second.status_code == 409, second.text
    assert "just completed" in second.json()["detail"]
    assert len(noop_provider.checkout_sessions) == 1


@pytest.fixture
def app_base(monkeypatch):
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "app_base_url", "https://gtm.example.com")


async def test_checkout_returns_only_to_this_app(client, noop_provider, app_base):
    token, _ = await _seeded_tenant(client, slug="one5", plan_id="free")
    bad = await client.post("/api/billing/checkout", headers=auth(token),
                            json={"plan_id": "launch", "success_url": "https://evil.example/x"})
    assert bad.status_code == 422, bad.text
    assert noop_provider.checkout_sessions == []

    ok = await client.post("/api/billing/checkout", headers=auth(token),
                           json={"plan_id": "launch", "success_url": "/settings/billing?ok=1",
                                 "cancel_url": "https://gtm.example.com/settings/billing"})
    assert ok.status_code == 200, ok.text
    sent = noop_provider.checkout_sessions[-1]
    assert sent["success_url"] == "https://gtm.example.com/settings/billing?ok=1"
    assert sent["cancel_url"] == "https://gtm.example.com/settings/billing"

    sneaky = await client.post("/api/billing/checkout", headers=auth(token),
                               json={"plan_id": "launch", "cancel_url": "//evil.example/x"})
    assert sneaky.status_code == 422, sneaky.text


async def test_the_portal_returns_only_to_this_app(client, noop_provider, app_base):
    token, tid = await _seeded_tenant(client, slug="one6")
    await _follow(tid, "sub_live6")
    r = await client.post("/api/billing/portal", headers=auth(token),
                          json={"return_url": "https://evil.example/x"})
    assert r.status_code == 422, r.text
    assert noop_provider.portal_sessions == []


# ---- the webhook follows one subscription, once it has started ---------------------------------

async def test_a_started_subscription_replacing_a_live_one_ends_the_old_one(
        client, monkeypatch, noop_provider):
    from nexus.workers.tasks import handle_end_replaced_subscription

    _, tid = await _seeded_tenant(client, slug="one7")
    await _follow(tid, "sub_old7", plan_id="launch")
    queued: list[dict] = []

    async def queue(payload):
        queued.append(payload)

    monkeypatch.setattr("nexus.billing.webhooks._queue_end_of_replaced", queue)

    await _send(client, monkeypatch, "checkout.session.completed",
                _session(tid, "sub_new7", "paid", plan_id="accelerate"))
    sub, _, _ = await _state(tid)
    assert sub.psp_subscription_id == "sub_new7" and sub.plan_id == "accelerate"
    assert any(p["id"] == "sub_old7" and p["reason"] == "replaced"
               for p in sub.meta["previous_psp_subscriptions"])
    assert queued == [{"tenant_id": tid, "subscription_id": "sub_old7",
                       "replaced_by": "sub_new7"}]

    # Ended only once the replacement is confirmed running at Stripe.
    assert (await handle_end_replaced_subscription(queued[0]))["skipped"]
    assert noop_provider.cancellations == []
    noop_provider.subscriptions["sub_new7"] = {"id": "sub_new7", "status": "active"}
    out = await handle_end_replaced_subscription(queued[0])
    assert out["ended"] == "sub_old7"
    assert {"id": "sub_old7", "at_period_end": False} in noop_provider.cancellations

    # The old one's later events change nothing: no flip back, no cancellation.
    await _send(client, monkeypatch, "customer.subscription.updated",
                _sub(tid, "sub_old7", "active", plan_id="launch"))
    await _send(client, monkeypatch, "customer.subscription.deleted",
                _sub(tid, "sub_old7", "canceled", plan_id="launch"))
    sub, _, _ = await _state(tid)
    assert (sub.psp_subscription_id, sub.plan_id, sub.status) == ("sub_new7", "accelerate",
                                                                  "active")


async def test_a_subscription_that_has_not_started_cannot_touch_a_live_one(
        client, monkeypatch, noop_provider):
    _, tid = await _seeded_tenant(client, slug="one8")
    await _follow(tid, "sub_old8", plan_id="launch")
    queued: list[dict] = []

    async def queue(payload):
        queued.append(payload)

    monkeypatch.setattr("nexus.billing.webhooks._queue_end_of_replaced", queue)

    await _send(client, monkeypatch, "customer.subscription.created",
                _sub(tid, "sub_try8", "incomplete", plan_id="accelerate"))
    await _send(client, monkeypatch, "customer.subscription.updated",
                _sub(tid, "sub_try8", "incomplete_expired", plan_id="accelerate"))
    sub, _, _ = await _state(tid)
    assert (sub.psp_subscription_id, sub.plan_id, sub.status) == ("sub_old8", "launch", "active")
    assert queued == []


async def test_a_first_purchase_moves_the_workspace_only_once_it_is_paid(
        client, monkeypatch, noop_provider):
    _, tid = await _seeded_tenant(client, slug="one9", plan_id="free")
    _, before, credits = await _state(tid)

    await _send(client, monkeypatch, "customer.subscription.created",
                _sub(tid, "sub_new9", "incomplete"))
    sub, balance, _ = await _state(tid)
    assert sub.plan_id == "free" and sub.psp_subscription_id in (None, "")
    assert balance == before

    # An abandoned attempt expiring must not cancel the free plan the workspace is on.
    await _send(client, monkeypatch, "customer.subscription.updated",
                _sub(tid, "sub_new9", "incomplete_expired"))
    sub, _, _ = await _state(tid)
    assert (sub.plan_id, sub.status) == ("free", "active")

    await _send(client, monkeypatch, "customer.subscription.updated",
                _sub(tid, "sub_paid9", "active"))
    sub, balance, _ = await _state(tid)
    assert (sub.plan_id, sub.psp_subscription_id) == ("launch", "sub_paid9")
    assert balance == before + credits


async def test_a_bank_debit_checkout_waits_for_the_money(client, monkeypatch, noop_provider):
    _, tid = await _seeded_tenant(client, slug="one10", plan_id="free")
    _, before, credits = await _state(tid)

    await _send(client, monkeypatch, "checkout.session.completed",
                _session(tid, "sub_ach10", "unpaid"))
    sub, balance, _ = await _state(tid)
    assert sub.plan_id == "free" and balance == before

    await _send(client, monkeypatch, "checkout.session.async_payment_succeeded",
                _session(tid, "sub_ach10", "paid"))
    sub, balance, _ = await _state(tid)
    assert (sub.plan_id, sub.psp_subscription_id, sub.status) == ("launch", "sub_ach10",
                                                                  "active")
    assert balance == before + credits


async def test_a_failed_bank_debit_leaves_the_workspace_where_it_was(
        client, monkeypatch, noop_provider):
    _, tid = await _seeded_tenant(client, slug="one11", plan_id="free")
    _, before, _ = await _state(tid)
    await _send(client, monkeypatch, "checkout.session.completed",
                _session(tid, "sub_ach11", "unpaid"))
    await _send(client, monkeypatch, "checkout.session.async_payment_failed",
                _session(tid, "sub_ach11", "unpaid"))
    sub, balance, _ = await _state(tid)
    assert (sub.plan_id, sub.status, balance) == ("free", "active", before)
    assert "pending_psp_subscription" not in (sub.meta or {})


def test_the_asynchronous_checkout_events_are_handled():
    from nexus.billing.webhooks import HANDLED_EVENTS

    assert "checkout.session.async_payment_succeeded" in HANDLED_EVENTS
    assert "checkout.session.async_payment_failed" in HANDLED_EVENTS
