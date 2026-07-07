from __future__ import annotations

import uuid
from datetime import datetime, timezone

from tests.conftest import make_tenant, tenant_session


async def _member(ts, *, pooling=False):
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


async def test_intro_paths_rank_visible_brokers_by_strength():
    from nexus.models.network import NetworkPerson
    from nexus.network.connectors.base import NetworkSyncBatch, RawIdentity, Touchpoint
    from nexus.network.intro import intro_paths
    from nexus.network.service import ingest_batch

    now = datetime(2026, 6, 29, tzinfo=timezone.utc)
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        # Two members both know Target. Alice (pooled) has a recent two-way thread → strong.
        # Bob (pooled) only has a cold contact → weak.
        alice, alice_acc = await _member(ts, pooling=True)
        bob, bob_acc = await _member(ts, pooling=True)
        target = RawIdentity(external_id="t1", email="target@corp.com", name="Tess Target",
                             title="VP Procurement", company="Corp")

        await ingest_batch(ts, alice_acc, NetworkSyncBatch(
            identities=[target],
            touchpoints=[
                Touchpoint(person_external_id="t1", kind="email_sent", at=now),
                Touchpoint(person_external_id="t1", kind="email_received", at=now),
            ],
        ), now=now)
        await ingest_batch(ts, bob_acc, NetworkSyncBatch(identities=[target]), now=now)

        person = (await ts.list(NetworkPerson))[0]
        paths = await intro_paths(ts, person_id=person.id, member_id=alice.id)
        assert [p.broker_member_id for p in paths] == [alice.id, bob.id]  # strong first
        assert paths[0].strength > paths[1].strength
