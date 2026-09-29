"""`GET /accounts?q=` searches the whole workspace, not the page already loaded.

The list endpoint returns at most 200 accounts, newest first. A picker that filtered those in the
browser (the list page's "Add companies") could never find the 201st account, however exactly it
was typed.
"""
from __future__ import annotations

from tests.conftest import auth, signup


async def test_q_matches_name_or_domain_case_insensitively(client):
    token = await signup(client, slug="accq", email="o@accq.io", company="AccQ")
    for name, domain in (("Acme Robotics", "acme.io"), ("Beta Labs", "betalabs.com"),
                         ("Gamma", "acmegamma.dev")):
        r = await client.post("/api/accounts", headers=auth(token),
                              json={"name": name, "domain": domain})
        assert r.status_code == 201, r.text

    r = await client.get("/api/accounts", headers=auth(token), params={"q": "ACME"})
    assert r.status_code == 200
    assert {a["name"] for a in r.json()} == {"Acme Robotics", "Gamma"}
    assert r.headers["X-Total-Count"] == "2"

    by_domain = await client.get("/api/accounts", headers=auth(token), params={"q": "betalabs"})
    assert [a["name"] for a in by_domain.json()] == ["Beta Labs"]

    everything = await client.get("/api/accounts", headers=auth(token))
    assert everything.headers["X-Total-Count"] == "3"
