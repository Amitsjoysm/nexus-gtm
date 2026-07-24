from __future__ import annotations

import uuid


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


async def test_resolution_dedupes_by_email_and_creates_on_miss():
    from nexus.models.network import NetworkPerson
    from nexus.network.resolution import resolution_key, resolve_person

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        # case-insensitive email dedupe → same person
        p1 = await resolve_person(ts, email="Ann@Acme.com", name="Ann Lee", title="CTO",
                                  company="Acme")
        p2 = await resolve_person(ts, email="ann@acme.com", name="A. Lee", title="Chief Tech",
                                  company="Acme")
        assert p1.id == p2.id
        assert p1.primary_email == "ann@acme.com"

        # no email: name+company dedupe
        q1 = await resolve_person(ts, email=None, name="Bob Roy", title="VP", company="Globex")
        q2 = await resolve_person(ts, email=None, name="bob roy", title="VP Sales",
                                  company="globex")
        assert q1.id == q2.id

        # different person, same company → NOT merged (conservative)
        r = await resolve_person(ts, email=None, name="Carol Diaz", title="VP", company="Globex")
        assert r.id != q1.id

        assert len(await ts.list(NetworkPerson)) == 3

    # resolution_key is the normalized email when present, else a name|company hash
    assert resolution_key(email="A@B.com", name="x", company="y") == "a@b.com"
    assert resolution_key(email=None, name="A", company="B").startswith("h:")
