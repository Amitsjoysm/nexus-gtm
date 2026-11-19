"""The discovery exclusion list (asked 2026-11-19).

"What if an enterprise has 2,000+ companies and an exclusion list of domains?" Domains on the list
(customers, competitors, partners) are never offered by discovery, through the same table and the
same checks as "Don't suggest these again" (`accounts.suppression`), so Populate, the daily sweep,
the Orchestrator and Parallel all honour it with no change of their own. Accounts the workspace
holds are not touched, and a list of thousands is read in slices.
"""
from __future__ import annotations

from sqlalchemy import select

from nexus.accounts.suppression import read_entry
from tests.conftest import auth, signup, tenant_session


def test_an_entry_is_read_as_the_company_domain_it_names():
    assert read_entry("acme.com") == "acme.com"
    assert read_entry("https://www.Acme.com/pricing") == "acme.com"
    assert read_entry("@globex.io") == "globex.io"
    assert read_entry("jane.doe@initech.com") == "initech.com"
    assert read_entry(" \"hooli.io\", ") == "hooli.io"
    for unreadable in ("", "gmail.com", "someone@gmail.com", "not a domain", "localhost"):
        assert read_entry(unreadable) == "", unreadable


async def test_a_pasted_list_is_added_reported_and_read_back(client):
    token = await signup(client, slug="dx1", email="o@dx1.io", company="DX1")
    h = auth(token)
    r = await client.post("/api/discovery/exclusions", headers=h, json={"entries": [
        "acme.com", "https://www.acme.com", "@globex.io", "cfo@initech.com",
        "someone@gmail.com", "nonsense", "",
    ]})
    assert r.status_code == 200, r.text
    out = r.json()
    assert (out["added"], out["already"], out["total"]) == (3, 0, 3)
    assert out["unreadable"] == ["someone@gmail.com", "nonsense"]

    again = (await client.post("/api/discovery/exclusions", headers=h,
                               json={"entries": ["acme.com", "hooli.io"]})).json()
    assert (again["added"], again["already"], again["total"]) == (1, 1, 4)

    listed = (await client.get("/api/discovery/exclusions", headers=h,
                               params={"q": "ACME"})).json()
    assert listed["total"] == 1 and listed["items"][0]["domain"] == "acme.com"
    assert listed["items"][0]["reason"] == "excluded"

    gone = await client.post("/api/discovery/exclusions/remove", headers=h,
                             json={"domains": ["https://acme.com", "nobody.io"]})
    assert gone.json() == {"removed": 1}
    assert (await client.get("/api/discovery/exclusions", headers=h)).json()["total"] == 3


async def test_a_list_of_thousands_is_taken_in_one_request(client):
    token = await signup(client, slug="dx2", email="o@dx2.io", company="DX2")
    entries = [f"company{i}.com" for i in range(2500)]
    r = await client.post("/api/discovery/exclusions", headers=auth(token),
                          json={"entries": entries})
    assert r.status_code == 200, r.text
    assert r.json()["added"] == 2500 and r.json()["total"] == 2500
    too_many = await client.post("/api/discovery/exclusions", headers=auth(token),
                                 json={"entries": entries * 3})
    assert too_many.status_code == 422, "the screen sends a long list in slices"


async def test_discovery_never_offers_an_excluded_company(client):
    from nexus.companies.resolution import company_id_for
    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.company import Company
    from nexus.prospecting.companies import ProspectQuery, _held, _matching

    token = await signup(client, slug="dx3", email="o@dx3.io", company="DX3")
    async with get_platform_sessionmaker()() as s:
        for domain in ("acme-excluded.io", "acme-open.io"):
            s.add(Company(id=company_id_for(domain), domain=domain, name=domain,
                          linkedin_industry_id=4))
        await s.commit()
    r = await client.post("/api/discovery/exclusions", headers=auth(token),
                          json={"entries": ["acme-excluded.io"]})
    assert r.json()["added"] == 1

    from nexus.models.records import DiscoverySuppression

    async with get_platform_sessionmaker()() as s:
        tid = (await s.scalars(select(DiscoverySuppression.tenant_id).where(
            DiscoverySuppression.domain == "acme-excluded.io"))).one()
    async with tenant_session(tid) as ts:
        assert await _held(ts, ["acme-excluded.io", "acme-open.io"]) == {"acme-excluded.io"}
        offered = {c.domain for c in (await ts.session.scalars(
            _matching(ts, ProspectQuery(industry_ids=(4,)), set()))).all()}
        assert "acme-open.io" in offered and "acme-excluded.io" not in offered


async def test_a_rep_may_read_the_list_and_not_change_it(client):
    from nexus.core.security import create_access_token
    from tests.conftest import make_tenant

    tid = await make_tenant("dx4", "DX4")
    rep = auth(create_access_token(user_id="u-rep", tenant_id=tid, role="rep"))
    assert (await client.get("/api/discovery/exclusions", headers=rep)).status_code == 200
    refused = await client.post("/api/discovery/exclusions", headers=rep,
                                json={"entries": ["acme.com"]})
    assert refused.status_code == 403
