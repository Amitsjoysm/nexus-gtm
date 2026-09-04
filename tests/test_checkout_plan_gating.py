# tests/test_checkout_plan_gating.py
"""A customer must not be able to buy a plan that was never for sale.

Found by probing the running deployment: an ordinary authenticated customer POSTed
`/api/billing/checkout` with `plan_id: "internal"` and got back a real Stripe Checkout session.

`internal` is the STAFF tier. It costs $0 and its `plan_class` is in `_UNLIMITED_CLASSES`, so
`resolve_entitlement` returns `mode="unlimited"` and every quota check and every credit burn is
skipped. Completing that $0 checkout would put a paying-tier customer on unlimited everything.
`legacy-unlimited` (the pre-billing grandfather plan) and `trial` are exposed the same way.

`UNPURCHASABLE_PLAN_CLASSES` was `("free",)` alone, and the reasoning behind that entry — a $0
plan should not go through a payment page — applies word for word to the three that were missing.

The price list already excluded them. The list and the till disagreeing about what is for sale is
the shape of the bug, so the test pins them to the same source rather than to two literals that
can drift.
"""
from __future__ import annotations

import pytest


def _classes():
    from nexus.api.routers.billing import (
        ADMIN_MANAGED_PLAN_CLASSES,
        UNPURCHASABLE_PLAN_CLASSES,
    )

    return set(ADMIN_MANAGED_PLAN_CLASSES) | set(UNPURCHASABLE_PLAN_CLASSES)


# Every class a self-serve customer must never reach through hosted checkout, and why.
NEVER_SELF_SERVE = {
    "internal": "the staff tier — $0 and in _UNLIMITED_CLASSES, so buying it grants everything",
    "unlimited": "grandfathered pre-billing tenants; bypasses quotas and credit burns entirely",
    "partner": "also in _UNLIMITED_CLASSES",
    "trial": "granted by the trial flow, not bought; $0 with credits attached",
    "free": "a $0 downgrade has no business on a payment page",
    "custom": "a negotiated per-tenant deal, re-buyable at whatever the price row says",
    "enterprise": "as custom",
}


@pytest.mark.parametrize("plan_class,why", sorted(NEVER_SELF_SERVE.items()))
def test_no_privileged_plan_class_can_be_bought(plan_class, why):
    assert plan_class in _classes(), (
        f"checkout accepts plan_class={plan_class!r}: {why}"
    )


def test_every_unlimited_class_is_refused_by_checkout():
    """Bound to the engine's own set rather than a copy, so adding a fourth unlimited class
    cannot quietly open a fourth way to buy unlimited usage."""
    from nexus.billing.entitlements import _UNLIMITED_CLASSES

    reachable = sorted(_UNLIMITED_CLASSES - _classes())
    assert not reachable, (
        f"these plan classes grant unlimited usage AND can be checked out: {reachable}"
    )


def test_no_zero_priced_plan_is_purchasable():
    """A $0 plan on a payment page is either a downgrade wearing a card form or a free upgrade.

    Read from the seed rather than a list, because the failure was precisely that a new $0 plan
    (`internal`) was added and the checkout guard was not revisited.
    """
    from nexus.billing.plans import PLAN_SEED

    purchasable_free = sorted({
        p["id"] for p in PLAN_SEED
        if p.get("status") == "active"
        and int(p.get("base_price_cents") or 0) == 0
        and p["plan_class"] not in _classes()
    })
    assert not purchasable_free, (
        f"these cost nothing and can be bought: {purchasable_free}"
    )


def test_the_paid_ladder_is_still_purchasable():
    """The guard must not become so broad that nothing can be sold."""
    from nexus.billing.plans import PLAN_SEED

    sellable = {
        p["id"] for p in PLAN_SEED
        if p.get("status") == "active"
        and int(p.get("base_price_cents") or 0) > 0
        and p["plan_class"] not in _classes()
    }
    for expected in ("launch", "launch-annual", "accelerate", "accelerate-annual"):
        assert expected in sellable, f"{expected} is no longer purchasable"
