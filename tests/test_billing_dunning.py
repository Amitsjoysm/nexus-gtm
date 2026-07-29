# tests/test_billing_dunning.py
"""Dunning: recover a declined payment, or escalate honestly.

Without this a declined card is a silent write-off — the invoice sits finalized forever with an
error attached and nobody retries or is told. Most failed card payments succeed on a later
attempt, so this is where the recoverable revenue is.
"""
from __future__ import annotations

from datetime import timedelta

from tests.conftest import make_tenant, tenant_session


async def _failed_invoice(slug: str = "dun", plan_id: str = "growth"):
    """A tenant holding one finalized invoice whose collection has already failed."""
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.billing.rates import sync_rates
    from nexus.billing.rating import finalize_invoice, rate_period
    from nexus.billing.rollups import period_key, rebuild_rollups
    from nexus.core.db import utcnow
    from nexus.models.billing import BillingSubscription

    await sync_catalog()
    await sync_plans()
    await sync_rates()
    tid = await make_tenant(slug=slug, name=slug)
    async with tenant_session(tid) as ts:
        ts.add(BillingSubscription(plan_id=plan_id, status="active",
                                   psp_customer_id="cus_test"))
        await ts.flush()
        await rebuild_rollups(ts)
        inv = await rate_period(ts, period_key=period_key(utcnow(), "period"))
        await finalize_invoice(ts, inv.id)
        inv.meta = {**(inv.meta or {}), "payment_failed_at": utcnow().isoformat(),
                    "last_payment_error": "card_declined"}
        await ts.flush()
        return tid, inv.id


class DecliningProvider:
    """Every charge fails, the way a dead card does."""

    name = "declining"

    def __init__(self):
        self.attempts = 0

    async def ensure_customer(self, **k):
        return "cus_test"

    async def attach_payment_method(self, **k):
        return True

    async def ensure_plan_price(self, **k):
        return {}

    async def refund(self, **k):
        raise NotImplementedError

    async def charge(self, **k):
        from nexus.billing.payments import PaymentResult

        self.attempts += 1
        return PaymentResult(ok=False, provider=self.name, detail={"error": "card_declined"})


async def test_a_due_invoice_is_retried(monkeypatch):
    from nexus.billing.dunning import run_dunning
    from nexus.billing.payments import set_payment_provider

    provider = DecliningProvider()
    set_payment_provider(provider)
    try:
        tid, inv_id = await _failed_invoice("dun1")
        async with tenant_session(tid) as ts:
            res = await run_dunning(ts)
        assert res["due"] == 1
        assert res["attempted"] == 1
        assert provider.attempts == 1
    finally:
        set_payment_provider(None)


async def test_it_does_not_retry_before_the_next_attempt_is_due():
    """Retrying faster than the schedule damages authorization rates and can cost a fee."""
    from nexus.billing.dunning import run_dunning
    from nexus.billing.payments import set_payment_provider

    provider = DecliningProvider()
    set_payment_provider(provider)
    try:
        tid, _ = await _failed_invoice("dun2")
        async with tenant_session(tid) as ts:
            await run_dunning(ts)            # attempt 1, schedules the next for +3 days
            await run_dunning(ts)            # immediately again
            await run_dunning(ts)
        assert provider.attempts == 1        # only the first was actually due
    finally:
        set_payment_provider(None)


async def test_the_schedule_is_followed_across_time():
    from nexus.billing.dunning import run_dunning
    from nexus.billing.payments import set_payment_provider
    from nexus.core.db import utcnow

    provider = DecliningProvider()
    set_payment_provider(provider)
    try:
        tid, _ = await _failed_invoice("dun3")
        now = utcnow()
        async with tenant_session(tid) as ts:
            await run_dunning(ts, now=now)                          # 1
            await run_dunning(ts, now=now + timedelta(days=4))      # 2
            await run_dunning(ts, now=now + timedelta(days=20))     # 3, exhausts
        assert provider.attempts == 3
    finally:
        set_payment_provider(None)


async def test_exhausted_dunning_escalates_without_voiding_the_debt():
    """The debt is real. Escalate to past_due; writing it off is a human decision."""
    from nexus.billing.dunning import run_dunning
    from nexus.billing.payments import set_payment_provider
    from nexus.core.db import utcnow
    from nexus.models.billing import BillingInvoice, BillingSubscription

    set_payment_provider(DecliningProvider())
    try:
        tid, inv_id = await _failed_invoice("dun4")
        now = utcnow()
        async with tenant_session(tid) as ts:
            for offset in (0, 4, 20):
                await run_dunning(ts, now=now + timedelta(days=offset))

            inv = await ts.get(BillingInvoice, inv_id)
            sub = await ts.first(BillingSubscription)
            assert inv.status == "finalized"          # still owed, never silently voided
            assert inv.meta["dunning_exhausted_at"]
            assert sub.status == "past_due"           # escalated
    finally:
        set_payment_provider(None)


async def test_a_recovered_payment_marks_the_invoice_paid():
    """The happy path this exists for: the retry succeeds."""
    from nexus.billing.dunning import run_dunning
    from nexus.billing.payments import NoopPaymentProvider, set_payment_provider
    from nexus.models.billing import BillingInvoice

    set_payment_provider(NoopPaymentProvider())      # always succeeds
    try:
        tid, inv_id = await _failed_invoice("dun5")
        async with tenant_session(tid) as ts:
            res = await run_dunning(ts)
            assert res["recovered"] == 1
            inv = await ts.get(BillingInvoice, inv_id)
            assert inv.status == "paid"
            assert inv.meta["recovered_at"]
    finally:
        set_payment_provider(None)


async def test_an_invoice_that_never_failed_is_left_alone():
    """Dunning owns retries, not first attempts."""
    from nexus.billing.dunning import run_dunning
    from nexus.billing.payments import set_payment_provider
    from nexus.models.billing import BillingInvoice

    provider = DecliningProvider()
    set_payment_provider(provider)
    try:
        tid, inv_id = await _failed_invoice("dun6")
        async with tenant_session(tid) as ts:
            inv = await ts.get(BillingInvoice, inv_id)
            inv.meta = {}                     # never attempted
            await ts.flush()
            res = await run_dunning(ts)
        assert res["due"] == 0
        assert provider.attempts == 0
    finally:
        set_payment_provider(None)


async def test_a_paid_invoice_is_never_dunned():
    from nexus.billing.dunning import run_dunning
    from nexus.billing.payments import set_payment_provider
    from nexus.models.billing import BillingInvoice

    provider = DecliningProvider()
    set_payment_provider(provider)
    try:
        tid, inv_id = await _failed_invoice("dun7")
        async with tenant_session(tid) as ts:
            inv = await ts.get(BillingInvoice, inv_id)
            inv.status = "paid"
            await ts.flush()
            assert (await run_dunning(ts))["due"] == 0
        assert provider.attempts == 0
    finally:
        set_payment_provider(None)


async def test_the_sweep_can_be_switched_off(monkeypatch):
    from nexus.core.config import get_settings
    from nexus.workers.tasks import handle_dunning_sweep

    monkeypatch.setattr(get_settings(), "billing_dunning_enabled", False)
    assert (await handle_dunning_sweep({}))["skipped"] == "dunning_disabled"


# ---- seats are a gauge, not a counter -------------------------------------------------------

async def test_seat_usage_counts_live_members_not_events():
    """Summing events would only ever climb: remove a member and a counter still shows the old
    seat count, so the customer could never get back under their limit."""
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.entitlements import current_usage
    from nexus.models.identity import Membership, User

    await sync_catalog()
    tid = await make_tenant(slug="seats", name="Seats")
    async with tenant_session(tid) as ts:
        assert await current_usage(ts, "seat.member") == 0

        users = []
        for i in range(3):
            u = User(email=f"m{i}@seats.test", full_name=f"M{i}", password_hash="x")
            ts.session.add(u)
            await ts.flush()
            ts.session.add(Membership(tenant_id=tid, user_id=u.id, role="rep"))
            users.append(u)
        await ts.flush()
        assert await current_usage(ts, "seat.member") == 3

        # Removing a member must lower the count. A counter could never do this.
        member = await ts.first(Membership, Membership.user_id == users[0].id)
        await ts.delete(member)
        await ts.flush()
        assert await current_usage(ts, "seat.member") == 2
