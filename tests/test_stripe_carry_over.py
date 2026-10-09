# tests/test_stripe_carry_over.py
"""Switching the Stripe account changes only where the money goes (product owner, 2026-10-09).

Live subscriptions billed in the previous account are recreated in the new one, first billed where
the old paid period ends, and the old ones are told to end then. Nothing is charged twice, no
credits are granted twice, and nothing moves without the old key that can stop the old one.
"""
from __future__ import annotations

import uuid

import pytest

from tests.conftest import auth
from tests.stripe_fakes import TwoAccountStripe
from tests.test_admin_permissions import _make_role
from tests.test_stripe_admin_subscriptions import _boss_and_customer, _change, _start, _sub


@pytest.fixture
def stripe(monkeypatch):
    from nexus.billing import stripe_carryover
    from nexus.billing.payments import set_payment_provider

    fake = TwoAccountStripe()
    set_payment_provider(fake)

    async def olds(current_key):
        return [fake.view("acct_old")]

    monkeypatch.setattr(stripe_carryover, "previous_account_providers", olds)
    yield fake
    set_payment_provider(None)


def _olds(monkeypatch, providers):
    from nexus.billing import stripe_carryover

    async def olds(current_key):
        return providers

    monkeypatch.setattr(stripe_carryover, "previous_account_providers", olds)


async def _switched(client, monkeypatch, stripe, slug):
    """A workspace billed in acct_old, then the key changes to acct_new."""
    boss, tid = await _boss_and_customer(client, monkeypatch, slug)
    assert (await _start(client, boss, tid, method="invoice")).status_code == 200
    old_id = (await _sub(tid)).psp_subscription_id
    stripe.account = "acct_new"
    return boss, tid, old_id


def _carry(client, token):
    return client.post("/api/admin/billing/subscriptions/stripe-carry-over", headers=auth(token))


def _row(response, tid):
    return next(r for r in response.json()["subscriptions"] if r["tenant_id"] == tid)


async def _ledger_rows(tid) -> int:
    from sqlalchemy import func, select

    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.billing import BillingCreditLedger

    async with get_platform_sessionmaker()() as s:
        return int(await s.scalar(select(func.count()).select_from(BillingCreditLedger).where(
            BillingCreditLedger.tenant_id == tid)) or 0)


# ---- the move ------------------------------------------------------------------------------------

async def test_a_live_subscription_moves_and_the_old_one_ends_when_the_paid_period_does(
        client, monkeypatch, stripe):
    boss, tid, old_id = await _switched(client, monkeypatch, stripe, "sco1")
    paid_until = stripe.subscriptions[old_id]["current_period_end"]
    r = await _carry(client, boss)
    assert r.status_code == 200, r.text
    assert _row(r, tid)["outcome"] == "carried"

    sub = await _sub(tid)
    new_id = sub.psp_subscription_id
    assert new_id != old_id and stripe.owner[new_id] == "acct_new"
    assert sub.plan_id == "launch" and sub.psp_customer_id.startswith("cus_acct_new")
    assert stripe.carried[-1]["starts_at"] == paid_until        # nothing billed twice
    assert stripe.carried[-1]["payment_method"] == ""           # no card here: an invoice
    assert stripe.subscriptions[new_id]["collection_method"] == "send_invoice"
    old = stripe.subscriptions[old_id]
    assert old["cancel_at_period_end"] is True and old["metadata"]["nexus_carried_to"] == new_id
    assert sub.meta["previous_psp_subscriptions"][-1] == {"id": old_id, "account": "acct_old"}
    assert sub.meta["psp_account"] == "acct_new"


async def test_no_credits_are_granted_for_a_period_already_paid(client, monkeypatch, stripe):
    boss, tid, _old = await _switched(client, monkeypatch, stripe, "sco2")
    before = await _ledger_rows(tid)
    assert _row(await _carry(client, boss), tid)["outcome"] == "carried"
    assert await _ledger_rows(tid) == before


async def test_a_card_stripe_copied_into_the_new_account_is_charged_as_before(
        client, monkeypatch, stripe):
    boss, tid, _old = await _switched(client, monkeypatch, stripe, "sco3")
    customer = (await _sub(tid)).psp_customer_id
    stripe.copied.add(("acct_new", customer))
    stripe.cards[("acct_new", customer)] = "pm_copied"
    assert _row(await _carry(client, boss), tid)["outcome"] == "carried"
    sub = await _sub(tid)
    assert sub.psp_customer_id == customer                      # adopted, not duplicated
    assert stripe.carried[-1]["payment_method"] == "pm_copied"
    assert stripe.subscriptions[sub.psp_subscription_id]["collection_method"] == \
        "charge_automatically"


async def test_a_subscription_already_in_this_account_is_left_alone(client, monkeypatch, stripe):
    boss, tid = await _boss_and_customer(client, monkeypatch, "sco4")
    assert (await _start(client, boss, tid, method="invoice")).status_code == 200
    r = await _carry(client, boss)
    assert r.status_code == 200 and all(x["tenant_id"] != tid for x in r.json()["subscriptions"])
    assert stripe.carried == []


# ---- what stops it -------------------------------------------------------------------------------

async def test_without_the_old_key_nothing_moves(client, monkeypatch, stripe):
    """A new subscription beside an old one nobody can stop would bill the customer twice."""
    boss, tid, old_id = await _switched(client, monkeypatch, stripe, "sco5")
    _olds(monkeypatch, [])
    row = _row(await _carry(client, boss), tid)
    assert row["outcome"] == "failed" and "not stored here" in row["detail"]
    assert (await _sub(tid)).psp_subscription_id == old_id and stripe.carried == []


async def test_one_the_old_account_already_ends_is_left(client, monkeypatch, stripe):
    boss, tid, old_id = await _switched(client, monkeypatch, stripe, "sco6")
    stripe.subscriptions[old_id] = {**stripe.subscriptions[old_id], "cancel_at_period_end": True}
    row = _row(await _carry(client, boss), tid)
    assert row["outcome"] == "skipped" and stripe.carried == []


async def test_if_the_old_account_refuses_to_end_it_the_new_one_goes_at_once(
        client, monkeypatch, stripe):
    boss, tid, old_id = await _switched(client, monkeypatch, stripe, "sco7")
    old_view = stripe.view("acct_old")
    old_view.refuse_end = True
    _olds(monkeypatch, [old_view])
    row = _row(await _carry(client, boss), tid)
    assert row["outcome"] == "failed" and "refused to end it" in row["detail"]
    new_id = next(k for k, v in stripe.owner.items()
                  if v == "acct_new" and k in stripe.subscriptions)
    assert {"id": new_id, "at_period_end": False, "account": "acct_new"} in stripe.cancellations
    assert (await _sub(tid)).psp_subscription_id == old_id


async def test_a_half_finished_move_is_finished_not_repeated(client, monkeypatch, stripe):
    boss, tid, old_id = await _switched(client, monkeypatch, stripe, "sco8")
    made = await stripe.create_carried_subscription(
        customer_id="cus_x", price_id="price_x", tenant_id=tid, plan_id="launch",
        starts_at=0, trial=False, payment_method="", days_until_due=14, carried_from=old_id,
        idempotency_key="earlier-run")
    await stripe.view("acct_old").end_carried_subscription(subscription_id=old_id,
                                                           carried_to=made["id"])
    row = _row(await _carry(client, boss), tid)
    assert row["outcome"] == "carried" and row["to"] == made["id"]
    assert len(stripe.carried) == 1                             # only the earlier run's
    assert (await _sub(tid)).psp_subscription_id == made["id"]


# ---- anything that acts on a subscription moves it first -----------------------------------------

async def test_a_plan_change_moves_a_missed_subscription_then_reprices_it(
        client, monkeypatch, stripe):
    boss, tid, old_id = await _switched(client, monkeypatch, stripe, "sco9")
    r = await _change(client, boss, tid)
    assert r.status_code == 200, r.text
    sub = await _sub(tid)
    assert sub.plan_id == "accelerate" and sub.psp_subscription_id != old_id
    assert stripe.price_changes[-1]["id"] == sub.psp_subscription_id


async def test_cancel_reaches_a_subscription_still_in_the_old_account(
        client, monkeypatch, stripe):
    boss, tid, old_id = await _switched(client, monkeypatch, stripe, "sco10")
    r = await client.post(f"/api/admin/billing/tenants/{tid}/subscription/cancel",
                          headers=auth(boss), json={"at_period_end": True})
    assert r.status_code == 200, r.text
    assert stripe.cancellations[-1] == {"id": old_id, "at_period_end": True,
                                        "account": "acct_old"}
    assert (await _sub(tid)).cancel_at_period_end is True


async def test_the_portal_opens_on_the_new_accounts_customer(client, monkeypatch, stripe):
    from tests.test_billing_checkout_api import _seeded_tenant

    token, _tid = await _seeded_tenant(client, slug="sco11")
    body = {"plan_id": "accelerate", "success_url": "https://app/ok", "cancel_url": "https://app/no"}
    assert (await client.post("/api/billing/checkout", json=body,
                              headers=auth(token))).status_code == 200
    stripe.account = "acct_new"
    r = await client.post("/api/billing/portal", json={}, headers=auth(token))
    assert r.status_code == 200, r.text
    assert stripe.portal_sessions[-1]["customer_id"].startswith("cus_acct_new")


# ---- who and when --------------------------------------------------------------------------------

async def test_the_retry_is_superadmin_only(client, monkeypatch, stripe):
    boss, _tid = await _boss_and_customer(client, monkeypatch, "sco12")
    finance = await _make_role(client, boss, "cfo@sco12.com", "finance", "sco12f")
    assert (await _carry(client, finance)).status_code == 403


async def test_without_stripe_the_retry_says_so(client, monkeypatch):
    from nexus.billing.payments import NoopPaymentProvider, set_payment_provider

    set_payment_provider(NoopPaymentProvider())
    try:
        boss, _tid = await _boss_and_customer(client, monkeypatch, "sco13")
        r = await _carry(client, boss)
        assert r.status_code == 409 and "No Stripe credential" in r.text
    finally:
        set_payment_provider(None)


async def test_making_a_new_key_live_moves_the_subscriptions(client, monkeypatch, stripe):
    from nexus.billing.credentials import add_credential
    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.payment_credential import PaymentCredential

    from nexus.billing import payments

    boss, tid, _old = await _switched(client, monkeypatch, stripe, "sco14")

    async def resolve():
        return stripe

    # Activation drops any injected provider (it re-reads the credential), so pin the fake.
    monkeypatch.setattr(payments, "resolve_payment_provider", resolve)
    row = await add_credential(label="new", secret_key="sk_test_carry14")
    async with get_platform_sessionmaker()() as s:
        (await s.get(PaymentCredential, row.id)).status = "verified"
        await s.commit()
    r = await client.post(f"/api/admin/payment-credentials/{row.id}/activate",
                          headers=auth(boss))
    assert r.status_code == 200, r.text
    carried = [x for x in r.json()["stripe_carry"] if x.get("tenant_id") == tid]
    assert carried and carried[0]["outcome"] == "carried"


# ---- the webhook ---------------------------------------------------------------------------------

async def _deliver(event_type: str, obj: dict) -> dict:
    from nexus.billing.webhooks import VerifiedEvent, handle_event
    from nexus.core.db import get_platform_sessionmaker

    async with get_platform_sessionmaker()() as s:
        out = await handle_event(s, VerifiedEvent(
            event_id=f"evt_{uuid.uuid4().hex}", event_type=event_type,
            payload={"data": {"object": obj}}, digest=""))
        await s.commit()
    return out


async def test_the_old_subscriptions_end_never_ends_the_new_one(client, monkeypatch, stripe):
    boss, tid, old_id = await _switched(client, monkeypatch, stripe, "sco15")
    assert _row(await _carry(client, boss), tid)["outcome"] == "carried"
    out = await _deliver("customer.subscription.deleted",
                         {"id": old_id, "customer": "cus_old", "metadata": {"tenant_id": tid}})
    assert out["applied"] is False
    assert (await _sub(tid)).status == "active"


async def test_the_carried_first_period_keeps_the_period_it_was_paid_for(
        client, monkeypatch, stripe):
    import time

    boss, tid, old_id = await _switched(client, monkeypatch, stripe, "sco16")
    assert _row(await _carry(client, boss), tid)["outcome"] == "carried"
    sub = await _sub(tid)
    started, ends = sub.current_period_start, sub.current_period_end
    from nexus.billing.stripe_carryover import _epoch_of

    await _deliver("customer.subscription.updated", {
        "id": sub.psp_subscription_id, "customer": sub.psp_customer_id, "status": "active",
        "current_period_start": int(time.time()), "current_period_end": _epoch_of(ends),
        "metadata": {"tenant_id": tid, "plan_id": "launch", "nexus_carried_from": old_id}})
    after = await _sub(tid)
    assert after.current_period_start == started and after.status == "active"
