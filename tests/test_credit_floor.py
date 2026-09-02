# tests/test_credit_floor.py
"""When a credit-funded plan runs out of credits, the calls stop.

Per-capability quota alone did not achieve that, and the gap was total. The `free` plan lists NINE
capabilities; `resolve_entitlement` falls back to permissive catalog defaults for the other 61, and
``default_quota`` is None for every capability in the seed. So `check_and_meter` skipped its quota
branch entirely (`ent.quota is not None` is False), `enabled` capabilities are explicitly "never
quota-limited", and a free tenant could run unlimited enrichment, search and research while its 200
credits sat untouched. The credits were decorative for 61 of 70 capabilities.

The floor is scoped tightly, because this is the money path and a false block is an outage for a
paying customer:

* only plans that ARE credit-funded (``included_credits > 0``);
* only capabilities that actually cost credits (a live rate card);
* never a `module.*` gate — those price nothing, and blocking one revokes a feature the customer
  still has rather than saying "you are out of credits";
* never when the entitlement carries an explicit overage price, which means "keep going and
  invoice it";
* never in shadow mode.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from nexus.models.identity import Tenant


async def _tenant_on(plan_id: str, *, credits: float = 0.0):
    """A tenant on ``plan_id`` whose credit balance is exactly ``credits``."""
    from nexus.billing.credits import balance, burn_credits, grant_credits
    from nexus.billing.subscriptions import ensure_subscription
    from nexus.core.db import get_sessionmaker
    from nexus.core.tenancy import TenantSession

    async with get_sessionmaker()() as s:
        tenant = Tenant(name=plan_id, slug=f"cf-{plan_id}-{int(credits)}")
        s.add(tenant)
        await s.flush()
        ts = TenantSession(s, tenant.id)
        await ensure_subscription(ts, plan_id=plan_id)
        if credits > 0:
            await grant_credits(ts, credits, kind="grant", reason="test",
                                idempotency_key=f"t:{tenant.id}")
        await s.commit()
        assert await balance(ts) == credits
        return tenant.id


async def _decide(tenant_id: str, capability_id: str):
    from nexus.billing.entitlements import check_and_meter
    from nexus.core.db import get_sessionmaker
    from nexus.core.tenancy import TenantSession

    async with get_sessionmaker()() as s:
        return await check_and_meter(
            TenantSession(s, tenant_id), capability_id=capability_id, quantity=1
        )


@pytest.fixture
async def seeded(fresh_db, monkeypatch):
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.billing.rates import sync_rates
    from nexus.core.config import get_settings

    await sync_catalog()
    await sync_plans()
    await sync_rates()
    # Enforcement ON: the floor must not fire in shadow, which is asserted separately below.
    monkeypatch.setattr(get_settings(), "billing_enforcement", "on")
    return True


async def test_a_free_tenant_with_no_credits_is_blocked(seeded):
    """THE gap. `enrich.account` is not listed on free, so it resolved to a catalog default with
    no quota and ran unbounded while the balance sat at zero."""
    tid = await _tenant_on("free", credits=0)
    result = await _decide(tid, "enrich.account")
    assert result.allowed is False
    assert result.reason == "credits_exhausted"


async def test_a_free_tenant_with_credits_is_allowed(seeded):
    """The compatibility line in the other direction: having credits must still work."""
    tid = await _tenant_on("free", credits=200)
    assert (await _decide(tid, "enrich.account")).allowed is True


# ---- the exemptions, each of which is an outage if wrong --------------------------------------

async def test_legacy_unlimited_is_never_blocked(seeded):
    """Grandfathered tenants are invoiced on other terms and were never given a balance. Stopping
    them would break every pre-billing customer the moment enforcement is armed."""
    tid = await _tenant_on("legacy-unlimited", credits=0)
    assert (await _decide(tid, "enrich.account")).allowed is True


async def test_a_module_gate_is_never_blocked_by_the_floor(seeded):
    """A `module.*` gate prices nothing. Blocking one says 'you lost Campaigns' when the truth is
    'you are out of credits' — a different fact, and an unrecoverable-looking one."""
    tid = await _tenant_on("free", credits=0)
    result = await _decide(tid, "module.lists")
    assert result.reason != "credits_exhausted"


async def test_an_unpriced_capability_is_not_blocked(seeded):
    """Nothing to charge means nothing to run out of."""
    tid = await _tenant_on("free", credits=0)
    result = await _decide(tid, "seat.member")
    assert result.reason != "credits_exhausted"


async def test_shadow_mode_never_blocks(seeded, monkeypatch):
    """The whole promise of shadow is that it changes nothing. A floor that fired there would turn
    a rollout mode into a visible outage."""
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "billing_enforcement", "shadow")
    tid = await _tenant_on("free", credits=0)
    result = await _decide(tid, "enrich.account")
    assert result.allowed is True
    assert result.would_block is True, "shadow must still COMPUTE the block, or the counter lies"


async def test_enforcement_off_never_blocks(seeded, monkeypatch):
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "billing_enforcement", "off")
    tid = await _tenant_on("free", credits=0)
    assert (await _decide(tid, "enrich.account")).allowed is True


async def test_a_paid_plan_with_credits_is_unaffected(seeded):
    """Regression guard for every existing paying customer."""
    tid = await _tenant_on("accelerate", credits=8000)
    assert (await _decide(tid, "enrich.account")).allowed is True


async def test_a_tenant_with_no_subscription_is_still_allowed(seeded):
    """The engine's documented bias: no subscription resolves to allow. The floor must not turn
    that into a block, or a signup whose plan attach failed would be dead on arrival."""
    from nexus.core.db import get_sessionmaker

    async with get_sessionmaker()() as s:
        tenant = Tenant(name="NoSub", slug="cf-nosub")
        s.add(tenant)
        await s.commit()
        tid = tenant.id
    assert (await _decide(tid, "enrich.account")).allowed is True


async def test_the_balance_lookup_failing_does_not_block(seeded, monkeypatch):
    """A guard must never break the call it guards."""
    # Patched AFTER the tenant exists: `_tenant_on` reads the balance itself to assert its own
    # setup, so patching first breaks the fixture rather than the thing under test.
    tid = await _tenant_on("free", credits=0)

    async def boom(_ts):
        raise RuntimeError("ledger unavailable")

    monkeypatch.setattr("nexus.billing.credits.balance", boom)
    assert (await _decide(tid, "enrich.account")).allowed is True
