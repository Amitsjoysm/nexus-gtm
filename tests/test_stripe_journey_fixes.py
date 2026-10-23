"""What a real customer met walking the whole Stripe flow in test mode (2026-10-23).

Each test is one thing that went wrong on the local deploy, paying with Stripe's test cards:

* after a declined upgrade, updating the card and pressing Switch again (what the 402 tells them to
  do) replayed the decline for 24 hours: the idempotency key named the subscription, the plan and
  the period, none of which a decline changes;
* the declined upgrade's invoice, voided at Stripe, stayed in the customer's list as an unpaid
  $1,910 "finalized" invoice for a switch that never happened; and a late `payment_failed` could
  pull a paid or voided row back to "finalized";
* the invoice list showed `stripe:in_...` as the period, in Stripe-id order, with no charges;
* the seat meter read "0 of 1" for a workspace whose creator is a member: the usage page summed
  usage events for a gauge that enforcement reads as a live count;
* "Manage payment method" was offered to a Free workspace and answered "no payment account yet";
* a paid plan set to end could be resumed only on Stripe's page;
* Platform health rated "no webhook endpoint points at this app" as degraded, though no payment
  ever arrives, and told a test-mode account it "cannot take payment" while test payments worked.
"""
from __future__ import annotations

import time

from tests.conftest import auth
from tests.test_billing_checkout_api import _rep_token, _seeded_tenant
from tests.test_one_stripe_subscription import _follow
from tests.test_stripe_invoice_credits import _send


async def _invoices(client, token):
    r = await client.get("/api/billing/invoices", headers=auth(token))
    assert r.status_code == 200, r.text
    return r.json()


def _invoice(tid: str, psp: str, invoice_id: str, *, status: str, amount: int = 191000,
             reason: str = "subscription_update", created: int | None = None) -> dict:
    now = created or int(time.time())
    return {
        "id": invoice_id, "object": "invoice", "customer": f"cus_{tid[:6]}",
        "subscription": psp, "status": status, "amount_due": amount,
        "amount_paid": amount if status == "paid" else 0, "currency": "usd",
        "billing_reason": reason, "number": f"N-{invoice_id[-4:]}", "created": now,
        "hosted_invoice_url": f"https://invoice.stripe.com/i/{invoice_id}",
        "invoice_pdf": f"https://pay.stripe.com/invoice/{invoice_id}/pdf",
        "status_transitions": {"finalized_at": now},
        "lines": {"data": [{"description": "1 x Accelerate (at $1,910.00 / year)",
                            "amount": amount, "currency": "usd",
                            "period": {"start": now, "end": now + 365 * 86400}}]},
    }


# ---- a retry after a decline is a new request at Stripe ------------------------------------------

async def test_a_retry_after_a_declined_switch_is_a_new_request_at_stripe(monkeypatch):
    from nexus.billing.payments import StripePaymentProvider

    keys: list[str] = []
    latest = {"id": "in_first"}
    declined = {"id": "sub_1", "pending_update": {"expires_at": 1},
                "latest_invoice": {"id": "in_declined", "payment_intent": {
                    "status": "requires_payment_method",
                    "last_payment_error": {"message": "Your card was declined."}}}}

    async def fake_request(self, method, path, *, form=None, idempotency_key=""):
        if method == "GET" and path == "/subscriptions/sub_1":
            return {"id": "sub_1", "schedule": None, "latest_invoice": latest["id"],
                    "items": {"data": [{"id": "si_1", "price": {"id": "price_a"}}]}}
        if method == "POST" and path == "/subscriptions/sub_1":
            keys.append(idempotency_key)
            return declined
        return {"id": "in_declined", "status": "void"}

    monkeypatch.setattr(StripePaymentProvider, "_request", fake_request)
    p = StripePaymentProvider("sk_test_retry")
    first = await p.switch_subscription_now(subscription_id="sub_1", price_id="price_b",
                                            plan_id="b", idempotency_key="switch:sub_1:b:100")
    assert first["applied"] is False and first["decline"]
    # A double click, before anything changed at Stripe: the same key, so one charge at most.
    await p.switch_subscription_now(subscription_id="sub_1", price_id="price_b", plan_id="b",
                                    idempotency_key="switch:sub_1:b:100")
    assert keys[0] == keys[1]
    # The decline left its voided invoice as the subscription's latest: the next press, after the
    # customer updated their card, must reach Stripe as a new request.
    latest["id"] = "in_declined"
    await p.switch_subscription_now(subscription_id="sub_1", price_id="price_b", plan_id="b",
                                    idempotency_key="switch:sub_1:b:100")
    assert keys[2] != keys[1]
    assert all(k.startswith("switch:sub_1:b:100") for k in keys)


# ---- voided invoices leave the list; paid and void are final -------------------------------------

async def test_a_voided_invoice_leaves_the_customers_list(client, monkeypatch, noop_provider):
    token, tid = await _seeded_tenant(client, slug="sjvoid", plan_id="launch-annual")
    await _follow(tid, "sub_sjvoid", plan_id="launch-annual")

    await _send(client, monkeypatch, "invoice.finalized",
                _invoice(tid, "sub_sjvoid", "in_sjvoid1", status="open"))
    assert [i["status"] for i in await _invoices(client, token)] == ["finalized"]

    await _send(client, monkeypatch, "invoice.voided",
                _invoice(tid, "sub_sjvoid", "in_sjvoid1", status="void"))
    assert await _invoices(client, token) == []

    # Delivered late, the failure that preceded the void cannot bring it back.
    await _send(client, monkeypatch, "invoice.payment_failed",
                _invoice(tid, "sub_sjvoid", "in_sjvoid1", status="open"))
    assert await _invoices(client, token) == []


async def test_a_voided_invoice_seen_first_is_never_listed(client, monkeypatch, noop_provider):
    token, tid = await _seeded_tenant(client, slug="sjvoid2", plan_id="launch-annual")
    await _follow(tid, "sub_sjvoid2", plan_id="launch-annual")
    await _send(client, monkeypatch, "invoice.voided",
                _invoice(tid, "sub_sjvoid2", "in_sjvoid2", status="void"))
    await _send(client, monkeypatch, "invoice.finalized",
                _invoice(tid, "sub_sjvoid2", "in_sjvoid2", status="open"))
    assert await _invoices(client, token) == []


async def test_a_late_payment_failure_never_reopens_a_paid_invoice(client, monkeypatch,
                                                                    noop_provider):
    token, tid = await _seeded_tenant(client, slug="sjpaid", plan_id="launch-annual")
    await _follow(tid, "sub_sjpaid", plan_id="launch-annual")
    await _send(client, monkeypatch, "invoice.paid",
                _invoice(tid, "sub_sjpaid", "in_sjpaid", status="paid", reason="subscription_cycle"))
    await _send(client, monkeypatch, "invoice.payment_failed",
                _invoice(tid, "sub_sjpaid", "in_sjpaid", status="open", reason="subscription_cycle"))
    (only,) = await _invoices(client, token)
    assert only["status"] == "paid"


def test_the_voided_event_is_one_the_app_asks_stripe_for():
    from nexus.billing.webhooks import HANDLED_EVENTS

    assert "invoice.voided" in HANDLED_EVENTS


async def test_the_repair_takes_stripes_word_on_copies_written_before_the_fix(
        client, monkeypatch, noop_provider):
    """Rows written before `invoice.voided` was handled still read "finalized" and have no issue
    date: `scripts/repair_provider_invoices.py` asks Stripe, dry run first."""
    from sqlalchemy import select

    from nexus.billing.invoice_repair import repair_provider_invoices
    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.billing import BillingInvoice

    token, tid = await _seeded_tenant(client, slug="sjrep", plan_id="launch-annual")
    await _follow(tid, "sub_sjrep", plan_id="launch-annual")
    await _send(client, monkeypatch, "invoice.finalized",
                _invoice(tid, "sub_sjrep", "in_sjrep1", status="open"))
    async with get_platform_sessionmaker()() as s:       # as a row from before the fix looked
        row = (await s.scalars(select(BillingInvoice).where(
            BillingInvoice.tenant_id == tid))).one()
        row.meta = {k: v for k, v in row.meta.items()
                    if k not in ("issued_at", "service_start", "service_end", "lines")}
        await s.commit()

    class AtStripe:
        async def get_invoice(self, invoice_id):
            assert invoice_id == "in_sjrep1"
            return _invoice(tid, "sub_sjrep", "in_sjrep1", status="void")

    dry = await repair_provider_invoices(AtStripe(), apply=False)
    assert (dry.examined, dry.voided, dry.filled) == (1, 1, 1)
    assert [i["status"] for i in await _invoices(client, token)] == ["finalized"]

    done = await repair_provider_invoices(AtStripe(), apply=True)
    assert done.voided == 1
    assert await _invoices(client, token) == []
    again = await repair_provider_invoices(AtStripe(), apply=True)
    assert again.examined == 0                           # idempotent: nothing left to ask about


# ---- the invoice list reads like an invoice list -------------------------------------------------

async def test_a_stripe_invoice_shows_its_service_period_lines_and_links(client, monkeypatch,
                                                                         noop_provider):
    token, tid = await _seeded_tenant(client, slug="sjlist", plan_id="launch")
    await _follow(tid, "sub_sjlist")
    older = int(time.time()) - 40 * 86400
    await _send(client, monkeypatch, "invoice.paid",
                _invoice(tid, "sub_sjlist", "in_zzzolder", status="paid", amount=9900,
                         reason="subscription_create", created=older))
    await _send(client, monkeypatch, "invoice.paid",
                _invoice(tid, "sub_sjlist", "in_aaanewer", status="paid", amount=59900,
                         reason="subscription_update"))

    newer, oldest = await _invoices(client, token)
    # Newest first by when Stripe issued them, not by the order of Stripe's ids.
    assert newer["total_cents"] == 59900 and oldest["total_cents"] == 9900
    assert newer["issued_at"] and newer["period_start"] and newer["period_end"]
    assert newer["period_start"] < newer["period_end"]
    assert newer["lines"][0]["description"].startswith("1 x Accelerate")
    assert newer["lines"][0]["amount_cents"] == 59900
    assert newer["hosted_url"].startswith("https://invoice.stripe.com/")
    assert newer["pdf_url"].endswith("/pdf")


# ---- gauges are live counts on the usage page too ------------------------------------------------

async def test_the_usage_page_counts_seats_as_live_members(client, noop_provider):
    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.identity import Membership, User

    token, tid = await _seeded_tenant(client, slug="sjseat", plan_id="launch")
    async with get_platform_sessionmaker()() as s:
        user = User(email="second@sjseat.com", full_name="Second", password_hash="x")
        s.add(user)
        await s.flush()
        s.add(Membership(tenant_id=tid, user_id=user.id, role="rep"))
        await s.commit()

    usage = (await client.get("/api/billing/usage", headers=auth(token))).json()
    seats = next(c for c in usage["capabilities"] if c["capability_id"] == "seat.member")
    assert seats["used"] == 2


# ---- only a workspace with a payment account is offered the portal -------------------------------

async def test_usage_says_whether_the_workspace_has_a_payment_account(client, noop_provider):
    token, tid = await _seeded_tenant(client, slug="sjacct", plan_id="free")
    usage = (await client.get("/api/billing/usage", headers=auth(token))).json()
    assert usage["payment_account"] is False

    await _follow(tid, "noop_sub_sjacct")
    usage = (await client.get("/api/billing/usage", headers=auth(token))).json()
    assert usage["payment_account"] is True
    assert usage["period_start"] is None or isinstance(usage["period_start"], str)


# ---- a plan set to end can be kept from the app --------------------------------------------------

async def test_keep_my_plan_resumes_a_plan_set_to_end(client, noop_provider):
    from tests.test_hybrid_plan_changes import _set_meta

    token, tid = await _seeded_tenant(client, slug="sjkeep", plan_id="launch")
    await _follow(tid, "noop_sub_sjkeep")
    await _set_meta(tid, cancel_at_period_end=True)
    usage = (await client.get("/api/billing/usage", headers=auth(token))).json()
    assert usage["cancel_at_period_end"] is True

    assert (await client.post("/api/billing/scheduled-change/cancel",
                              headers=auth(_rep_token(tid)), json={})).status_code == 403
    r = await client.post("/api/billing/scheduled-change/cancel", headers=auth(token), json={})
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "kept", "plan_id": "launch"}
    assert noop_provider.resumed == ["noop_sub_sjkeep"]
    usage = (await client.get("/api/billing/usage", headers=auth(token))).json()
    assert usage["cancel_at_period_end"] is False


async def test_the_stripe_adapter_resumes_a_subscription_set_to_end(monkeypatch):
    from nexus.billing.payments import StripePaymentProvider

    calls: list = []

    async def fake_request(self, method, path, *, form=None, idempotency_key=""):
        calls.append((method, path, dict(form or {})))
        return {"id": "sub_1", "status": "active", "cancel_at_period_end": False}

    monkeypatch.setattr(StripePaymentProvider, "_request", fake_request)
    out = await StripePaymentProvider("sk_test_resume").resume_subscription(
        subscription_id="sub_1")
    assert out["cancel_at_period_end"] is False
    assert calls == [("POST", "/subscriptions/sub_1", {"cancel_at_period_end": "false"})]


# ---- Platform health says how bad a webhook gap is -----------------------------------------------

def test_no_endpoint_for_this_app_is_an_error_and_a_missing_event_is_degraded():
    from nexus.api.routers.admin_health import webhook_endpoint_severity

    base = "https://gtm.example.com"
    url = f"{base}/api/billing/webhooks/stripe"
    handled = ("invoice.paid", "invoice.voided")
    assert webhook_endpoint_severity([], base_url=base, handled=handled)[0] == "error"
    assert webhook_endpoint_severity(
        [{"url": url, "status": "disabled", "enabled_events": ["*"]}],
        base_url=base, handled=handled)[0] == "error"
    severity, text = webhook_endpoint_severity(
        [{"url": url, "status": "enabled", "enabled_events": ["invoice.paid"]}],
        base_url=base, handled=handled)
    assert severity == "degraded" and "invoice.voided" in text
    assert webhook_endpoint_severity(
        [{"url": url, "status": "enabled", "enabled_events": list(handled)}],
        base_url=base, handled=handled) == ("ok", "")


def test_a_test_mode_account_is_not_told_it_cannot_take_payment():
    from nexus.api.routers.admin_health import charges_note

    assert "cannot take payment" in charges_note("live")
    note = charges_note("test")
    assert "cannot take payment" not in note and "test" in note
