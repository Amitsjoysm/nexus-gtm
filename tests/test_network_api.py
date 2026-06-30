from __future__ import annotations

import pytest

from tests.conftest import auth, client, signup


async def test_network_end_to_end_over_http(client):
    token = await signup(client, slug="acme", email="rep@acme.com", company="Acme")
    h = auth(token)

    # connect a fixture source
    r = await client.post("/api/network/accounts",
                          json={"provider": "fixture", "external_account_id": "rep@acme.com"},
                          headers=h)
    assert r.status_code == 201, r.text
    account_id = r.json()["id"]
    assert r.json()["pooling_enabled"] is False

    # import a batch inline
    r = await client.post(
        f"/api/network/accounts/{account_id}/import",
        json={
            "identities": [
                {"external_id": "g1", "email": "ann@health.com", "name": "Ann Lee",
                 "title": "CTO", "company": "HealthCo"},
            ],
            "touchpoints": [
                {"person_external_id": "g1", "kind": "email_sent", "at": "2026-06-29T00:00:00Z"},
            ],
        },
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert r.json()["new_persons"] == 1

    # search finds Ann
    r = await client.post("/api/network/search", json={"query": "CTO at HealthCo"}, headers=h)
    assert r.status_code == 200, r.text
    hits = r.json()
    assert hits[0]["person"]["full_name"] == "Ann Lee"
    person_id = hits[0]["person"]["id"]

    # intro paths attribute the broker
    r = await client.get(f"/api/network/people/{person_id}/intro-paths", headers=h)
    assert r.status_code == 200, r.text
    assert len(r.json()) == 1
    assert r.json()[0]["strength"] > 0

    # list accounts never leaks oauth
    r = await client.get("/api/network/accounts", headers=h)
    assert "oauth" not in r.json()[0]


async def test_network_rejects_unauthenticated(client):
    r = await client.get("/api/network/accounts")
    assert r.status_code in (401, 403)


async def test_patch_account_rejects_invalid_status(client):
    token = await signup(client, slug="acme2", email="rep@acme2.com", company="Acme2")
    h = auth(token)
    r = await client.post("/api/network/accounts",
                          json={"provider": "fixture", "external_account_id": "rep@acme2.com"},
                          headers=h)
    account_id = r.json()["id"]
    r = await client.patch(f"/api/network/accounts/{account_id}",
                           json={"status": "bogus"}, headers=h)
    assert r.status_code == 422  # Literal-constrained status


async def test_search_rejects_empty_query(client):
    token = await signup(client, slug="acme3", email="rep@acme3.com", company="Acme3")
    h = auth(token)
    r = await client.post("/api/network/search", json={"query": ""}, headers=h)
    assert r.status_code == 422  # min_length=1


async def test_cross_tenant_cannot_see_each_others_network(client):
    # Tenant A connects + imports a contact.
    ta = auth(await signup(client, slug="ta", email="rep@ta.com", company="TA"))
    r = await client.post("/api/network/accounts",
                          json={"provider": "fixture", "external_account_id": "rep@ta.com"},
                          headers=ta)
    a_acc = r.json()["id"]
    await client.post(f"/api/network/accounts/{a_acc}/import",
                      json={"identities": [{"external_id": "p1", "email": "x@ta.com",
                                            "name": "X Person", "title": "CTO"}]},
                      headers=ta)
    person_id = (await client.post("/api/network/search",
                                   json={"query": "CTO"}, headers=ta)).json()[0]["person"]["id"]

    # Tenant B sees no accounts, no search hits, and 404 on A's person id.
    tb = auth(await signup(client, slug="tb", email="rep@tb.com", company="TB"))
    assert (await client.get("/api/network/accounts", headers=tb)).json() == []
    assert (await client.post("/api/network/search", json={"query": "CTO"}, headers=tb)).json() == []
    assert (await client.get(f"/api/network/people/{person_id}", headers=tb)).status_code == 404
