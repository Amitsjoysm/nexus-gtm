# tests/test_credits_only_billing.py
"""One price per request, taken from the rate card, paid in credits.

Two prices used to exist for the same action: the rate card, and `overage_price_credits` on the
plan entitlement. They disagreed on 11 plan/capability pairs, in both directions:

* ``verify.email`` was 0.25 credits in plan and 1.00 past the allowance — 4x more expensive for
  crossing a line the customer cannot see;
* ``enrich.contact`` on `core` was 4.0 in plan and 2.00 past it — HALF price for overflowing, so a
  customer acting rationally would deliberately exceed their own quota.

Worse, credits were only burned for the portion BEYOND the quota (`if over > 0`), so a request
inside the allowance cost nothing at all and the balance a customer was sold barely moved. "You
have 2,000 credits" was not the truth about anything.

Now: every metered request burns ``credits_per_unit x quantity``, whatever side of any line it
falls on, and when the balance cannot cover it the call stops.

GAUGES ARE THE EXCEPTION, deliberately. ``seat.member``, ``platform.storage`` and
``network.persons`` resolve to a live count — members held, GB stored — not to an action somebody
performed. Charging them per request is meaningless, and letting them run on credits would silently
lock people out of a workspace they are paying for when the balance ran dry. They keep hard caps
and stay outside the credit system entirely.
"""
from __future__ import annotations

import pytest

from nexus.models.identity import Tenant


def _rate(capability_id: str) -> float:
    """The seeded rate-card price. Derived, never hard-coded: a repricing must fail this file
    loudly rather than leave it asserting a stale number."""
    from nexus.billing.rates import RATE_SEED

    return next(
        float(r["credits_per_unit"]) for r in RATE_SEED if r["capability_id"] == capability_id
    )


async def _tenant(plan_id: str, *, credits: float):
    from nexus.billing.credits import grant_credits
    from nexus.billing.subscriptions import ensure_subscription
    from nexus.core.db import get_sessionmaker
    from nexus.core.tenancy import TenantSession

    async with get_sessionmaker()() as s:
        t = Tenant(name=plan_id, slug=f"co-{plan_id}-{int(credits)}")
        s.add(t)
        await s.flush()
        ts = TenantSession(s, t.id)
        await ensure_subscription(ts, plan_id=plan_id)
        if credits:
            await grant_credits(ts, credits, kind="grant", reason="test",
                                idempotency_key=f"seed:{t.id}")
        await s.commit()
        return t.id


async def _call(tenant_id: str, capability_id: str, *, quantity: float = 1, key: str | None = None):
    from nexus.billing.entitlements import check_and_meter
    from nexus.core.db import get_sessionmaker
    from nexus.core.tenancy import TenantSession

    async with get_sessionmaker()() as s:
        ts = TenantSession(s, tenant_id)
        r = await check_and_meter(ts, capability_id=capability_id, quantity=quantity,
                                  idempotency_key=key)
        await s.commit()
        return r


async def _balance(tenant_id: str) -> float:
    from nexus.billing.credits import balance
    from nexus.core.db import get_sessionmaker
    from nexus.core.tenancy import TenantSession

    async with get_sessionmaker()() as s:
        return await balance(TenantSession(s, tenant_id))


@pytest.fixture
async def seeded(fresh_db, monkeypatch):
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.billing.rates import sync_rates
    from nexus.core.config import get_settings

    await sync_catalog()
    await sync_plans()
    await sync_rates()
    monkeypatch.setattr(get_settings(), "billing_enforcement", "on")


# ---- uniform pricing ---------------------------------------------------------------------------

async def test_a_request_inside_the_allowance_still_costs_credits(seeded):
    """THE change. Credits were burned only past the quota, so an in-plan request was free and the
    balance a customer was sold barely moved."""
    tid = await _tenant("accelerate", credits=8000)
    before = await _balance(tid)
    assert (await _call(tid, "enrich.account")).allowed is True
    after = await _balance(tid)
    assert after < before, "an in-plan request deducted nothing from the balance"


async def test_the_price_is_the_rate_card(seeded):
    """Not a second number on the plan — the rate card is the only price."""
    expected = _rate("enrich.account")
    tid = await _tenant("accelerate", credits=8000)
    before = await _balance(tid)
    await _call(tid, "enrich.account")
    assert before - await _balance(tid) == pytest.approx(expected)


async def test_the_same_request_costs_the_same_on_every_plan(seeded):
    """The non-uniformity that motivated this: `verify.email` cost 0.25 in plan and 1.00 past it,
    so the same action had two prices depending on an invisible line."""
    costs = {}
    for plan in ("launch", "accelerate"):
        tid = await _tenant(plan, credits=5000)
        before = await _balance(tid)
        await _call(tid, "verify.email")
        costs[plan] = before - await _balance(tid)
    assert costs["launch"] == pytest.approx(costs["accelerate"]), costs


async def test_quantity_multiplies_the_price(seeded):
    rate = _rate("enrich.account")
    tid = await _tenant("accelerate", credits=8000)
    before = await _balance(tid)
    await _call(tid, "enrich.account", quantity=5)
    assert before - await _balance(tid) == pytest.approx(rate * 5)


async def test_a_retry_with_the_same_key_does_not_double_charge(seeded):
    tid = await _tenant("accelerate", credits=8000)
    await _call(tid, "enrich.account", key="same-key")
    after_first = await _balance(tid)
    await _call(tid, "enrich.account", key="same-key")
    assert await _balance(tid) == after_first


# ---- exhaustion --------------------------------------------------------------------------------

async def test_an_exhausted_balance_stops_the_call(seeded):
    tid = await _tenant("free", credits=0)
    result = await _call(tid, "enrich.account")
    assert result.allowed is False
    assert result.reason in ("credits_exhausted", "quota_exhausted")


async def test_a_balance_too_small_for_this_request_stops_it(seeded):
    """A balance below one request's price is not "some credits left", it is not enough."""
    tid = await _tenant("free", credits=_rate("enrich.account") - 1)
    before = await _balance(tid)
    assert (await _call(tid, "enrich.account")).allowed is False
    assert await _balance(tid) == before, "a refused call must not partially charge"


async def test_credits_run_down_to_zero_and_then_stop(seeded):
    """End to end, the customer's mental model: spend until it is gone, then it stops."""
    rate = _rate("enrich.account")
    tid = await _tenant("free", credits=rate * 3)   # exactly three calls' worth
    for i in range(3):
        assert (await _call(tid, "enrich.account", key=f"k{i}")).allowed is True, f"call {i}"
    assert await _balance(tid) == 0
    assert (await _call(tid, "enrich.account", key="k-final")).allowed is False


# ---- gauges keep hard caps ---------------------------------------------------------------------

async def test_seats_are_capped_not_charged(seeded):
    """A gauge resolves to a live count, not an action. Charging per request is meaningless, and
    running seats on credits would lock people out of a workspace they are paying for."""
    tid = await _tenant("free", credits=200)
    before = await _balance(tid)
    await _call(tid, "seat.member")
    assert await _balance(tid) == before, "a gauge deducted credits"


async def test_storage_is_capped_not_charged(seeded):
    tid = await _tenant("free", credits=200)
    before = await _balance(tid)
    await _call(tid, "platform.storage")
    assert await _balance(tid) == before


async def test_a_gauge_over_its_cap_is_still_refused(seeded):
    """The cap has to still bite, or an uncapped free tier could add 500 members."""
    tid = await _tenant("free", credits=200)     # free allows 1 seat
    result = await _call(tid, "seat.member", quantity=5)
    assert result.allowed is False


# ---- the compatibility lines -------------------------------------------------------------------

async def test_shadow_mode_charges_nothing_and_blocks_nothing(seeded, monkeypatch):
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "billing_enforcement", "shadow")
    tid = await _tenant("free", credits=0)
    assert (await _call(tid, "enrich.account")).allowed is True


async def test_legacy_unlimited_is_never_charged_or_blocked(seeded):
    """Grandfathered tenants were never given a balance and are invoiced on other terms."""
    tid = await _tenant("legacy-unlimited", credits=0)
    before = await _balance(tid)
    assert (await _call(tid, "enrich.account")).allowed is True
    assert await _balance(tid) == before


async def test_an_unpriced_capability_is_free_and_allowed(seeded):
    """No rate card means nothing to charge. It must not become a block."""
    tid = await _tenant("free", credits=0)
    result = await _call(tid, "module.lists")
    assert result.allowed is True
