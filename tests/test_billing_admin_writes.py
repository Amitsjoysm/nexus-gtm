# tests/test_billing_admin_writes.py
"""The admin write surface: pricing changes without a deploy, but only by a platform admin."""
from __future__ import annotations

from tests.conftest import auth, signup  # `client` is an auto-discovered conftest fixture


async def test_admin_writes_reject_a_tenant_owner(client):
    """A workspace owner is not a platform admin. Tenant RBAC must grant nothing here."""
    token = await signup(client, slug="aw1", email="o@aw1.com", company="AW1")
    r = await client.patch("/api/admin/billing/plans/growth",
                           headers=auth(token), json={"base_price_cents": 1})
    assert r.status_code in (401, 404)


async def test_admin_writes_reject_anonymous(client):
    r = await client.patch("/api/admin/billing/plans/growth", json={"base_price_cents": 1})
    assert r.status_code in (401, 404)


async def test_platform_admin_can_reprice_a_plan(client, monkeypatch):
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.core.config import get_settings

    await sync_catalog()
    await sync_plans()
    monkeypatch.setattr(get_settings(), "platform_admin_emails", "boss@nexus.com")
    token = await signup(client, slug="aw2", email="boss@nexus.com", company="AW2")

    r = await client.patch("/api/admin/billing/plans/growth", headers=auth(token),
                           json={"base_price_cents": 8900})
    assert r.status_code == 200, r.text
    assert r.json()["base_price_cents"] == 8900


async def test_rate_card_write_refuses_a_below_floor_price(client, monkeypatch):
    """The margin floor is enforced at the API too, not just in the seed — an admin must not be
    able to click past it without recording an exception."""
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.rates import sync_rates
    from nexus.core.config import get_settings

    await sync_catalog()
    await sync_rates()
    monkeypatch.setattr(get_settings(), "platform_admin_emails", "boss@nexus.com")
    token = await signup(client, slug="aw3", email="boss@nexus.com", company="AW3")

    # 1 credit = $0.01 against $0.012 COGS -> underwater.
    r = await client.put("/api/admin/billing/rates/ai.account_qa", headers=auth(token),
                         json={"credits_per_unit": 1})
    assert r.status_code == 422
    assert "margin" in r.text.lower()

    r = await client.put("/api/admin/billing/rates/ai.account_qa", headers=auth(token),
                         json={"credits_per_unit": 1, "margin_exception": True,
                               "margin_exception_reason": "strategic loss leader"})
    assert r.status_code == 200, r.text
    assert r.json()["margin_exception"] is True


async def test_credit_grant_is_idempotent(client, monkeypatch):
    """A double-clicked button must not mint credits twice."""
    from sqlalchemy import select

    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.core.config import get_settings
    from nexus.core.db import get_sessionmaker
    from nexus.models.identity import Tenant

    await sync_catalog()
    await sync_plans()
    monkeypatch.setattr(get_settings(), "platform_admin_emails", "boss@nexus.com")
    token = await signup(client, slug="aw4", email="boss@nexus.com", company="AW4")

    async with get_sessionmaker()() as s:
        tid = (await s.scalars(select(Tenant.id).where(Tenant.slug == "aw4"))).first()

    body = {"amount": 500, "reason": "goodwill", "idempotency_key": "goodwill-2026-07"}
    first = await client.post(f"/api/admin/billing/tenants/{tid}/credits",
                              headers=auth(token), json=body)
    assert first.status_code == 200, first.text
    assert first.json()["applied"] is True
    # The DELTA, not an absolute. A new workspace now starts on `free` and is granted that plan's
    # 200 included credits at signup, so an absolute assertion here was really asserting "nothing
    # else in the product ever grants credits" — which was never the property this test is about.
    # What it means is: the second click adds nothing.
    granted = first.json()["balance"]
    assert granted >= 500, f"the 500-credit grant did not land: balance {granted}"

    second = await client.post(f"/api/admin/billing/tenants/{tid}/credits",
                               headers=auth(token), json=body)
    assert second.status_code == 200, second.text
    assert second.json()["applied"] is False        # same key -> no new credits
    assert second.json()["balance"] == granted, "a repeated key minted credits a second time"


async def test_admin_can_move_a_tenant_between_plans(client, monkeypatch):
    from sqlalchemy import select

    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.core.config import get_settings
    from nexus.core.db import get_sessionmaker
    from nexus.models.identity import Tenant

    await sync_catalog()
    await sync_plans()
    monkeypatch.setattr(get_settings(), "platform_admin_emails", "boss@nexus.com")
    token = await signup(client, slug="aw5", email="boss@nexus.com", company="AW5")

    async with get_sessionmaker()() as s:
        tid = (await s.scalars(select(Tenant.id).where(Tenant.slug == "aw5"))).first()

    r = await client.post(f"/api/admin/billing/tenants/{tid}/subscription",
                          headers=auth(token), json={"plan_id": "growth"})
    assert r.status_code == 200, r.text
    assert r.json()["plan_id"] == "growth"

    r = await client.post(f"/api/admin/billing/tenants/{tid}/subscription",
                          headers=auth(token), json={"plan_id": "professional"})
    assert r.status_code == 200, r.text
    assert r.json()["plan_id"] == "professional"


async def test_unknown_plan_and_capability_are_rejected(client, monkeypatch):
    from nexus.billing.catalog import sync_catalog
    from nexus.core.config import get_settings

    await sync_catalog()
    monkeypatch.setattr(get_settings(), "platform_admin_emails", "boss@nexus.com")
    token = await signup(client, slug="aw6", email="boss@nexus.com", company="AW6")

    r = await client.patch("/api/admin/billing/plans/no-such-plan",
                           headers=auth(token), json={"base_price_cents": 1})
    assert r.status_code == 404

    r = await client.put("/api/admin/billing/rates/no.such.capability",
                         headers=auth(token), json={"credits_per_unit": 5})
    assert r.status_code == 404


# ---- audit trail ---------------------------------------------------------------------------
# docs/billing/17-Production-Checklist.md §Admin requires 100% of admin mutations captured.
# Without this, a platform admin can reprice a plan and nothing records who or when.

async def _admin(client, monkeypatch, slug: str):
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.billing.rates import sync_rates
    from nexus.core.config import get_settings

    await sync_catalog()
    await sync_plans()
    await sync_rates()
    monkeypatch.setattr(get_settings(), "platform_admin_emails", "boss@nexus.com")
    return await signup(client, slug=slug, email="boss@nexus.com", company=slug.upper())


async def _audit_rows(action: str | None = None):
    from sqlalchemy import select

    from nexus.core.db import get_sessionmaker
    from nexus.models.billing import BillingAuditLog

    async with get_sessionmaker()() as s:
        stmt = select(BillingAuditLog)
        if action:
            stmt = stmt.where(BillingAuditLog.action == action)
        return list((await s.scalars(stmt)).all())


async def test_plan_repricing_is_audited_with_before_and_after(client, monkeypatch):
    token = await _admin(client, monkeypatch, "au1")
    r = await client.patch("/api/admin/billing/plans/growth", headers=auth(token),
                           json={"base_price_cents": 9900})
    assert r.status_code == 200, r.text

    rows = await _audit_rows("plan.update")
    assert len(rows) == 1
    assert rows[0].target == "growth"
    assert rows[0].actor
    # The whole point: what it was, not just what it became.
    assert rows[0].before["base_price_cents"] == 7900
    assert rows[0].after["base_price_cents"] == 9900


async def test_rate_change_and_credit_grant_are_audited(client, monkeypatch):
    from sqlalchemy import select

    from nexus.core.db import get_sessionmaker
    from nexus.models.identity import Tenant

    token = await _admin(client, monkeypatch, "au2")
    async with get_sessionmaker()() as s:
        tid = (await s.scalars(select(Tenant.id).where(Tenant.slug == "au2"))).first()

    r = await client.put("/api/admin/billing/rates/ai.email_draft", headers=auth(token),
                         json={"credits_per_unit": 3})
    assert r.status_code == 200, r.text

    r = await client.post(f"/api/admin/billing/tenants/{tid}/credits", headers=auth(token),
                          json={"amount": 100, "reason": "goodwill",
                                "idempotency_key": "au2-1"})
    assert r.status_code == 200, r.text

    assert len(await _audit_rows("rate.upsert")) == 1
    grants = await _audit_rows("credits.grant")
    assert len(grants) == 1
    assert grants[0].subject_tenant_id == tid       # which customer was affected


async def test_audit_log_is_not_tenant_scoped():
    """It records actions ACROSS tenants, so it must not carry a `tenant_id` column — that is
    what `scripts/apply_rls.py` keys on, and an RLS policy would hide the log from the platform
    admins who are its only readers."""
    from nexus.models.billing import BillingAuditLog

    assert "tenant_id" not in BillingAuditLog.__table__.columns
    assert "subject_tenant_id" in BillingAuditLog.__table__.columns


async def test_a_failed_audit_write_does_not_roll_back_the_mutation(client, monkeypatch):
    """A billing change that succeeded must not be undone because its audit row failed."""
    import nexus.api.routers.admin_billing_write as mod

    token = await _admin(client, monkeypatch, "au3")

    async def failing(*a, **k):
        return False

    monkeypatch.setattr(mod, "record_admin_action", failing)
    r = await client.patch("/api/admin/billing/plans/starter", headers=auth(token),
                           json={"base_price_cents": 4900})
    assert r.status_code == 200
    assert r.json()["base_price_cents"] == 4900


# ---- cost rates ----------------------------------------------------------------------------------

async def _pricing_admin(client, monkeypatch, *, slug: str, email: str) -> str:
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.billing.rates import sync_rates
    from nexus.core.config import get_settings

    await sync_catalog()
    await sync_plans()
    await sync_rates()
    monkeypatch.setattr(get_settings(), "platform_admin_emails", email)
    return await signup(client, slug=slug, email=email, company=slug.upper())


async def test_a_cost_rise_is_recorded_even_when_it_breaks_the_margin(client, monkeypatch):
    """The one write in billing admin that does not enforce the floor, and the asymmetry is the
    whole point.

    `validate_rate` refuses a PRICE below cost because a price is a decision. A COST is not a
    decision — it is an observation about what a provider charges. Refusing to record that a vendor
    raised prices would leave the guardrail comparing against the old number and reporting a healthy
    margin, which is exactly the failure this endpoint exists to end: `search.web` carried a $0.004
    cost while we were paying Exa $0.007, and sat at 30% margin with nothing complaining.
    """
    token = await _pricing_admin(client, monkeypatch, slug="cr1", email="boss@cr1.com")

    # ai.email_draft is 2 credits. At $0.05 cost that is deeply underwater.
    r = await client.put("/api/admin/billing/costs/ai.email_draft", headers=auth(token),
                         json={"unit_cost_usd": 0.05, "source": "provider raised prices"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["unit_cost_usd"] == 0.05
    assert body["gross_margin"] < 0.5

    # And it actually persisted — the point is that the system now believes the true number.
    rates = (await client.get("/api/admin/billing/rates", headers=auth(token))).json()
    row = next(x for x in rates if x["capability_id"] == "ai.email_draft")
    assert row["unit_cost_usd"] == 0.05


async def test_it_reports_every_capability_the_change_pushed_under_the_floor(client, monkeypatch):
    """Recording is not enough — the response has to be a work list.

    Checks the WHOLE catalog rather than the one capability that was typed, because one provider
    price change can move several that share the input, and an operator who only hears about the one
    they edited will not go looking for the others.
    """
    token = await _pricing_admin(client, monkeypatch, slug="cr2", email="boss@cr2.com")
    r = await client.put("/api/admin/billing/costs/ai.email_draft", headers=auth(token),
                         json={"unit_cost_usd": 0.05})
    breached = r.json()["below_floor"]
    ids = [b["capability_id"] for b in breached]
    assert "ai.email_draft" in ids
    entry = next(b for b in breached if b["capability_id"] == "ai.email_draft")
    # The operator came to record a cost, not to do algebra: what the price must become is given.
    assert entry["credits_to_clear_floor"] == 10.0      # $0.05 / ($0.01 x 0.5)
    assert entry["gross_margin"] < 0.5


async def test_a_healthy_cost_change_reports_nothing_broken(client, monkeypatch):
    """A work list on every write would be noise, and noise is how a real one gets ignored."""
    token = await _pricing_admin(client, monkeypatch, slug="cr3", email="boss@cr3.com")
    r = await client.put("/api/admin/billing/costs/ai.email_draft", headers=auth(token),
                         json={"unit_cost_usd": 0.0001})
    assert r.status_code == 200
    assert r.json()["below_floor"] == []


async def test_repricing_after_a_cost_rise_clears_the_breach(client, monkeypatch):
    """The full loop the endpoint exists to enable: record the truth, then act on it."""
    token = await _pricing_admin(client, monkeypatch, slug="cr4", email="boss@cr4.com")
    await client.put("/api/admin/billing/costs/ai.email_draft", headers=auth(token),
                     json={"unit_cost_usd": 0.05})

    # The rate endpoint DOES enforce the floor, so the new price has to clear it.
    low = await client.put("/api/admin/billing/rates/ai.email_draft", headers=auth(token),
                           json={"credits_per_unit": 5})
    assert low.status_code == 422, "5 credits against $0.05 is 0% margin and must be refused"

    ok = await client.put("/api/admin/billing/rates/ai.email_draft", headers=auth(token),
                          json={"credits_per_unit": 12})
    assert ok.status_code == 200, ok.text
    assert ok.json()["gross_margin"] >= 0.5

    # And the breach is gone.
    again = await client.put("/api/admin/billing/costs/ai.email_draft", headers=auth(token),
                             json={"unit_cost_usd": 0.05})
    assert not any(b["capability_id"] == "ai.email_draft"
                   for b in again.json()["below_floor"])


async def test_a_cost_change_is_audited_with_its_source(client, monkeypatch):
    """The cost is the input the margin floor trusts, so "who says?" is the first question anyone
    will ask of it."""
    from sqlalchemy import select

    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.billing import BillingAuditLog

    token = await _pricing_admin(client, monkeypatch, slug="cr5", email="boss@cr5.com")
    await client.put("/api/admin/billing/costs/search.web", headers=auth(token),
                     json={"unit_cost_usd": 0.007, "source": "Exa list price, Aug 2026: $7/1k"})

    async with get_platform_sessionmaker()() as s:
        rows = list((await s.scalars(select(BillingAuditLog))).all())
    entry = next(r for r in rows if r.action == "cost.upsert")
    assert entry.target == "search.web"
    assert "Exa list price" in (entry.note or "")
    assert (entry.before or {}).get("unit_cost_usd") is not None, "the previous cost is recorded"


async def test_an_unknown_capability_is_a_404(client, monkeypatch):
    token = await _pricing_admin(client, monkeypatch, slug="cr6", email="boss@cr6.com")
    r = await client.put("/api/admin/billing/costs/nope.nothing", headers=auth(token),
                         json={"unit_cost_usd": 0.01})
    assert r.status_code == 404


async def test_a_negative_cost_is_refused(client, monkeypatch):
    """Not a margin judgement — a negative cost is not an observation about anything."""
    token = await _pricing_admin(client, monkeypatch, slug="cr7", email="boss@cr7.com")
    r = await client.put("/api/admin/billing/costs/ai.email_draft", headers=auth(token),
                         json={"unit_cost_usd": -1})
    assert r.status_code == 422


async def test_a_tenant_owner_cannot_change_costs(client):
    """Cost feeds the margin floor. Editing it is repricing power by another route."""
    token = await signup(client, slug="cr8", email="o@cr8.com", company="CR8")
    assert (await client.put("/api/admin/billing/costs/ai.email_draft", headers=auth(token),
                             json={"unit_cost_usd": 0.01})).status_code == 404
