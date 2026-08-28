# tests/test_billing_ledger_concurrency.py
"""Two check-then-write races on the money path, and the transaction damage one of them does.

Both are invisible on SQLite, which serializes writers, so each is exercised here by injecting
the interleaving the database would otherwise have to produce.
"""
from __future__ import annotations

import pytest

from tests.conftest import make_tenant, tenant_session


async def test_a_concurrent_duplicate_usage_write_leaves_the_session_usable():
    """`record_usage` promises metering can never break the product. It broke it here.

    The dedupe pre-check and the INSERT are not atomic, so two callers holding the same
    idempotency key both pass the check and the loser violates `uq_usage_idempotency` at flush.
    The bare `except Exception` then swallows it — but on Postgres the transaction is already
    aborted, so the caller's very next statement raises PendingRollbackError. The one code path
    the guarantee exists for is the one that broke it.

    The losing interleaving is reproduced by making the pre-check miss a row that is already
    committed, which is exactly what the winning writer leaves behind.
    """
    import nexus.billing.usage as usage_mod
    from nexus.billing.usage import record_usage

    tid = await make_tenant()

    # The winner's row.
    async with tenant_session(tid) as ts:
        assert await record_usage(
            ts, capability_id="ai.email_draft", idempotency_key="race"
        ) is True

    async def blind(ts, key):
        return False        # the pre-check the loser ran before the winner committed

    async with tenant_session(tid) as ts:
        usage_mod._already_recorded = blind
        try:
            wrote = await record_usage(
                ts, capability_id="ai.email_draft", idempotency_key="race"
            )
        finally:
            usage_mod._already_recorded = usage_mod._already_recorded_impl

        assert wrote is False, "the duplicate must not report itself as a new billed row"

        # The whole point: the caller's transaction survives. Without the savepoint this raised
        # PendingRollbackError and took down whatever the metered action was doing.
        again = await record_usage(
            ts, capability_id="ai.email_draft", idempotency_key="after-the-race"
        )
        assert again is True, "the session was poisoned by a swallowed IntegrityError"

    # And exactly one row exists for the contended key.
    from sqlalchemy import func, select

    from nexus.models.billing import BillingUsageEvent

    async with tenant_session(tid) as ts:
        n = await ts.session.scalar(
            select(func.count(BillingUsageEvent.id)).where(
                BillingUsageEvent.tenant_id == tid,
                BillingUsageEvent.idempotency_key == "race",
            )
        )
        assert n == 1, f"the same idempotency key billed {n} times"


async def test_a_balance_gated_burn_serializes_on_the_tenant_not_the_capability():
    """The balance is a tenant-wide pot; the meter's lock is keyed per capability.

    `_lock_capability` correctly serializes the quota read for one capability, but two DIFFERENT
    capabilities burning at once take two different locks, both read the same balance, and both
    conclude there is enough. The account goes negative without `allow_negative`.
    """
    import nexus.billing.credits as credits_mod
    from nexus.billing.credits import burn_credits, grant_credits

    tid = await make_tenant()
    keys: list[str] = []

    original = credits_mod._lock_tenant_credits

    async def spy(ts):
        keys.append(ts.tenant_id)
        return await original(ts)

    async with tenant_session(tid) as ts:
        await grant_credits(ts, 100, reason="x", idempotency_key="g")
        credits_mod._lock_tenant_credits = spy
        try:
            assert await burn_credits(ts, 40, idempotency_key="b1") is True
        finally:
            credits_mod._lock_tenant_credits = original

    assert keys == [tid], (
        "a burn that can be refused for insufficient balance must take the tenant-wide lock; "
        "per-capability locking lets two capabilities overdraw the same pot"
    )


async def test_an_overdraw_is_still_refused_and_an_allowed_overdraft_still_passes():
    """The lock must not change either existing outcome."""
    from nexus.billing.credits import balance, burn_credits, grant_credits

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        await grant_credits(ts, 10, reason="x", idempotency_key="g")

        assert await burn_credits(ts, 25, idempotency_key="too-big") is False
        assert await balance(ts) == 10

        # Overage billing passes `allow_negative` and must still be able to go under.
        assert await burn_credits(
            ts, 25, idempotency_key="overage", allow_negative=True
        ) is True
        assert await balance(ts) == -15
