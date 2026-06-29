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
