# tests/test_stripe_admin_subscriptions.py
"""A superadmin starts a workspace on a Stripe subscription, or moves a Stripe-billed workspace to
another plan at Stripe (decided 2026-10-09)."""
from __future__ import annotations

import pytest

from tests.conftest import auth, signup
from tests.stripe_fakes import TwoAccountStripe
from tests.test_admin_permissions import _bootstrap_admin, _make_role


@pytest.fixture
def stripe():
    from nexus.billing.payments import set_payment_provider

    fake = TwoAccountStripe()
    set_payment_provider(fake)
    yield fake
    set_payment_provider(None)


async def _boss_and_customer(client, monkeypatch, slug: str):
    boss = await _bootstrap_admin(client, monkeypatch, slug)
    company = f"{slug} Customer"
    await signup(client, slug=f"{slug}-c", email=f"o@{slug}-c.com", company=company)
    rows = (await client.get(f"/api/admin/billing/customers?q={company}",
                             headers=auth(boss))).json()
    return boss, rows[0]["tenant_id"]


async def _sub(tid: str):
    from sqlalchemy import select

    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.billing import BillingSubscription

    async with get_platform_sessionmaker()() as s:
        return (await s.scalars(select(BillingSubscription).where(
            BillingSubscription.tenant_id == tid))).first()


def _start(client, boss, tid, **body):
    return client.post(f"/api/admin/billing/tenants/{tid}/stripe-subscription",
                       headers=auth(boss), json={"plan_id": "launch", **body})


async def test_an_emailed_invoice_starts_the_subscription_with_credits_waiting(
        client, monkeypatch, stripe):
    boss, tid = await _boss_and_customer(client, monkeypatch, "sas1")
    r = await _start(client, boss, tid, method="invoice")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["method"] == "invoice" and body["days_until_due"] == 14
    sub = await _sub(tid)
    assert sub.plan_id == "launch" and sub.psp_subscription_id == body["psp_subscription_id"]
    assert sub.psp_customer_id.startswith("cus_acct_old")
    assert sub.meta["credits_pending_plan"] == "launch"
    assert sub.meta["collection"] == "send_invoice"
    assert stripe.subscriptions[sub.psp_subscription_id]["days_until_due"] == 14


async def test_the_days_to_pay_can_be_changed(client, monkeypatch, stripe):
    boss, tid = await _boss_and_customer(client, monkeypatch, "sas2")
    r = await _start(client, boss, tid, method="invoice", days_until_due=30)
    assert r.status_code == 200, r.text
    assert stripe.subscriptions[(await _sub(tid)).psp_subscription_id]["days_until_due"] == 30


async def test_a_checkout_link_changes_nothing_until_the_customer_pays(
        client, monkeypatch, stripe):
    boss, tid = await _boss_and_customer(client, monkeypatch, "sas3")
    plan_before = (await _sub(tid)).plan_id
    r = await _start(client, boss, tid, method="checkout_link")
    assert r.status_code == 200, r.text
    assert r.json()["url"].startswith("https://") and r.json()["expires_at"] > 0
    assert stripe.checkout_sessions[-1]["tenant_id"] == tid
    assert (await _sub(tid)).plan_id == plan_before


async def test_a_workspace_already_on_stripe_is_refused(client, monkeypatch, stripe):
    boss, tid = await _boss_and_customer(client, monkeypatch, "sas4")
    assert (await _start(client, boss, tid, method="invoice")).status_code == 200
    again = await _start(client, boss, tid, method="invoice")
    assert again.status_code == 409 and "Change its plan" in again.text


async def test_a_plan_with_nothing_to_bill_is_refused(client, monkeypatch, stripe):
    boss, tid = await _boss_and_customer(client, monkeypatch, "sas5")
    r = await _start(client, boss, tid, plan_id="free", method="invoice")
    assert r.status_code == 409


async def test_without_stripe_it_says_so(client, monkeypatch):
    boss, tid = await _boss_and_customer(client, monkeypatch, "sas6")
    r = await _start(client, boss, tid, method="invoice")
    assert r.status_code == 409 and "Payments tab" in r.text


async def test_only_a_superadmin_can_start_one(client, monkeypatch, stripe):
    boss, tid = await _boss_and_customer(client, monkeypatch, "sas7")
    finance = await _make_role(client, boss, "cfo@sas7.com", "finance", "sas7f")
    assert (await _start(client, finance, tid, method="invoice")).status_code == 403
