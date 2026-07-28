# tests/test_billing_credits.py
from __future__ import annotations

from tests.conftest import make_tenant, tenant_session


async def test_grant_and_balance():
    from nexus.billing.credits import balance, grant_credits

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        await grant_credits(ts, 2000, kind="grant", reason="monthly plan grant",
                            idempotency_key="grant:2026-07")
        assert await balance(ts) == 2000


async def test_grant_is_idempotent():
    from nexus.billing.credits import balance, grant_credits

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        for _ in range(3):
            await grant_credits(ts, 500, kind="grant", reason="monthly",
                                idempotency_key="grant:2026-07")
        assert await balance(ts) == 500          # the monthly grant lands exactly once


async def test_burn_reduces_balance_and_can_go_negative_only_when_allowed():
    from nexus.billing.credits import balance, burn_credits, grant_credits

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        await grant_credits(ts, 100, kind="grant", reason="x", idempotency_key="g1")
        ok = await burn_credits(ts, 30, reason="ai.email_draft overage",
                                idempotency_key="b1", capability_id="ai.email_draft")
        assert ok is True and await balance(ts) == 70

        # Insufficient balance without allow_negative -> refused, ledger unchanged.
        ok = await burn_credits(ts, 500, reason="too much", idempotency_key="b2")
        assert ok is False and await balance(ts) == 70

        # Overage billing explicitly permits going negative (invoiced later).
        ok = await burn_credits(ts, 500, reason="overage", idempotency_key="b3",
                                allow_negative=True)
        assert ok is True and await balance(ts) == -430


async def test_ledger_is_append_only_history():
    from nexus.billing.credits import burn_credits, grant_credits, history
    from nexus.models.billing import BillingCreditLedger

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        await grant_credits(ts, 100, kind="grant", reason="a", idempotency_key="g")
        await burn_credits(ts, 40, reason="b", idempotency_key="b")
        rows = await ts.list(BillingCreditLedger)
        assert len(rows) == 2                       # nothing mutated, both movements kept
        h = await history(ts, limit=10)
        assert len(h) == 2
