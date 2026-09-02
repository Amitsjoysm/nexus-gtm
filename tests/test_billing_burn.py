# tests/test_billing_burn.py
"""Credits are actually spent — one price per request, and no second price anywhere.

**This file used to pin a four-step ladder**: included quota first, then credits, then an explicit
overage price, then the wall. That model is gone. It gave the same action up to three different
prices depending on where in the period it happened to land, so a customer could not answer "what
does this cost?" without knowing their own running total, and the answer changed on the 1st of the
month. Worse, the last two steps put the same overage in two places — the ledger at the moment of
use and the invoice at period close — which is a real double-charge this codebase shipped once and
had to grow a dedicated regression test for.

The model now: **every priced request burns the rate card, and when the balance cannot cover it the
call stops.** One price, paid one way, at the moment of use.

What that deletes, and why each deletion is safe:

* *inside quota burns nothing* — there is no free tier inside a period any more. A plan's credits
  ARE its quota; spending them is what using the plan means.
* *an overage price allows without credits* — the wall is now the balance, so "keep going and
  invoice it" no longer exists for a credit-priced capability.
* *the plan overage price beats the rate card* — there is one price, so there is nothing to beat.
* *overage beyond the balance still reaches the invoice* — nothing reaches the invoice.
  `test_no_priced_action_can_ever_reach_an_invoice_as_overage` pins that structurally, which is a
  stronger statement than the two agreement tests it replaces: they checked that the ledger and the
  invoice charged the SAME amount, and this checks the invoice charges nothing at all.

GAUGES ARE UNAFFECTED and still hold hard quotas — seats and stored bytes are levels, not requests,
so there is no "request" to price. `_is_gauge` is the fork.
"""
from __future__ import annotations

import pytest

from tests.conftest import make_tenant, tenant_session


async def _seed(plan_id: str = "free"):
    """A tenant on `plan_id`."""
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.billing.rates import sync_rates
    from tests.conftest import put_on_plan

    await sync_catalog()
    await sync_plans()
    await sync_rates()
    tid = await make_tenant()
    await put_on_plan(tid, plan_id)
    return tid


@pytest.fixture
def enforcing(monkeypatch):
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "billing_enforcement", "on")
    return get_settings()


# ---- the charge --------------------------------------------------------------------------------

async def test_every_priced_request_spends_credits(enforcing):
    """THE model. No free window, no ladder — the card is charged on request one."""
    from nexus.billing.credits import balance, grant_credits
    from nexus.billing.entitlements import check_and_meter

    tid = await _seed()
    async with tenant_session(tid) as ts:
        await grant_credits(ts, 1000, reason="x", idempotency_key="g")
        res = await check_and_meter(ts, capability_id="ai.email_draft", idempotency_key="a")
        assert res.allowed is True
        assert await balance(ts) < 1000, "a priced request must cost something"


async def test_the_price_does_not_change_with_how_much_was_used_before(enforcing):
    """Uniformity, stated directly: the 1st request and the 500th cost the same.

    This is what the customer is actually buying — a number they can multiply. Under the old ladder
    the same draft cost 0 credits inside quota and 2 outside it, so the answer to "what does an
    email draft cost?" depended on the calendar.
    """
    from nexus.billing.credits import balance, grant_credits
    from nexus.billing.entitlements import check_and_meter

    tid = await _seed()
    async with tenant_session(tid) as ts:
        await grant_credits(ts, 10_000, reason="x", idempotency_key="g")

        start = await balance(ts)
        await check_and_meter(ts, capability_id="ai.email_draft", idempotency_key="first")
        first_cost = start - await balance(ts)

        # Push far past what the old free quota (20) would have been.
        for i in range(30):
            await check_and_meter(ts, capability_id="ai.email_draft", idempotency_key=f"mid{i}")

        before_last = await balance(ts)
        await check_and_meter(ts, capability_id="ai.email_draft", idempotency_key="last")
        last_cost = before_last - await balance(ts)

    assert first_cost > 0
    assert first_cost == last_cost, (
        f"the same action cost {first_cost} then {last_cost} — the price moved with usage"
    )


async def test_quantity_scales_the_charge(enforcing):
    from nexus.billing.credits import balance, grant_credits
    from nexus.billing.entitlements import check_and_meter

    tid = await _seed()
    async with tenant_session(tid) as ts:
        await grant_credits(ts, 10_000, reason="x", idempotency_key="g")
        start = await balance(ts)
        await check_and_meter(ts, capability_id="ai.email_draft", idempotency_key="one")
        one = start - await balance(ts)

        mid = await balance(ts)
        await check_and_meter(ts, capability_id="ai.email_draft", quantity=5,
                              idempotency_key="five")
        five = mid - await balance(ts)

    assert five == pytest.approx(one * 5)


# ---- the wall ----------------------------------------------------------------------------------

async def test_an_empty_balance_stops_the_call(enforcing):
    """"When credits run out, calls stop." The balance IS the wall now."""
    from nexus.billing.credits import balance
    from nexus.billing.entitlements import check_and_meter

    tid = await _seed()
    async with tenant_session(tid) as ts:
        res = await check_and_meter(ts, capability_id="ai.email_draft", idempotency_key="a")
        assert res.allowed is False
        assert res.reason == "credits_exhausted"
        assert await balance(ts) == 0, "nothing may be spent on a refused action"


async def test_a_balance_too_small_for_the_call_stops_it(enforcing):
    """All-or-nothing. A partly-paid action would be a service half-delivered and half-billed."""
    from nexus.billing.credits import balance, grant_credits
    from nexus.billing.entitlements import check_and_meter

    tid = await _seed()
    async with tenant_session(tid) as ts:
        await grant_credits(ts, 1, reason="tiny", idempotency_key="g")
        res = await check_and_meter(ts, capability_id="enrich.account", quantity=100,
                                    idempotency_key="big")
        assert res.allowed is False
        assert res.reason == "credits_exhausted"
        assert await balance(ts) == 1, "a refused call must leave the balance untouched"


async def test_an_unpriced_capability_is_not_a_wall(enforcing):
    """Nothing to charge means nothing to run out of.

    An unpriced capability that blocked would turn "we forgot to price this" into an outage, which
    is the opposite of the engine's unknown-means-allow bias.
    """
    from nexus.billing.entitlements import check_and_meter
    from nexus.models.billing import BillingRateCard

    tid = await _seed()
    async with tenant_session(tid) as ts:
        card = await ts.session.get(BillingRateCard, "ai.email_draft")
        if card is not None:
            card.active = False
        await ts.flush()
        res = await check_and_meter(ts, capability_id="ai.email_draft", idempotency_key="a")
        assert res.allowed is True


# ---- the properties that survived the model change ---------------------------------------------

async def test_burn_is_idempotent_across_retries(enforcing):
    """A retried request re-derives the same charge; it must pay once, and must not be refused for
    an action it has already paid for."""
    from nexus.billing.credits import balance, grant_credits
    from nexus.billing.entitlements import check_and_meter

    tid = await _seed()
    async with tenant_session(tid) as ts:
        await grant_credits(ts, 1000, reason="x", idempotency_key="g")

        first = await check_and_meter(ts, capability_id="ai.email_draft", idempotency_key="same")
        after_first = await balance(ts)
        second = await check_and_meter(ts, capability_id="ai.email_draft", idempotency_key="same")

        assert first.allowed is True and second.allowed is True
        assert after_first < 1000
        assert await balance(ts) == after_first, "charged twice for one request"


async def test_a_failing_burn_degrades_to_allow(monkeypatch, enforcing):
    """The seam must never break the product, even when the money path errors.

    Losing one charge beats refusing a customer's work because our ledger had a bad second.
    """
    import nexus.billing.entitlements as ent_mod
    from nexus.billing.entitlements import check_and_meter

    async def boom(*a, **k):
        raise RuntimeError("ledger down")

    tid = await _seed()
    async with tenant_session(tid) as ts:
        monkeypatch.setattr(ent_mod, "_burn_for_usage", boom)
        res = await check_and_meter(ts, capability_id="ai.email_draft", idempotency_key="v1")
        assert res.allowed is True


async def test_shadow_mode_still_never_blocks():
    """Default posture is unchanged: evaluate and record, never refuse. No `enforcing` fixture."""
    from nexus.billing.entitlements import check_and_meter

    tid = await _seed()
    async with tenant_session(tid) as ts:
        res = await check_and_meter(ts, capability_id="ai.email_draft", idempotency_key="s1")
        assert res.allowed is True
        assert res.would_block is True, "shadow mode must still compute what it WOULD have done"


async def test_a_gauge_still_uses_a_hard_quota_not_credits(enforcing):
    """Seats are a LEVEL, not a request. There is no "seat request" to price, and summing seat
    events would only ever climb — so a customer could never get back under a seat limit."""
    from nexus.billing.credits import balance, grant_credits
    from nexus.billing.entitlements import check_and_meter

    tid = await _seed()
    async with tenant_session(tid) as ts:
        await grant_credits(ts, 1000, reason="x", idempotency_key="g")
        await check_and_meter(ts, capability_id="seat.member", idempotency_key="s")
        assert await balance(ts) == 1000, "a gauge must not draw on the credit balance"


# ---- the double-charge, closed structurally ----------------------------------------------------

async def test_no_priced_action_can_ever_reach_an_invoice_as_overage(enforcing):
    """The regression that motivated the whole change, now impossible rather than merely absent.

    Found by running the loop against Stripe: 200 units past quota burned 200 credits at the moment
    of use AND appeared as a 200c overage line on the invoice. The old fix made the two agree; this
    removes the second charge entirely, so there is nothing left to disagree.
    """
    from nexus.billing.credits import grant_credits
    from nexus.billing.entitlements import check_and_meter
    from nexus.billing.rating import rate_period
    from nexus.billing.rollups import period_key, rebuild_rollups
    from nexus.core.db import utcnow
    from nexus.models.billing import BillingInvoiceLine

    tid = await _seed("launch")
    async with tenant_session(tid) as ts:
        await grant_credits(ts, 50_000, reason="plenty", idempotency_key="g")
        await check_and_meter(ts, capability_id="verify.email", quantity=5200,
                              idempotency_key="bulk")

        await rebuild_rollups(ts)
        inv = await rate_period(ts, period_key=period_key(utcnow(), "period"))
        lines = await ts.list(BillingInvoiceLine, BillingInvoiceLine.invoice_id == inv.id)

    overage = [ln for ln in lines if ln.kind == "overage"]
    assert overage == [], (
        f"a credit-priced action produced an invoice line: {[(l.kind, l.amount_cents) for l in overage]}"
        " — the customer would pay for it twice"
    )
