# tests/test_stripe_catalog.py
"""Cached Stripe ids are reused only inside the account they were made in (reported 2026-10-09:
"product id not available" after production moved to a live key and staging to a new test
account)."""
from __future__ import annotations

from nexus.billing.stripe_catalog import (
    archive_plan_price,
    customer_id_for,
    plan_price_id,
    rename_plan_product,
)
from nexus.models.billing import BillingPlan, BillingSubscription
from tests.stripe_fakes import TwoAccountStripe

LEGACY_TERMS = {"price_amount_cents": 9900, "price_currency": "USD", "price_interval": "month"}


def _plan(meta: dict | None = None) -> BillingPlan:
    return BillingPlan(id="launch", name="Launch", plan_class="standard", status="active",
                       base_price_cents=9900, currency="USD", interval="month",
                       meta=dict(meta or {}))


def _sub(customer: str | None = None) -> BillingSubscription:
    return BillingSubscription(tenant_id="tenant-1", plan_id="launch",
                               psp_customer_id=customer, meta={})


async def test_a_price_is_created_once_and_reused_in_the_same_account():
    stripe, plan = TwoAccountStripe(), _plan()
    first = await plan_price_id(plan, stripe)
    assert await plan_price_id(plan, stripe) == first
    assert len(stripe.ensured) == 1
    assert plan.meta["price_account"] == "acct_old"


async def test_a_new_account_gets_its_own_product_and_price():
    stripe, plan = TwoAccountStripe(), _plan()
    old = await plan_price_id(plan, stripe)
    stripe.account = "acct_new"
    new = await plan_price_id(plan, stripe)
    assert new != old and new.startswith("price_acct_new")
    assert stripe.ensured[-1]["product_id"] == ""      # the old account's product is not reused
    previous = plan.meta["previous_stripe"][-1]
    assert previous["price_id"] == old and previous["account"] == "acct_old"
    assert previous["product_id"].startswith("prod_acct_old")


async def test_a_new_amount_keeps_the_product_and_archives_the_old_price():
    stripe, plan = TwoAccountStripe(), _plan()
    old = await plan_price_id(plan, stripe)
    product = plan.meta["product_id"]
    plan.base_price_cents = 12900
    new = await plan_price_id(plan, stripe)
    assert new != old and plan.meta["product_id"] == product
    assert stripe.ensured[-1]["product_id"] == product
    assert stripe.price_active[old] is False


async def test_a_cache_from_before_accounts_were_recorded_is_checked_once():
    stripe = TwoAccountStripe()
    stripe.owner["price_legacy"] = "acct_old"
    plan = _plan({"product_id": "prod_legacy", "price_id": "price_legacy", **LEGACY_TERMS})
    assert await plan_price_id(plan, stripe) == "price_legacy"
    assert stripe.ensured == []
    assert plan.meta["price_account"] == "acct_old"


async def test_a_legacy_cache_from_another_account_is_replaced():
    stripe = TwoAccountStripe("acct_live")
    plan = _plan({"product_id": "prod_test", "price_id": "price_test", **LEGACY_TERMS})
    assert (await plan_price_id(plan, stripe)).startswith("price_acct_live")
    assert plan.meta["previous_stripe"][-1]["price_id"] == "price_test"


async def test_an_archived_price_is_reactivated_when_the_plan_is_sold_again():
    stripe, plan = TwoAccountStripe(), _plan()
    price = await plan_price_id(plan, stripe)
    assert await archive_plan_price(plan, stripe) == ""
    assert stripe.price_active[price] is False
    assert await plan_price_id(plan, stripe) == price
    assert stripe.price_active[price] is True


async def test_a_rename_reaches_the_product_in_this_account_only():
    stripe, plan = TwoAccountStripe(), _plan()
    await plan_price_id(plan, stripe)
    plan.name = "Launch Plus"
    assert await rename_plan_product(plan, stripe) == ""
    assert stripe.renames[-1]["name"] == "Launch Plus"
    stripe.account = "acct_other"
    assert await rename_plan_product(plan, stripe) == ""
    assert len(stripe.renames) == 1


async def test_a_customer_from_another_account_is_replaced():
    stripe, sub = TwoAccountStripe(), _sub("cus_test_1")
    cid = await customer_id_for(sub, stripe, tenant_id="tenant-1", email="o@x.com")
    assert cid.startswith("cus_acct_old") and sub.psp_customer_id == cid
    assert sub.meta["previous_psp_customers"][-1]["id"] == "cus_test_1"
    assert sub.meta["psp_customer_account"] == "acct_old"


async def test_a_customer_in_this_account_is_kept():
    stripe, sub = TwoAccountStripe(), _sub("cus_keep")
    stripe.owner["cus_keep"] = "acct_old"
    assert await customer_id_for(sub, stripe, tenant_id="tenant-1", email="o@x.com") == "cus_keep"
    assert sub.meta["psp_customer_account"] == "acct_old"


async def test_no_email_and_no_usable_customer_returns_nothing():
    assert await customer_id_for(_sub(), TwoAccountStripe(), tenant_id="tenant-1",
                                 email="") == ""
