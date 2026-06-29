"""End-to-end API tests over the ASGI app (offline: stub LLM, demo signals)."""
from __future__ import annotations

import httpx
import pytest_asyncio

from nexus.main import create_app


@pytest_asyncio.fixture
async def client():
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _signup(client, slug="acme", email="rep@acme.com"):
    r = await client.post("/api/auth/signup", json={
        "company_name": "Acme", "company_slug": slug, "full_name": "Rep",
        "email": email, "password": "password123"})
    assert r.status_code == 201, r.text
    return r.json()["access_token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


async def test_health(client):
    r = await client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


async def test_signup_login_flow(client):
    await _signup(client)
    r = await client.post("/api/auth/login", json={
        "email": "rep@acme.com", "password": "password123"})
    assert r.status_code == 200
    assert r.json()["role"] == "owner"

    bad = await client.post("/api/auth/login", json={
        "email": "rep@acme.com", "password": "wrong"})
    assert bad.status_code == 401


async def test_duplicate_slug_rejected(client):
    await _signup(client, slug="acme", email="a@acme.com")
    r = await client.post("/api/auth/signup", json={
        "company_name": "Acme2", "company_slug": "acme", "full_name": "X",
        "email": "b@acme.com", "password": "password123"})
    assert r.status_code == 409


async def test_unauthenticated_is_rejected(client):
    r = await client.get("/api/accounts")
    assert r.status_code in (401, 403)


async def test_full_pipeline_creates_inbox(client):
    token = await _signup(client)
    h = _auth(token)

    # Define the relevance profile so scoring is grounded.
    await client.put("/api/relevance/profile", headers=h, json={
        "icp": {"industries": ["Software"], "employee_min": 100, "employee_max": 1000},
        "value_props": [{"name": "Speed", "description": "x", "pains_solved": ["slow"]}],
        "product_context": "GTM platform"})

    acc = (await client.post("/api/accounts", headers=h, json={
        "name": "Globex", "domain": "globex.com", "industry": "Software",
        "employee_count": 500, "country": "US", "tech_stack": ["snowflake"]})).json()

    pipe = await client.post(f"/api/agents/pipeline/{acc['id']}", headers=h)
    assert pipe.status_code == 200
    assert pipe.json()["new_signals"] >= 1
    assert pipe.json()["scoring_status"] == "completed"

    inbox = await client.get("/api/inbox", headers=h)
    assert inbox.status_code == 200
    assert len(inbox.json()) >= 1
    # Tasks come back ordered by priority desc.
    pris = [t["priority"] for t in inbox.json()]
    assert pris == sorted(pris, reverse=True)


async def test_tenant_isolation_through_api(client):
    t1 = await _signup(client, slug="alpha", email="a@alpha.com")
    t2 = await _signup(client, slug="beta", email="b@beta.com")

    acc = await client.post("/api/accounts", headers=_auth(t1), json={
        "name": "Alpha Co", "domain": "alpha.co"})
    acc_id = acc.json()["id"]

    # Tenant 2 must not see or fetch tenant 1's account.
    assert (await client.get("/api/accounts", headers=_auth(t2))).json() == []
    assert (await client.get(f"/api/accounts/{acc_id}", headers=_auth(t2))).status_code == 404
    assert (await client.get(f"/api/accounts/{acc_id}", headers=_auth(t1))).status_code == 200


async def test_run_named_agent(client):
    token = await _signup(client)
    h = _auth(token)
    acc = (await client.post("/api/accounts", headers=h, json={
        "name": "Globex", "domain": "globex.com", "industry": "Software",
        "employee_count": 500})).json()
    r = await client.post("/api/agents/research/run", headers=h,
                          json={"account_id": acc["id"], "inputs": {}})
    assert r.status_code == 200
    assert r.json()["status"] == "completed"
