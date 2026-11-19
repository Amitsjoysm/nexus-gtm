"""Two people in one workspace search Prospector at the same time, and neither waits on the other.

Reported 2026-11-19: "if one user is using Prospector, other users are not able to use it at the
same time". The provider takes seconds to answer (14-124 s on a cold start), so anything that held
a database lock or a connection across that wait would queue every colleague behind the first
search. This runs the real API on Postgres under RLS as the app role, with a provider that takes
`DELAY_S` to answer every request, and measures: concurrent searches finish in about one delay, not
one per person.
"""
from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest
import pytest_asyncio

from tests.prospector_fixtures import CONTACTS, page
from tests_integration.conftest import requires_pg
from tests_integration.rls_harness import app_client, rls_database, seed_billing

pytestmark = [pytest.mark.asyncio, requires_pg]

DELAY_S = 3.0
PEOPLE = 4


@pytest_asyncio.fixture
async def concurrency_db(monkeypatch):
    async with rls_database(monkeypatch, database="nexus_prospector_concurrency_test",
                            role="nexus_app_prospector_conc",
                            password="prospector-concurrency-password") as owner_url:
        await seed_billing()
        yield owner_url


@pytest.fixture
def slow_vendor():
    from nexus.prospector import search
    from nexus.prospector.client import InfoJoyClient

    seen: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(time.monotonic())
        await asyncio.sleep(DELAY_S)
        body = json.loads(request.content or b"{}")
        return httpx.Response(200, json=page(CONTACTS, number=int(body.get("PageNumber") or 1),
                                             size=int(body.get("PageSize") or 25)))

    search.set_client(InfoJoyClient(keys=["k"], transport=httpx.MockTransport(handler),
                                    backoff_s=0))
    yield seen
    search.set_client(None)


async def _colleagues(c, n: int) -> list[dict]:
    """The owner who signed up, plus n-1 members of the same workspace."""
    from nexus.core.db import get_platform_sessionmaker
    from nexus.core.security import create_access_token, hash_password
    from nexus.models.identity import Membership, User, Workspace

    r = await c.post("/api/auth/signup", json={
        "company_name": "Busy Co", "company_slug": "busy-co", "full_name": "Owner",
        "email": "owner@busy-co.example.com", "password": "password123"})
    assert r.status_code == 201, r.text
    tenant_id = r.json()["tenant_id"]
    headers = [{"Authorization": f"Bearer {r.json()['access_token']}"}]
    async with get_platform_sessionmaker()() as s:
        from sqlalchemy import select

        workspace = (await s.scalars(select(Workspace).where(
            Workspace.tenant_id == tenant_id))).first()
        for i in range(1, n):
            user = User(email=f"rep{i}@busy-co.example.com", full_name=f"Rep {i}",
                        password_hash=hash_password("password123"))
            s.add(user)
            await s.flush()
            s.add(Membership(tenant_id=tenant_id, user_id=user.id, workspace_id=workspace.id,
                             role="rep"))
            await s.flush()
            headers.append({"Authorization": "Bearer " + create_access_token(
                user_id=user.id, tenant_id=tenant_id, role="rep", token_version=0)})
        await s.commit()
    return headers


async def test_colleagues_searching_at_once_do_not_wait_for_each_other(concurrency_db,
                                                                        slow_vendor):
    async with app_client() as c:
        people = await _colleagues(c, PEOPLE)

        async def search(i: int, h: dict) -> int:
            # Different titles each: none is answered from another's cached page.
            r = await c.post("/api/prospector/search", headers=h, json={
                "view": "contacts", "filters": {"titles": [f"Title {i}"]}, "page_size": 25})
            return r.status_code

        started = time.monotonic()
        statuses = await asyncio.gather(*(search(i, h) for i, h in enumerate(people)))
        took = time.monotonic() - started

    assert statuses == [200] * PEOPLE, statuses
    # Every request reached the provider within moments of the first (none queued behind another).
    spread = max(slow_vendor) - min(slow_vendor)
    assert spread < DELAY_S / 2, f"provider calls were spaced {spread:.1f}s apart"
    assert took < DELAY_S * 2, f"{PEOPLE} people took {took:.1f}s: they waited for each other"
