from __future__ import annotations

from datetime import datetime, timezone

from tests.conftest import make_tenant, tenant_session


async def _seed_account(ts, *, pooling=False):
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
        external_account_id=f"{u.email}", pooling_enabled=pooling,
    )
    ts.add(acc)
    await ts.session.flush()
    return acc


def test_parse_query_extracts_titles_and_keywords():
    from nexus.network.search import parse_query

    q = parse_query("Find me a CTO at healthcare startups in New York")
    assert "cto" in q.titles
    assert "healthcare" in q.keywords
    assert "find" not in q.keywords  # stopword


async def test_search_ranks_visible_people_by_match_and_strength():
    from nexus.network.connectors.base import NetworkSyncBatch, RawIdentity, Touchpoint
    from nexus.network.search import search_network
    from nexus.network.service import ingest_batch

    now = datetime(2026, 6, 29, tzinfo=timezone.utc)
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acc = await _seed_account(ts, pooling=True)
        await ingest_batch(
            ts, acc,
            NetworkSyncBatch(
                identities=[
                    RawIdentity(external_id="g1", email="ann@health.com", name="Ann Lee",
                                title="CTO", company="HealthCo"),
                    RawIdentity(external_id="g2", email="bob@bank.com", name="Bob Roy",
                                title="CFO", company="BankCo"),
                ],
                touchpoints=[Touchpoint(person_external_id="g1", kind="email_sent", at=now)],
            ),
            now=now,
        )
        hits = await search_network(ts, member_id=acc.member_id, query="CTO at HealthCo")
        assert hits[0].person.full_name == "Ann Lee"
        assert hits[0].broker_member_ids == [acc.member_id]
        assert hits[0].best_strength > 0


async def test_search_breaks_ties_by_connection_strength():
    """Two people who match the query EQUALLY are ordered by best visible connection strength."""
    from datetime import datetime, timezone

    from nexus.network.connectors.base import NetworkSyncBatch, RawIdentity, Touchpoint
    from nexus.network.search import search_network
    from nexus.network.service import ingest_batch

    now = datetime(2026, 6, 29, tzinfo=timezone.utc)
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acc = await _seed_account(ts, pooling=True)
        # Both are "CTO at DataCo" → identical keyword match. Ann has a recent two-way thread
        # (strong); Bob is a cold contact (weak). Strength must break the tie → Ann first.
        await ingest_batch(ts, acc, NetworkSyncBatch(
            identities=[
                RawIdentity(external_id="a", email="ann@dataco.com", name="Ann", title="CTO",
                            company="DataCo"),
                RawIdentity(external_id="b", email="bob@dataco.com", name="Bob", title="CTO",
                            company="DataCo"),
            ],
            touchpoints=[
                Touchpoint(person_external_id="a", kind="email_sent", at=now),
                Touchpoint(person_external_id="a", kind="email_received", at=now),
            ],
        ), now=now)

        hits = await search_network(ts, member_id=acc.member_id, query="CTO at DataCo")
        names = [h.person.full_name for h in hits]
        assert names == ["Ann", "Bob"]  # equal match, higher strength first
        assert hits[0].best_strength > hits[1].best_strength
