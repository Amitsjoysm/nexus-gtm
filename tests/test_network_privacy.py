from __future__ import annotations

import uuid
from datetime import datetime, timezone

from tests.conftest import make_tenant, tenant_session


async def _member(ts, *, pooling):
    from nexus.models.identity import Membership, User
    from nexus.models.network import NetworkSourceAccount

    u = User(email=f"u{uuid.uuid4().hex}@x.com", full_name="U", password_hash="x")
    ts.session.add(u)
    await ts.session.flush()
    m = Membership(user_id=u.id, role="rep")
    ts.add(m)
    await ts.session.flush()
    acc = NetworkSourceAccount(
        member_id=m.id, user_id=u.id, provider="fixture",
        external_account_id=u.email, pooling_enabled=pooling,
    )
    ts.add(acc)
    await ts.session.flush()
    return m, acc


async def test_non_pooled_edges_are_invisible_to_other_members():
    from nexus.models.network import NetworkPerson
    from nexus.network.connectors.base import NetworkSyncBatch, RawIdentity
    from nexus.network.intro import intro_paths
    from nexus.network.search import search_network
    from nexus.network.service import ingest_batch, set_pooling

    now = datetime(2026, 6, 29, tzinfo=timezone.utc)
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        alice, alice_acc = await _member(ts, pooling=False)  # private
        bob, _ = await _member(ts, pooling=False)
        await ingest_batch(ts, alice_acc, NetworkSyncBatch(
            identities=[RawIdentity(external_id="t1", email="t@corp.com", name="Tess",
                                    title="VP", company="Corp")],
        ), now=now)
        person = (await ts.list(NetworkPerson))[0]

        # Alice (owner) sees it; Bob does not.
        assert len(await intro_paths(ts, person_id=person.id, member_id=alice.id)) == 1
        assert len(await intro_paths(ts, person_id=person.id, member_id=bob.id)) == 0
        assert len(await search_network(ts, member_id=bob.id, query="VP Corp")) == 0

        # Alice opts into pooling → Bob can now see + intro through her.
        await set_pooling(ts, alice_acc, True)
        assert len(await intro_paths(ts, person_id=person.id, member_id=bob.id)) == 1
        hits = await search_network(ts, member_id=bob.id, query="VP Corp")
        assert hits and hits[0].broker_member_ids == [alice.id]


async def test_graph_is_tenant_isolated():
    from nexus.models.network import NetworkPerson
    from nexus.network.connectors.base import NetworkSyncBatch, RawIdentity
    from nexus.network.service import ingest_batch

    t1 = await make_tenant(slug="t1")
    t2 = await make_tenant(slug="t2")
    async with tenant_session(t1) as ts:
        _, acc = await _member(ts, pooling=True)
        await ingest_batch(ts, acc, NetworkSyncBatch(
            identities=[RawIdentity(external_id="x", email="a@a.com", name="A")]
        ))
    async with tenant_session(t2) as ts:
        assert await ts.list(NetworkPerson) == []  # t2 cannot see t1's graph
