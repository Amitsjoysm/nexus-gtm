# tests/test_admin_customers.py
"""The Control-plane customer directory.

One place to find a workspace — by the email of anyone in it, or by its own name — and see what it
is on, what it has used, and what it owes. Before this the answer was spread across four screens
and "which workspace is this person in?" had no surface at all.

Every read here crosses tenants, which is this codebase's most-repeated production-only bug: under
the RLS-bound app role a cross-tenant aggregate returns ZERO ROWS rather than raising, so it looks
exactly like a customer who has used nothing.
"""
from __future__ import annotations

from tests.conftest import auth, signup


async def _superadmin(client, monkeypatch, *, slug: str, email: str) -> str:
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "platform_admin_emails", email)
    return await signup(client, slug=slug, email=email, company=slug.upper())


async def test_a_tenant_owner_cannot_read_the_directory(client):
    """It exposes every workspace on the platform. No tenant role reaches it, however senior."""
    token = await signup(client, slug="cd0", email="o@cd0.com", company="CD0")
    assert (await client.get("/api/admin/billing/customers",
                             headers=auth(token))).status_code == 404
    assert (await client.get("/api/admin/billing/customers/whatever/usage",
                             headers=auth(token))).status_code == 404


async def test_searching_by_a_member_email_finds_their_workspace(client, monkeypatch):
    """The question an operator granting credits actually has.

    Credits belong to a WORKSPACE, not to a person, so "give this user credits" has to resolve
    through membership first. The row carries the matched email back so the operator can see they
    found the right person rather than a workspace merely containing a similar address.
    """
    token = await _superadmin(client, monkeypatch, slug="cd1", email="boss@cd1.com")
    await signup(client, slug="acmeco", email="rep@acmeco.com", company="Acme Co")

    r = await client.get("/api/admin/billing/customers?q=rep@acmeco.com", headers=auth(token))
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["workspace"] == "Acme Co"
    assert rows[0]["matched_email"] == "rep@acmeco.com"
    assert rows[0]["users"] >= 1


async def test_searching_by_workspace_name_also_works(client, monkeypatch):
    """An operator who knows the company but not who works there."""
    token = await _superadmin(client, monkeypatch, slug="cd2", email="boss@cd2.com")
    await signup(client, slug="globex", email="o@initech.com", company="Globex")

    rows = (await client.get("/api/admin/billing/customers?q=Globex",
                             headers=auth(token))).json()
    assert any(r["workspace"] == "Globex" for r in rows)
    # Matched on the workspace, not on a person, so the column stays empty rather than picking
    # some member arbitrarily and implying the operator searched for them.
    assert next(r for r in rows if r["workspace"] == "Globex")["matched_email"] == ""


async def test_a_term_matching_both_a_name_and_an_email_reports_the_person(client, monkeypatch):
    """Searching "acme" finds Acme Ltd both by name and through anyone @acme.com, and the row
    says which person matched. Neither half is redundant: a workspace can be named nothing like
    its email domain, and a member's address can belong to a company the workspace is not named
    after."""
    token = await _superadmin(client, monkeypatch, slug="cd7", email="boss@cd7.com")
    await signup(client, slug="acmeltd", email="cfo@acmeltd.com", company="Acme Ltd")

    rows = (await client.get("/api/admin/billing/customers?q=acmeltd",
                             headers=auth(token))).json()
    row = next(r for r in rows if r["workspace"] == "Acme Ltd")
    assert row["matched_email"] == "cfo@acmeltd.com"


async def test_a_search_that_matches_nothing_is_empty_not_everything(client, monkeypatch):
    """The filter must not fall open. An unmatched search returning every workspace on the
    platform would read as a result rather than as a miss."""
    token = await _superadmin(client, monkeypatch, slug="cd3", email="boss@cd3.com")
    await signup(client, slug="hidden", email="o@hidden.com", company="Hidden")

    rows = (await client.get("/api/admin/billing/customers?q=zzz-no-such-customer",
                             headers=auth(token))).json()
    assert rows == []


async def test_the_directory_counts_usage_belonging_to_another_tenant(client, monkeypatch):
    """The RLS trap, pinned.

    `billing_usage_events` and `billing_credit_ledger` are tenant-scoped. Read through the
    RLS-bound app role, a cross-tenant aggregate returns zero rows and raises nothing — the
    directory would show every customer at 0 requests and 0 credits, which is indistinguishable
    from a platform nobody uses. The same mistake shipped once already in /admin/billing/overview.
    """
    from nexus.core.db import get_platform_sessionmaker, utcnow
    from nexus.models.billing import BillingCreditLedger, BillingUsageEvent

    token = await _superadmin(client, monkeypatch, slug="cd4", email="boss@cd4.com")
    other = await signup(client, slug="userco", email="o@userco.com", company="User Co")
    assert other

    rows = (await client.get("/api/admin/billing/customers?q=User Co",
                             headers=auth(token))).json()
    tenant_id = rows[0]["tenant_id"]

    async with get_platform_sessionmaker()() as s:
        s.add(BillingUsageEvent(
            tenant_id=tenant_id, capability_id="ai.email_draft", quantity=1, unit="action",
            idempotency_key="cust-dir-probe-1", occurred_at=utcnow(),
        ))
        s.add(BillingCreditLedger(
            tenant_id=tenant_id, delta=250.0, reason="goodwill", kind="grant",
            idempotency_key="cust-dir-credit-1",
        ))
        await s.commit()

    again = (await client.get("/api/admin/billing/customers?q=User Co",
                              headers=auth(token))).json()
    row = next(r for r in again if r["tenant_id"] == tenant_id)
    assert row["requests_this_period"] == 1, "a cross-tenant count must see another tenant's rows"
    assert row["credits_balance"] == 250.0


async def test_the_usage_detail_reads_another_workspace(client, monkeypatch):
    """"How much has this customer used?" had no answer short of impersonating them."""
    from nexus.core.db import get_platform_sessionmaker, utcnow
    from nexus.models.billing import BillingUsageEvent

    token = await _superadmin(client, monkeypatch, slug="cd5", email="boss@cd5.com")
    await signup(client, slug="usageco", email="o@usageco.com", company="Usage Co")
    rows = (await client.get("/api/admin/billing/customers?q=Usage Co",
                             headers=auth(token))).json()
    tenant_id = rows[0]["tenant_id"]

    async with get_platform_sessionmaker()() as s:
        for i in range(3):
            s.add(BillingUsageEvent(
                tenant_id=tenant_id, capability_id="ai.research_brief", quantity=1, unit="action",
                idempotency_key=f"usage-detail-{i}", occurred_at=utcnow(),
            ))
        await s.commit()

    r = await client.get(f"/api/admin/billing/customers/{tenant_id}/usage", headers=auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["workspace"] == "Usage Co"
    assert body["requests_this_period"] == 3
    brief = next(c for c in body["capabilities"] if c["capability_id"] == "ai.research_brief")
    assert brief["used"] == 3.0
    # Only what was actually used. The whole catalog at zero would bury the three rows that
    # matter under sixty rows of nothing.
    assert len(body["capabilities"]) == 1


async def test_usage_for_an_unknown_workspace_is_a_404(client, monkeypatch):
    """Not an empty report. "This workspace has used nothing" and "there is no such workspace"
    send an operator in opposite directions."""
    token = await _superadmin(client, monkeypatch, slug="cd6", email="boss@cd6.com")
    r = await client.get("/api/admin/billing/customers/no-such-tenant/usage",
                         headers=auth(token))
    assert r.status_code == 404
