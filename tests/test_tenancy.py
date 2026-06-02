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
