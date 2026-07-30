# tests/test_billing_webhook_lifecycle.py
"""Subscription- and invoice-lifecycle webhooks (M12).

The failure this prevents is divergence: a customer cancels or changes a card in the provider's
own portal, we never hear about it, and our database and the provider disagree about what
somebody owes. These are the events that keep the two in step.

Signature verification, freshness and the exactly-once table are covered in
``test_billing_webhook_security.py`` and are not re-tested here.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest

from tests.conftest import auth, signup  # noqa: F401  (`client` is an auto-discovered fixture)

SECRET = "whsec_test_secret_for_unit_tests"


def _sign(body: bytes, secret: str = SECRET) -> str:
    ts = int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def _body(event_id: str, event_type: str, obj: dict) -> bytes:
    return json.dumps({"id": event_id, "type": event_type, "data": {"object": obj}}).encode()


async def _post(client, event_id: str, event_type: str, obj: dict):
    body = _body(event_id, event_type, obj)
    return await client.post(
        "/api/billing/webhooks/stripe", content=body,
        headers={"Stripe-Signature": _sign(body), "content-type": "application/json"},
    )


@pytest.fixture
def webhook_secret(monkeypatch):
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "stripe_webhook_secret", SECRET)
    return SECRET


async def _tenant_with_subscription(*, slug: str, plan_id: str = "starter", **fields):
    """A seeded tenant holding one subscription. Returns its tenant id."""
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.models.billing import BillingSubscription
    from tests.conftest import make_tenant, tenant_session

    await sync_catalog()
    await sync_plans()
    tid = await make_tenant(slug=slug, name=slug)
    async with tenant_session(tid) as ts:
        ts.add(BillingSubscription(plan_id=plan_id, status="active", **fields))
        await ts.flush()
    return tid


async def _subscription(tid: str):
    from nexus.models.billing import BillingSubscription
    from tests.conftest import tenant_session

    async with tenant_session(tid) as ts:
        return await ts.first(BillingSubscription)


def _period(offset_days: int = 30) -> int:
    return int(time.time()) + offset_days * 86400


# ---- checkout.session.completed --------------------------------------------------------------

async def test_checkout_completion_links_and_activates(client, webhook_secret):
    """The self-serve happy path: our row learns the provider ids and the purchased plan."""
    tid = await _tenant_with_subscription(slug="wh1", plan_id="starter")

    r = await _post(client, "evt_cs_1", "checkout.session.completed", {
        "id": "cs_1", "customer": "cus_1", "subscription": "sub_1",
        "payment_status": "paid", "mode": "subscription",
        "metadata": {"tenant_id": tid, "plan_id": "growth"},
    })
    assert r.status_code == 200, r.text
    assert r.json()["applied"] is True

    sub = await _subscription(tid)
    assert sub.psp_subscription_id == "sub_1"
    assert sub.psp_customer_id == "cus_1"
    assert sub.plan_id == "growth"
    assert sub.status == "active"
    # Taking a new plan means taking its terms — same rule change_plan applies.
    assert sub.grandfathered is False


async def test_checkout_completion_resolves_by_client_reference_id(client, webhook_secret):
    """Sessions created outside our own code path carry the tenant only here."""
    tid = await _tenant_with_subscription(slug="wh2")

    r = await _post(client, "evt_cs_2", "checkout.session.completed", {
        "id": "cs_2", "customer": "cus_2", "subscription": "sub_2",
        "payment_status": "paid", "client_reference_id": tid,
    })
    assert r.status_code == 200, r.text
    assert (await _subscription(tid)).psp_subscription_id == "sub_2"


async def test_checkout_completion_ignores_an_unknown_plan(client, webhook_secret):
    """A subscription pointing at a plan we do not have would break the entitlement chain."""
    tid = await _tenant_with_subscription(slug="wh3", plan_id="starter")

    r = await _post(client, "evt_cs_3", "checkout.session.completed", {
        "id": "cs_3", "customer": "cus_3", "subscription": "sub_3", "payment_status": "paid",
        "metadata": {"tenant_id": tid, "plan_id": "not-a-real-plan"},
    })
    assert r.status_code == 200, r.text

    sub = await _subscription(tid)
    assert sub.plan_id == "starter"          # unchanged
    assert sub.psp_subscription_id == "sub_3"


# ---- customer.subscription.* -----------------------------------------------------------------

async def test_subscription_created_mirrors_status_and_period(client, webhook_secret):
    tid = await _tenant_with_subscription(slug="wh4", psp_customer_id="cus_4")
    start, end = int(time.time()), _period()

    r = await _post(client, "evt_sc_1", "customer.subscription.created", {
        "id": "sub_4", "customer": "cus_4", "status": "trialing",
        "current_period_start": start, "current_period_end": end,
        "cancel_at_period_end": False, "metadata": {"tenant_id": tid},
    })
    assert r.status_code == 200, r.text
    assert r.json()["applied"] is True

    sub = await _subscription(tid)
    assert sub.status == "trialing"
    assert sub.psp_subscription_id == "sub_4"
    assert int(sub.current_period_end.timestamp()) == end


async def test_subscription_updated_mirrors_a_cancellation_request(client, webhook_secret):
    """Cancel-at-period-end in the portal has to reach us, or we keep billing."""
    tid = await _tenant_with_subscription(slug="wh5", psp_subscription_id="sub_5")

    r = await _post(client, "evt_su_1", "customer.subscription.updated", {
        "id": "sub_5", "customer": "cus_5", "status": "active",
        "cancel_at_period_end": True, "metadata": {"tenant_id": tid},
    })
    assert r.status_code == 200, r.text

    sub = await _subscription(tid)
    assert sub.cancel_at_period_end is True
    assert sub.status == "active"


async def test_subscription_deleted_cancels_us_too(client, webhook_secret):
    """The event that matters most: the provider has stopped billing. Staying active would keep
    entitlements open for a customer who is no longer paying."""
    tid = await _tenant_with_subscription(slug="wh6", psp_subscription_id="sub_6")

    r = await _post(client, "evt_sd_1", "customer.subscription.deleted", {
        "id": "sub_6", "customer": "cus_6", "status": "canceled",
    })
    assert r.status_code == 200, r.text

    sub = await _subscription(tid)
    assert sub.status == "canceled"
    assert sub.cancel_at_period_end is False


@pytest.mark.parametrize(
    ("provider_status", "expected"),
    [
        ("active", "active"),
        ("trialing", "trialing"),
        ("past_due", "past_due"),
        # Retries exhausted, still owed. past_due is what our own dunning escalates to and the
        # state that keeps the debt visible.
        ("unpaid", "past_due"),
        ("canceled", "canceled"),
    ],
)
async def test_every_mapped_provider_status_lands_on_an_existing_one(
    client, webhook_secret, provider_status, expected
):
    from nexus.models.billing import SUBSCRIPTION_STATUSES

    slug = f"wh7{provider_status}"
    tid = await _tenant_with_subscription(slug=slug, psp_subscription_id=f"sub_{slug}")

    r = await _post(client, f"evt_map_{provider_status}", "customer.subscription.updated", {
        "id": f"sub_{slug}", "customer": "cus_x", "status": provider_status,
    })
    assert r.status_code == 200, r.text

    sub = await _subscription(tid)
    assert sub.status == expected
    assert sub.status in SUBSCRIPTION_STATUSES     # no invented vocabulary


async def test_an_unmapped_provider_status_changes_nothing(client, webhook_secret):
    """`incomplete` means the subscription never started. Guessing would either cancel a live
    customer or activate one who has not paid."""
    tid = await _tenant_with_subscription(slug="wh8", psp_subscription_id="sub_8")

    r = await _post(client, "evt_unmapped", "customer.subscription.updated", {
        "id": "sub_8", "customer": "cus_8", "status": "incomplete",
    })
    assert r.status_code == 200, r.text

    sub = await _subscription(tid)
    assert sub.status == "active"                  # untouched
    assert sub.meta["unmapped_provider_status"] == "incomplete"


async def test_subscription_event_for_an_unknown_reference_is_harmless(client, webhook_secret):
    r = await _post(client, "evt_orphan", "customer.subscription.updated", {
        "id": "sub_nobody_has", "customer": "cus_nobody", "status": "active",
    })
    assert r.status_code == 200, r.text
    assert r.json()["applied"] is False
    assert r.json()["note"] == "no matching subscription"


# ---- invoice.* -------------------------------------------------------------------------------

async def _tenant_with_finalized_invoice(*, slug: str, psp_reference: str):
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.billing.rates import sync_rates
    from nexus.billing.rating import finalize_invoice, rate_period
    from nexus.billing.rollups import period_key, rebuild_rollups
    from nexus.core.db import utcnow
    from nexus.models.billing import BillingSubscription
    from tests.conftest import make_tenant, tenant_session

    await sync_catalog()
    await sync_plans()
    await sync_rates()
    tid = await make_tenant(slug=slug, name=slug)
    async with tenant_session(tid) as ts:
        ts.add(BillingSubscription(
            plan_id="growth", status="active", psp_subscription_id=f"sub_{slug}",
            psp_customer_id=f"cus_{slug}",
        ))
        await ts.flush()
        await rebuild_rollups(ts)
        inv = await rate_period(ts, period_key=period_key(utcnow(), "period"))
        await finalize_invoice(ts, inv.id)
        inv.meta = {**(inv.meta or {}), "psp_reference": psp_reference}
        await ts.flush()
        return tid, inv.id


async def test_invoice_paid_marks_our_invoice_paid(client, webhook_secret):
    from nexus.models.billing import BillingInvoice
    from tests.conftest import tenant_session

    tid, inv_id = await _tenant_with_finalized_invoice(slug="wi1", psp_reference="pi_wi1")

    r = await _post(client, "evt_ip_1", "invoice.paid", {
        "id": "in_wi1", "customer": "cus_wi1", "subscription": "sub_wi1",
        "payment_intent": "pi_wi1", "status": "paid", "amount_paid": 7900,
    })
    assert r.status_code == 200, r.text
    assert r.json()["applied"] is True

    async with tenant_session(tid) as ts:
        inv = await ts.get(BillingInvoice, inv_id)
        assert inv.status == "paid"
        assert inv.meta["psp_invoice_id"] == "in_wi1"


async def test_invoice_payment_failed_leaves_the_debt_open_and_arms_dunning(
    client, webhook_secret
):
    """A failed payment must never void a real debt, and dunning keys off payment_failed_at."""
    from nexus.billing.dunning import is_due
    from nexus.models.billing import BillingInvoice
    from tests.conftest import tenant_session

    tid, inv_id = await _tenant_with_finalized_invoice(slug="wi2", psp_reference="pi_wi2")

    r = await _post(client, "evt_if_1", "invoice.payment_failed", {
        "id": "in_wi2", "customer": "cus_wi2", "subscription": "sub_wi2",
        "payment_intent": "pi_wi2", "status": "open", "amount_due": 7900,
    })
    assert r.status_code == 200, r.text

    async with tenant_session(tid) as ts:
        inv = await ts.get(BillingInvoice, inv_id)
        assert inv.status == "finalized"           # still owed, not voided
        assert inv.meta["payment_failed_at"]
        assert is_due(inv) is True                 # the sweep will pick it up


async def test_invoice_paid_clears_the_next_dunning_attempt(client, webhook_secret):
    """Otherwise the next sweep charges a card for money the provider already took."""
    from nexus.billing.dunning import is_due
    from nexus.models.billing import BillingInvoice
    from tests.conftest import tenant_session

    tid, inv_id = await _tenant_with_finalized_invoice(slug="wi3", psp_reference="pi_wi3")
    async with tenant_session(tid) as ts:
        inv = await ts.get(BillingInvoice, inv_id)
        inv.meta = {**(inv.meta or {}), "payment_failed_at": "2026-01-01T00:00:00+00:00",
                    "dunning_attempts": 1, "next_attempt_at": "2026-01-02T00:00:00+00:00"}
        await ts.flush()

    r = await _post(client, "evt_ip_3", "invoice.paid", {
        "id": "in_wi3", "payment_intent": "pi_wi3", "status": "paid",
    })
    assert r.status_code == 200, r.text

    async with tenant_session(tid) as ts:
        inv = await ts.get(BillingInvoice, inv_id)
        assert inv.status == "paid"
        assert "next_attempt_at" not in inv.meta
        assert is_due(inv) is False


async def test_invoice_finalized_records_the_provider_reference(client, webhook_secret):
    from nexus.models.billing import BillingInvoice
    from tests.conftest import tenant_session

    tid, inv_id = await _tenant_with_finalized_invoice(slug="wi4", psp_reference="pi_wi4")

    r = await _post(client, "evt_ifin_1", "invoice.finalized", {
        "id": "in_wi4", "payment_intent": "pi_wi4", "status": "open", "amount_due": 7900,
    })
    assert r.status_code == 200, r.text

    async with tenant_session(tid) as ts:
        inv = await ts.get(BillingInvoice, inv_id)
        # Our own finalization is ours; the provider event only annotates.
        assert inv.status == "finalized"
        assert inv.meta["psp_amount_due_cents"] == 7900


async def test_a_provider_invoice_we_do_not_have_is_recorded_not_dropped(client, webhook_secret):
    """Hosted subscriptions generate provider invoices with no local row. Recording the
    reference keeps the money traceable without inventing an invoice."""
    tid = await _tenant_with_subscription(slug="wi5", psp_subscription_id="sub_wi5")

    r = await _post(client, "evt_ip_5", "invoice.paid", {
        "id": "in_wi5", "subscription": "sub_wi5", "customer": "cus_wi5", "status": "paid",
    })
    assert r.status_code == 200, r.text
    assert r.json()["applied"] is True

    sub = await _subscription(tid)
    assert sub.meta["psp_latest_invoice_id"] == "in_wi5"


async def test_an_invoice_event_matching_nothing_at_all_is_harmless(client, webhook_secret):
    r = await _post(client, "evt_ip_orphan", "invoice.paid", {
        "id": "in_nobody", "customer": "cus_nobody", "status": "paid",
    })
    assert r.status_code == 200, r.text
    assert r.json()["applied"] is False


# ---- idempotency ------------------------------------------------------------------------------

async def test_replaying_a_lifecycle_event_is_a_no_op(client, webhook_secret):
    """The event-id primary key catches the redelivery, and the endpoint still answers 200 —
    a duplicate is not a failure, or the provider retries forever."""
    tid = await _tenant_with_subscription(slug="wr1", psp_subscription_id="sub_wr1")
    payload = {"id": "sub_wr1", "customer": "cus_wr1", "status": "past_due"}

    first = await _post(client, "evt_replay_1", "customer.subscription.updated", payload)
    second = await _post(client, "evt_replay_1", "customer.subscription.updated", payload)

    assert first.status_code == 200 and first.json()["duplicate"] is False
    assert second.status_code == 200 and second.json()["duplicate"] is True
    assert (await _subscription(tid)).status == "past_due"


@pytest.mark.parametrize(
    ("event_type", "obj"),
    [
        ("checkout.session.completed",
         {"id": "cs_i", "customer": "cus_i", "subscription": "sub_idem",
          "payment_status": "paid"}),
        ("customer.subscription.created",
         {"id": "sub_idem", "customer": "cus_i", "status": "active",
          "current_period_end": _period()}),
        ("customer.subscription.updated",
         {"id": "sub_idem", "customer": "cus_i", "status": "past_due"}),
        ("customer.subscription.deleted", {"id": "sub_idem", "customer": "cus_i"}),
    ],
)
async def test_each_handler_is_idempotent_on_its_own(client, webhook_secret, event_type, obj):
    """Stronger than the dedupe table: applying the SAME change twice under DIFFERENT event ids
    (which the primary key does not catch — providers do redeliver under new ids after an
    endpoint change) must converge, not accumulate."""
    from nexus.billing.webhooks import VerifiedEvent, handle_event
    from nexus.core.db import get_platform_sessionmaker

    slug = f"idem{event_type.split('.')[-1]}"
    tid = await _tenant_with_subscription(slug=slug, psp_subscription_id="sub_idem")
    payload = {**obj, "metadata": {"tenant_id": tid}}

    states = []
    for n in (1, 2):
        event = VerifiedEvent(
            event_id=f"evt_{slug}_{n}", event_type=event_type,
            payload={"data": {"object": payload}}, digest="d",
        )
        async with get_platform_sessionmaker()() as session:
            outcome = await handle_event(session, event)
            await session.commit()
        assert outcome["applied"] is True
        states.append((outcome["status"], outcome.get("plan_id")))

    assert states[0] == states[1]                  # converged, not accumulated


async def test_an_out_of_order_delete_then_update_stays_canceled_only_if_told(
    client, webhook_secret
):
    """Absolute assignment, not accumulation: whatever the provider last said wins. A later
    `active` update legitimately reactivates; the point is that we mirror rather than infer."""
    tid = await _tenant_with_subscription(slug="wr2", psp_subscription_id="sub_wr2")

    await _post(client, "evt_ooo_1", "customer.subscription.deleted",
                {"id": "sub_wr2", "customer": "cus_wr2"})
    assert (await _subscription(tid)).status == "canceled"

    await _post(client, "evt_ooo_2", "customer.subscription.updated",
                {"id": "sub_wr2", "customer": "cus_wr2", "status": "active"})
    assert (await _subscription(tid)).status == "active"


async def test_lifecycle_events_never_cross_tenants(client, webhook_secret):
    """The webhook reads across tenants by design (the platform session). It must still only
    ever write the one workspace the reference belongs to."""
    a = await _tenant_with_subscription(slug="xta", psp_subscription_id="sub_xta")
    b = await _tenant_with_subscription(slug="xtb", psp_subscription_id="sub_xtb")

    r = await _post(client, "evt_xt_1", "customer.subscription.deleted",
                    {"id": "sub_xta", "customer": "cus_xta"})
    assert r.status_code == 200 and r.json()["tenant_id"] == a

    assert (await _subscription(a)).status == "canceled"
    assert (await _subscription(b)).status == "active"      # untouched
