"""Contact-level look-alike finder — "find more people like this person".

Ranks the other people in the workspace by how closely they resemble a seed contact (role /
seniority / department) and how similar their company is. Deterministic and offline (ranks
existing book contacts, no network). Covers the service ranking, tenant isolation, and the API.
"""
from __future__ import annotations

from nexus.lookalike import get_contact_lookalike_service
from nexus.models.account import Account, Contact
from tests.conftest import auth, make_tenant, signup, tenant_session


async def _seed_book(ts, tenant_id):
    """A small book: a fintech seed account + a peer fintech, and a far-off agriculture co."""
    fintech = Account(tenant_id=tenant_id, name="Acme Payments", domain="acme.co",
                      industry="Fintech", employee_count=200, country="USA")
    fintech.custom_fields = {"sub_industry": "payments", "city": "San Francisco"}
    peer = Account(tenant_id=tenant_id, name="Globex Spend", domain="globex.com",
                   industry="Fintech", employee_count=240, country="USA")
    peer.custom_fields = {"sub_industry": "payments", "city": "San Francisco"}
    farm = Account(tenant_id=tenant_id, name="Tractor Supply", domain="tractor.co",
                   industry="Agriculture", employee_count=12000, country="India")
    ts.add_all([fintech, peer, farm])
    await ts.flush()
    return fintech, peer, farm


async def test_service_ranks_closest_person_first():
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        fintech, peer, farm = await _seed_book(ts, tid)
        seed = Contact(tenant_id=tid, account_id=fintech.id, full_name="Jane Doe",
                       title="VP of Sales", seniority="vp")
        twin = Contact(tenant_id=tid, account_id=peer.id, full_name="John Roe",
                       title="VP of Sales", seniority="vp")  # same role, peer company
        weak = Contact(tenant_id=tid, account_id=farm.id, full_name="Bob Low",
                       title="Junior Field Technician", seniority="associate")  # far company + role
        ts.add_all([seed, twin, weak])
        await ts.flush()

        out = await get_contact_lookalike_service().find(ts, seed, limit=10)

    ids = [lk.contact_id for lk in out]
    assert seed.id not in ids  # never returns the seed itself
    assert ids[0] == twin.id  # closest person ranked first
    by_id = {lk.contact_id: lk for lk in out}
    assert by_id[twin.id].account_name == "Globex Spend"
    assert by_id[twin.id].score > by_id[weak.id].score


async def test_service_empty_when_no_other_contacts():
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acc = Account(tenant_id=tid, name="Solo", domain="solo.co")
        ts.add(acc)
        await ts.flush()
        only = Contact(tenant_id=tid, account_id=acc.id, full_name="Only One", title="CEO")
        ts.add(only)
        await ts.flush()
        assert await get_contact_lookalike_service().find(ts, only) == []


async def test_service_isolates_tenants():
    """A contact in tenant A must never surface a look-alike from tenant B."""
    a = await make_tenant(slug="a", name="A")
    b = await make_tenant(slug="b", name="B")
    async with tenant_session(a) as ts:
        acc_a = Account(tenant_id=a, name="A Co", domain="a.co", industry="Fintech")
        ts.add(acc_a)
        await ts.flush()
        seed = Contact(tenant_id=a, account_id=acc_a.id, full_name="A Person", title="VP Sales")
        ts.add(seed)
        await ts.flush()
    async with tenant_session(b) as ts:
        acc_b = Account(tenant_id=b, name="B Co", domain="b.co", industry="Fintech")
        ts.add(acc_b)
        await ts.flush()
        ts.add(Contact(tenant_id=b, account_id=acc_b.id, full_name="B Person", title="VP Sales"))
        await ts.flush()

    async with tenant_session(a) as ts:
        seed_a = await ts.get(Contact, seed.id)
        out = await get_contact_lookalike_service().find(ts, seed_a)
    assert out == []  # tenant A's book has only the seed; B's person is invisible


# ---- API -------------------------------------------------------------------------------------

async def test_contact_lookalikes_endpoint(client):
    h = auth(await signup(client))
    acc = (await client.post("/api/accounts", headers=h,
                             json={"name": "Acme", "domain": "acme.co"})).json()
    seed = (await client.post(f"/api/accounts/{acc['id']}/contacts", headers=h,
                              json={"full_name": "Jane", "title": "VP of Sales", "seniority": "vp"})).json()
    twin = (await client.post(f"/api/accounts/{acc['id']}/contacts", headers=h,
                              json={"full_name": "John", "title": "VP of Sales", "seniority": "vp"})).json()
    await client.post(f"/api/accounts/{acc['id']}/contacts", headers=h,
                      json={"full_name": "Bob", "title": "Junior Analyst", "seniority": "associate"})

    r = await client.post(f"/api/accounts/contacts/{seed['id']}/lookalikes", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["seed_contact_id"] == seed["id"]
    ids = [lk["contact_id"] for lk in body["lookalikes"]]
    assert seed["id"] not in ids
    assert ids[0] == twin["id"]  # the matching VP ranks first
    assert body["lookalikes"][0]["reasons"]  # human-readable why


async def test_contact_lookalikes_endpoint_unknown_404(client):
    h = auth(await signup(client))
    r = await client.post("/api/accounts/contacts/nope/lookalikes", headers=h)
    assert r.status_code == 404
