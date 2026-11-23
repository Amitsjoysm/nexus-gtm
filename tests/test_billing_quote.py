"""What a set of charges would cost and whether this workspace may spend it, before anything is
bought (MCP previews, 2026-11-20)."""
from __future__ import annotations

import uuid

import pytest

from nexus.core.config import get_settings
from tests.conftest import auth, principal_from_token, put_on_plan, signup, tenant_session


@pytest.fixture(autouse=True)
async def _billing_seeded():
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.billing.rates import sync_rates

    await sync_catalog()
    await sync_plans()
    await sync_rates()


async def _paid_workspace(client, slug):
    token = await signup(client, slug=slug, email=f"o@{slug}.io", company=slug.upper())
    tid = principal_from_token(token).tenant_id
    await put_on_plan(tid, "launch")
    return auth(token), tid


async def test_a_quote_prices_from_the_rate_card_and_spends_nothing(client, monkeypatch):
    from nexus.billing.credits import balance

    monkeypatch.setattr(get_settings(), "billing_enforcement", "on")
    h, tid = await _paid_workspace(client, "bq1")
    async with tenant_session(tid) as ts:
        before = await balance(ts)
    r = await client.get("/api/billing/quote", headers=h,
                         params={"capability": "enrich.contact", "quantity": 3})
    assert r.status_code == 200, r.text
    q = r.json()
    assert q["uses_credits"] is True and q["covered"] is True
    assert q["credits"] == q["lines"]["enrich.contact"] > 0
    assert q["balance"] == before
    async with tenant_session(tid) as ts:
        assert await balance(ts) == before, "a quote never burns"


async def test_a_short_balance_reads_not_covered_with_the_reason(client, monkeypatch):
    from nexus.billing.credits import balance, burn_credits

    monkeypatch.setattr(get_settings(), "billing_enforcement", "on")
    h, tid = await _paid_workspace(client, "bq2")
    async with tenant_session(tid) as ts:
        await burn_credits(ts, await balance(ts), reason="test", idempotency_key=uuid.uuid4().hex)
    q = (await client.get("/api/billing/quote", headers=h,
                          params={"capability": "enrich.contact", "quantity": 1})).json()
    assert q["covered"] is False and "credits" in q["reason"].lower()


async def test_an_unknown_capability_is_a_422(client):
    token = await signup(client, slug="bq3", email="o@bq3.io", company="BQ3")
    r = await client.get("/api/billing/quote", headers=auth(token),
                         params={"capability": "not.a.thing", "quantity": 1})
    assert r.status_code == 422
