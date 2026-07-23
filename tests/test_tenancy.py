"""Multi-tenant isolation is the security backbone — these tests guard it."""
from __future__ import annotations

import pytest

from nexus.core.tenancy import TenancyViolation, set_current_tenant
from nexus.models.account import Account
from tests.conftest import make_tenant, tenant_session


async def test_reads_are_scoped_to_the_active_tenant():
    t1 = await make_tenant("alpha", "Alpha")
    t2 = await make_tenant("beta", "Beta")

    async with tenant_session(t1) as ts:
        ts.add(Account(tenant_id=t1, name="Alpha Co", domain="alpha.co"))
        await ts.flush()

    async with tenant_session(t2) as ts:
        ts.add(Account(tenant_id=t2, name="Beta Co", domain="beta.co"))
        await ts.flush()

    async with tenant_session(t1) as ts:
        accts = await ts.list(Account)
        assert [a.name for a in accts] == ["Alpha Co"]

    async with tenant_session(t2) as ts:
        accts = await ts.list(Account)
        assert [a.name for a in accts] == ["Beta Co"]


async def test_get_returns_none_across_tenant_boundary():
    t1 = await make_tenant("alpha", "Alpha")
    t2 = await make_tenant("beta", "Beta")

    async with tenant_session(t1) as ts:
        acc = Account(tenant_id=t1, name="Alpha Co", domain="alpha.co")
        ts.add(acc)
        await ts.flush()
        acc_id = acc.id

    async with tenant_session(t2) as ts:
        assert await ts.get(Account, acc_id) is None


async def test_flush_listener_blocks_cross_tenant_insert():
    t1 = await make_tenant("alpha", "Alpha")
    t2 = await make_tenant("beta", "Beta")

    # Active tenant is t2 but the object claims t1 → must be rejected.
    set_current_tenant(t2)
    from nexus.core.db import get_sessionmaker

    async with get_sessionmaker()() as session:
        session.add(Account(tenant_id=t1, name="Sneaky", domain="x.co"))
        with pytest.raises(TenancyViolation):
            await session.flush()
    set_current_tenant(None)


async def test_insert_without_tenant_context_is_rejected():
    set_current_tenant(None)
    from nexus.core.db import get_sessionmaker

    async with get_sessionmaker()() as session:
        session.add(Account(name="Orphan", domain="orphan.co"))
        with pytest.raises(TenancyViolation):
            await session.flush()


async def test_flush_resolves_tenant_from_session_not_stale_context_var():
    """Regression (H-2): the before_flush guard must resolve the active tenant from the FLUSHING
    session (session.info), not a single process-global context var that a second session could
    have overwritten. Here a second session's binding is simulated by pointing the context var at
    another tenant while ts flushes its own — this previously raised a spurious TenancyViolation.

    (Single DB session on purpose: SQLite serialises writers, so two live uncommitted write
    sessions is a driver limitation — M-1 — not what this test is about.)"""
    t1 = await make_tenant("alpha", "Alpha")
    t2 = await make_tenant("beta", "Beta")

    async with tenant_session(t1) as ts:
        ts.add(Account(tenant_id=t1, name="Alpha Co", domain="alpha.co"))
        # Simulate another tenant session having set the process-global last.
        set_current_tenant(t2)
        try:
            await ts.flush()  # must NOT raise: ts.session.info pins tenant t1
        finally:
            set_current_tenant(t1)  # restore for a clean commit on context exit

    async with tenant_session(t1) as ts:
        assert [a.name for a in await ts.list(Account)] == ["Alpha Co"]
