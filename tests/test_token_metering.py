# tests/test_token_metering.py
"""`ai.tokens` was priced and metered at no call site.

The per-action capabilities charge a flat rate, which is right for a predictable bill: measured
token spread within an agent is about 4x median-to-max, which these margins absorb. `ai.tokens`
records what was actually consumed ALONGSIDE that flat charge, so the flat rate can be checked
against reality instead of assumed.
"""
from __future__ import annotations


async def test_nothing_consumed_records_nothing(fresh_db):
    """A zero-quantity event is noise in the usage stream. Same rule that keeps an unconfigured
    phone lookup off the bill."""
    from nexus.billing.tokens import meter_tokens

    assert await meter_tokens(None, tokens=0, agent="research") is False
    assert await meter_tokens(None, tokens=500, agent="research") is False


async def test_token_metering_never_breaks_the_run(fresh_db):
    """The actual contract: it does not raise.

    It is bookkeeping attached to work the customer is already waiting on, so losing the
    measurement must be much cheaper than losing the run. The return value says whether metering
    was ATTEMPTED, not whether a row landed — `record_usage` a layer down is already defensive and
    swallows its own write failures, so this helper cannot see them.
    """
    from nexus.billing.tokens import meter_tokens

    class Broken:
        tenant_id = "t"

        def __getattr__(self, name):
            raise RuntimeError("session is gone")

    # No exception escaping is the whole assertion.
    await meter_tokens(Broken(), tokens=500, agent="research")


async def test_tokens_are_billed_per_thousand(fresh_db):
    """The rate card prices `ai.tokens` per 1,000 tokens, so the quantity has to be in thousands
    or the bill is off by three orders of magnitude."""
    from sqlalchemy import select

    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.billing.rates import sync_rates
    from nexus.billing.tokens import meter_tokens
    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.billing import BillingUsageEvent
    from tests.conftest import make_tenant, tenant_session

    await sync_catalog()
    await sync_plans()
    await sync_rates()
    tid = await make_tenant("tok")

    async with tenant_session(tid) as ts:
        assert await meter_tokens(ts, tokens=2500, agent="research") is True

    async with get_platform_sessionmaker()() as s:
        rows = [
            e for e in (await s.scalars(select(BillingUsageEvent))).all()
            if e.capability_id == "ai.tokens"
        ]
    assert len(rows) == 1
    assert float(rows[0].quantity) == 2.5, "2,500 tokens is 2.5 units of 1,000"
    assert rows[0].attrs.get("agent") == "research"
    assert rows[0].attrs.get("tokens") == 2500
