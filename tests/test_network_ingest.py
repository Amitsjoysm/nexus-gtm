from __future__ import annotations

from datetime import datetime, timezone

import pytest


def test_sync_batch_dtos_construct():
    from nexus.network.connectors.base import NetworkSyncBatch, RawIdentity, Touchpoint

    batch = NetworkSyncBatch(
        identities=[RawIdentity(external_id="g1", email="A@Acme.com", name="Ann", title="CTO")],
        touchpoints=[
            Touchpoint(person_external_id="g1", kind="email_sent",
                       at=datetime(2026, 6, 1, tzinfo=timezone.utc))
        ],
        next_cursor="cursor-2",
    )
    assert batch.identities[0].relation == "contact"  # default
    assert batch.next_cursor == "cursor-2"
    assert batch.touchpoints[0].kind == "email_sent"


async def test_fixture_connector_returns_its_batch():
    from nexus.network.connectors.base import NetworkSyncBatch, RawIdentity, SourceAccountRef
    from nexus.network.connectors.fixture import FixtureConnector

    canned = NetworkSyncBatch(identities=[RawIdentity(external_id="g1", name="Ann")])
    conn = FixtureConnector(canned)
    ref = SourceAccountRef(id="acc1", provider="fixture", external_account_id="rep@acme.com")

    out = await conn.fetch(ref, None)
    assert out.identities[0].external_id == "g1"
    tokens = await conn.complete_auth("ignored")
    assert tokens.access_token == "fixture-token"


def test_registry_returns_fixture_and_override():
    from nexus.network.connectors.base import NetworkSyncBatch, RawIdentity
    from nexus.network.connectors.fixture import FixtureConnector
    from nexus.network.connectors.registry import (
        get_network_connector,
        set_network_connector,
    )

    assert get_network_connector("fixture").provider == "fixture"
    with pytest.raises(ValueError):
        get_network_connector("nope")

    override = FixtureConnector(NetworkSyncBatch(identities=[RawIdentity(external_id="x")]))
    set_network_connector(override)
    try:
        # the override short-circuits the registry for any provider name
        assert get_network_connector("google") is override
        assert get_network_connector("microsoft") is override
    finally:
        set_network_connector(None)
    # cleared → registry behaviour restored
    assert get_network_connector("fixture").provider == "fixture"


from tests.conftest import make_tenant, tenant_session


async def _seed_account(ts, *, pooling=False):
    """Create a User+Membership+NetworkSourceAccount; return the account."""
    import uuid

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
        external_account_id="rep@acme.com", pooling_enabled=pooling,
    )
    ts.add(acc)
    await ts.session.flush()
    return acc


async def test_ingest_batch_creates_persons_edges_and_materializes_strength():
    from nexus.models.network import NetworkEdge, NetworkPerson
    from nexus.network.connectors.base import NetworkSyncBatch, RawIdentity, Touchpoint
    from nexus.network.service import ingest_batch

    now = datetime(2026, 6, 29, tzinfo=timezone.utc)
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acc = await _seed_account(ts, pooling=True)
        batch = NetworkSyncBatch(
            identities=[
                RawIdentity(external_id="g1", email="ann@acme.com", name="Ann", title="CTO"),
                RawIdentity(external_id="g2", email="bob@globex.com", name="Bob", title="VP"),
            ],
            touchpoints=[
                Touchpoint(person_external_id="g1", kind="email_sent", at=now),
                Touchpoint(person_external_id="g1", kind="email_received", at=now),
            ],
            next_cursor="c2",
        )
        res = await ingest_batch(ts, acc, batch, now=now)
        assert res == {"identities": 2, "new_persons": 2, "new_edges": 2}

        people = await ts.list(NetworkPerson)
        assert {p.primary_email for p in people} == {"ann@acme.com", "bob@globex.com"}

        edges = await ts.list(NetworkEdge)
        ann_edge = next(e for e in edges if e.email_count == 2)
        assert ann_edge.relation == "email"
        assert ann_edge.strength == 69  # email 20 + recency 30 + freq 4 + reciprocity 15
        assert ann_edge.pooling_enabled is True  # mirrored from the source account

        # re-ingesting the same batch is idempotent (no duplicate persons/edges)
        res2 = await ingest_batch(ts, acc, batch, now=now)
        assert res2 == {"identities": 2, "new_persons": 0, "new_edges": 0}
        assert len(await ts.list(NetworkPerson)) == 2
        assert len(await ts.list(NetworkEdge)) == 2
        assert acc.sync_cursor == "c2"


async def test_ingest_clamps_oversized_strings():
    from nexus.models.network import NetworkIdentity, NetworkPerson
    from nexus.network.connectors.base import NetworkSyncBatch, RawIdentity
    from nexus.network.service import ingest_batch

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acc = await _seed_account(ts)
        batch = NetworkSyncBatch(
            identities=[RawIdentity(external_id="big", email="x@y.com",
                                    name="N" * 400, title="T" * 400, company="C" * 400)]
        )
        await ingest_batch(ts, acc, batch)
        person = (await ts.list(NetworkPerson))[0]
        ident = (await ts.list(NetworkIdentity))[0]
        assert len(person.full_name) == 200
        assert len(person.title) == 200
        assert len(person.company) == 200
        assert len(person.search_text) <= 600
        assert len(ident.name) == 200


async def test_sync_job_pulls_from_connector_and_ingests():
    from nexus.models.network import NetworkPerson
    from nexus.network.connectors.base import NetworkSyncBatch, RawIdentity
    from nexus.network.connectors.fixture import FixtureConnector
    from nexus.network.connectors.registry import set_network_connector
    from nexus.workers.tasks import handle_sync_network_account

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acc = await _seed_account(ts)
        acc_id = acc.id

    set_network_connector(
        FixtureConnector(NetworkSyncBatch(
            identities=[RawIdentity(external_id="g9", email="zoe@acme.com", name="Zoe")],
            next_cursor="cz",
        ))
    )
    try:
        res = await handle_sync_network_account({"tenant_id": tid, "account_id": acc_id})
    finally:
        set_network_connector(None)

    assert res["new_persons"] == 1
    async with tenant_session(tid) as ts:
        assert len(await ts.list(NetworkPerson)) == 1
