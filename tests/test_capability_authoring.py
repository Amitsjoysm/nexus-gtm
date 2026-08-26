# tests/test_capability_authoring.py
"""Creating a billable capability from the Control plane.

`CAPABILITY_SEED` is a Python list and `sync_catalog()` inserts only what it names, so a new
billable action meant a code edit and a release. The table was always writable; the API was not.

Safe to add because `sync_catalog` never deletes and re-asserts managed fields only for ids it
seeds -- an admin-created capability is invisible to it, so no deploy can revert one.
"""
from __future__ import annotations

from tests.conftest import auth, signup


async def _admin(client, monkeypatch, *, slug: str, email: str) -> str:
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.billing.rates import sync_rates
    from nexus.core.config import get_settings

    await sync_catalog()
    await sync_plans()
    await sync_rates()
    monkeypatch.setattr(get_settings(), "platform_admin_emails", email)
    return await signup(client, slug=slug, email=email, company=slug.upper())


async def test_a_capability_can_be_created_and_priced_in_one_call(client, monkeypatch):
    """Priced in the SAME call on purpose.

    A capability with no rate card is metered and then rated at nothing: usage accumulates, quotas
    count down, and no revenue line ever appears. That shipped once already -- `ai.scoring`, 4,090
    runs, free.
    """
    token = await _admin(client, monkeypatch, slug="ca1", email="boss@ca1.com")
    r = await client.post("/api/admin/billing/capabilities", headers=auth(token), json={
        "id": "enrich.company_news", "name": "Company news lookup", "category": "enrich",
        "unit": "lookup", "credits_per_unit": 3, "unit_cost_usd": 0.009,
        "description": "Recent news for one company.",
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["capability_id"] == "enrich.company_news"
    assert body["priced"] is True
    assert body["gross_margin"] >= 0.5

    rates = (await client.get("/api/admin/billing/rates", headers=auth(token))).json()
    assert any(x["capability_id"] == "enrich.company_news" for x in rates)


async def test_creating_without_a_price_warns_loudly(client, monkeypatch):
    """Allowed, but never silent."""
    token = await _admin(client, monkeypatch, slug="ca2", email="boss@ca2.com")
    r = await client.post("/api/admin/billing/capabilities", headers=auth(token), json={
        "id": "report.usage_export", "name": "Usage export", "category": "report",
        "unit": "export",
    })
    assert r.status_code == 201, r.text
    assert r.json()["priced"] is False
    assert "rated at nothing" in r.json()["warning"]


async def test_a_module_gate_needs_no_price_and_gets_no_warning(client, monkeypatch):
    """A gate is on/off, not a unit of anything. Warning about its missing price would be noise,
    and noise is how a real warning gets ignored."""
    token = await _admin(client, monkeypatch, slug="ca9", email="boss@ca9.com")
    r = await client.post("/api/admin/billing/capabilities", headers=auth(token), json={
        "id": "module.reporting", "name": "Reporting module", "category": "module",
        "unit": "flag",
    })
    assert r.status_code == 201, r.text
    assert r.json()["warning"] == ""


async def test_a_below_floor_price_is_refused(client, monkeypatch):
    """The same guard as the rate endpoint. There must be no path -- seed, rate endpoint, or this
    one -- that lands an underwater price."""
    token = await _admin(client, monkeypatch, slug="ca3", email="boss@ca3.com")
    r = await client.post("/api/admin/billing/capabilities", headers=auth(token), json={
        "id": "enrich.expensive", "name": "Expensive", "category": "enrich", "unit": "call",
        "credits_per_unit": 1, "unit_cost_usd": 0.05,
    })
    assert r.status_code == 422
    assert "margin" in r.text.lower()


async def test_a_duplicate_id_is_refused(client, monkeypatch):
    token = await _admin(client, monkeypatch, slug="ca4", email="boss@ca4.com")
    r = await client.post("/api/admin/billing/capabilities", headers=auth(token), json={
        "id": "ai.email_draft", "name": "Dup", "category": "ai", "unit": "action",
    })
    assert r.status_code == 409


async def test_the_id_must_be_dotted_and_lowercase(client, monkeypatch):
    """Ids appear in URLs, entitlement rows, usage events and invoice lines. `category.name` is
    the shape every existing one uses and the shape the UI groups on."""
    token = await _admin(client, monkeypatch, slug="ca5", email="boss@ca5.com")
    for bad in ("NoDot", "Has Space.x", "trailing.", ".leading", "a.b.c"):
        r = await client.post("/api/admin/billing/capabilities", headers=auth(token), json={
            "id": bad, "name": "X", "category": "ai", "unit": "action",
        })
        assert r.status_code == 400, f"{bad} should be refused"


async def test_an_unknown_dependency_is_refused(client, monkeypatch):
    """`depends_on` gates this capability behind another. Naming one that does not exist produces
    a capability that can never resolve -- permanently unusable, and silently."""
    token = await _admin(client, monkeypatch, slug="ca6", email="boss@ca6.com")
    r = await client.post("/api/admin/billing/capabilities", headers=auth(token), json={
        "id": "ai.thing", "name": "Thing", "category": "ai", "unit": "action",
        "depends_on": ["module.nonexistent"],
    })
    assert r.status_code == 400
    assert "do not exist" in r.text


async def test_a_known_dependency_is_accepted(client, monkeypatch):
    token = await _admin(client, monkeypatch, slug="ca10", email="boss@ca10.com")
    r = await client.post("/api/admin/billing/capabilities", headers=auth(token), json={
        "id": "ai.gated_thing", "name": "Gated thing", "category": "ai", "unit": "action",
        "depends_on": ["module.agents"], "credits_per_unit": 2, "unit_cost_usd": 0.004,
    })
    assert r.status_code == 201, r.text


async def test_a_redeploy_does_not_disturb_an_admin_created_capability(client, monkeypatch):
    """`sync_catalog` re-asserts managed fields for SEEDED ids only. That is what makes this
    endpoint safe to add rather than a race with the next release."""
    from sqlalchemy import select

    from nexus.billing.catalog import sync_catalog
    from nexus.core.db import get_sessionmaker
    from nexus.models.billing import BillingCapability

    token = await _admin(client, monkeypatch, slug="ca7", email="boss@ca7.com")
    await client.post("/api/admin/billing/capabilities", headers=auth(token), json={
        "id": "enrich.custom_thing", "name": "Custom thing", "category": "enrich",
        "unit": "call", "credits_per_unit": 2, "unit_cost_usd": 0.005,
    })
    await sync_catalog()          # what a deploy does

    async with get_sessionmaker()() as s:
        row = (await s.scalars(select(BillingCapability).where(
            BillingCapability.id == "enrich.custom_thing"))).first()
    assert row is not None, "a deploy removed an admin-created capability"
    assert row.name == "Custom thing"
    assert row.unit == "call"


async def test_a_new_capability_can_be_entitled_on_a_plan_immediately(client, monkeypatch):
    """The point of the whole endpoint: created, priced and sellable without a release."""
    token = await _admin(client, monkeypatch, slug="ca11", email="boss@ca11.com")
    await client.post("/api/admin/billing/capabilities", headers=auth(token), json={
        "id": "enrich.sellable", "name": "Sellable", "category": "enrich", "unit": "call",
        "credits_per_unit": 3, "unit_cost_usd": 0.008,
    })
    r = await client.put("/api/admin/billing/plans/launch/entitlements/enrich.sellable",
                         headers=auth(token), json={"mode": "metered", "quota": 100})
    assert r.status_code == 200, r.text


async def test_a_tenant_owner_cannot_create_a_capability(client):
    token = await signup(client, slug="ca8", email="o@ca8.com", company="CA8")
    assert (await client.post("/api/admin/billing/capabilities", headers=auth(token), json={
        "id": "ai.x", "name": "X", "category": "ai", "unit": "action",
    })).status_code == 403
