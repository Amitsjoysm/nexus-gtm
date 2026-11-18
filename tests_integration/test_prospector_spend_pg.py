"""Prospector's paid actions through the real API on Postgres under RLS, for a Free workspace.

Reported from staging, 2026-11-18: a workspace showing 200 credits could not save, list, export or
reveal: the confirm step said it had no credits. The offline suite runs these on SQLite, which has
no RLS, so a read that loses its tenant there still returns rows. This drives the same requests the
screen sends, as the least-privilege app role, with a fake provider.
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

from tests.prospector_fixtures import FakeVendor
from tests_integration.conftest import requires_pg
from tests_integration.rls_harness import app_client, rls_database, seed_billing

pytestmark = [pytest.mark.asyncio, requires_pg]


@pytest_asyncio.fixture
async def spend_db(monkeypatch):
    """Its own database and role: the integration job runs files in parallel."""
    async with rls_database(monkeypatch, database="nexus_prospector_spend_test",
                            role="nexus_app_prospector_spend",
                            password="prospector-spend-test-password") as owner_url:
        await seed_billing()
        yield owner_url


@pytest.fixture
def vendor():
    from nexus.prospector import search
    from nexus.prospector.client import InfoJoyClient

    fake = FakeVendor()
    search.set_client(InfoJoyClient(keys=["k"], transport=fake.transport(), backoff_s=0))
    yield fake
    search.set_client(None)


async def _signup(c, slug: str) -> dict:
    r = await c.post("/api/auth/signup", json={
        "company_name": slug.upper(), "company_slug": slug, "full_name": "Rep",
        "email": f"owner@{slug}.example.com", "password": "password123"})
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _forget_pages() -> None:
    """Empty the shared page cache, so the next request asks the provider again."""
    from sqlalchemy import delete

    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.prospector import ProspectorPage

    async with get_platform_sessionmaker()() as session:
        await session.execute(delete(ProspectorPage))
        await session.commit()


def _selection(rows, view="contacts"):
    return {"view": view, "scope": "total", "filters": {"titles": ["VP Sales"]},
            "rows": [{"api_page": r["api_page"], "api_page_size": r["api_page_size"],
                      "row_key": r["row_key"]} for r in rows]}


async def test_a_free_workspace_can_price_save_list_and_export(spend_db, vendor):
    async with app_client() as c:
        h = await _signup(c, "pxspend")
        search = await c.post("/api/prospector/search", headers=h, json={
            "view": "contacts", "scope": "total", "filters": {"titles": ["VP Sales"]},
            "page": 1, "page_size": 25})
        assert search.status_code == 200, search.text
        rows = search.json()["rows"]
        assert rows

        await _forget_pages()
        quote = await c.post("/api/prospector/quote", headers=h, json={
            "selection": _selection(rows), "action": "save", "include_phones": True})
        assert quote.status_code == 200, quote.text
        q = quote.json()
        assert q["balance"] == pytest.approx(200), q
        assert q["total_credits"] > 0 and q["enough"] is True, q

        made = await c.post("/api/lists", headers=h, json={"name": "Cold", "kind": "contact"})
        assert made.status_code in (200, 201), made.text
        listed = await c.post("/api/prospector/deliver", headers=h, json={
            "selection": _selection(rows[:2]), "action": "list", "include_phones": False,
            "list_id": made.json()["id"], "request_id": uuid.uuid4().hex})
        assert listed.status_code == 200, listed.text
        assert listed.json()["listed"] == 2, listed.json()

        export = await c.post("/api/prospector/export", headers=h, json={
            "selection": _selection(rows[2:]), "include_phones": True,
            "request_id": uuid.uuid4().hex})
        assert export.status_code == 200, export.text
        assert export.text.count("\n") >= 2, export.text

        # Charged once: what was listed or exported is now held or paid for, and costs nothing.
        after = (await c.post("/api/prospector/quote", headers=h, json={
            "selection": _selection(rows), "action": "save", "include_phones": False})).json()
        assert after["total_credits"] == 0 and after["balance"] == pytest.approx(195), after
