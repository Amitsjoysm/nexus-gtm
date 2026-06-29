from __future__ import annotations

import uuid

import pytest

from tests.conftest import make_tenant, tenant_session


async def seed_member(ts):
    """Create a global User + tenant-scoped Membership; return the Membership."""
    from nexus.models.identity import Membership, User

    u = User(email=f"u{uuid.uuid4().hex}@x.com", full_name="U", password_hash="x")
    ts.session.add(u)
    await ts.session.flush()
    m = Membership(user_id=u.id, role="rep")
    ts.add(m)
    await ts.session.flush()
    return m


async def test_source_account_round_trip_is_tenant_scoped():
    from nexus.models.network import NetworkSourceAccount

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        m = await seed_member(ts)
        ts.add(
            NetworkSourceAccount(
                member_id=m.id, user_id=m.user_id, provider="fixture",
                external_account_id="rep@acme.com", display_email="rep@acme.com",
            )
        )
        await ts.flush()
        rows = await ts.list(NetworkSourceAccount)
        assert len(rows) == 1
        assert rows[0].tenant_id == tid
        assert rows[0].pooling_enabled is False  # private by default
        assert rows[0].status == "connected"


def test_models_are_registered():
    import nexus.models as m

    for name in ("NetworkSourceAccount", "NetworkPerson", "NetworkIdentity", "NetworkEdge"):
        assert hasattr(m, name), f"{name} not exported from nexus.models"
