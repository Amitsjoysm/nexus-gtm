# tests/test_admin_permissions.py
"""Per-permission platform RBAC.

Before this, `require_platform_admin` checked only that an active row existed and never read
`platform_role` — a "support" admin could reprice every plan and mint unlimited credits. The role
column was decoration.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from tests.conftest import auth, signup  # `client` is an auto-discovered conftest fixture


async def _bootstrap_admin(client, monkeypatch, slug: str):
    """A superadmin via the env allowlist, used to provision the narrower roles."""
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.billing.rates import sync_rates
    from nexus.core.config import get_settings

    await sync_catalog()
    await sync_plans()
    await sync_rates()
    monkeypatch.setattr(get_settings(), "platform_admin_emails", "boss@nexus.com")
    return await signup(client, slug=slug, email="boss@nexus.com", company=slug.upper())


async def _make_role(client, boss: str, email: str, role: str, slug: str) -> str:
    """Grant `role` to `email`, then sign that user up and return their token."""
    r = await client.post(
        "/api/admin/billing/admins", headers=auth(boss),
        json={"email": email, "platform_role": role},
    )
    assert r.status_code == 200, r.text
    return await signup(client, slug=slug, email=email, company=slug.upper())


# ---- presets --------------------------------------------------------------------------------

def test_presets_are_narrower_than_superadmin():
    from nexus.billing.permissions import (
        ADMINS_MANAGE, ALL_PERMISSIONS, PRICING_WRITE, ROLE_PRESETS,
    )

    assert set(ROLE_PRESETS["superadmin"]) == set(ALL_PERMISSIONS)
    # The defect this milestone fixes: support could reprice and manage admins.
    assert PRICING_WRITE not in ROLE_PRESETS["support"]
    assert ADMINS_MANAGE not in ROLE_PRESETS["support"]
    assert ADMINS_MANAGE not in ROLE_PRESETS["finance"]


def test_an_unknown_role_degrades_to_read_only():
    """Failing to NO access would lock out an admin whose role was mistyped, with no in-product
    way to fix it. Read-only lets them see the console and ask for a correction."""
    from nexus.billing.permissions import BILLING_READ, permissions_for_role

    assert permissions_for_role("typo") == [BILLING_READ]


def test_an_empty_permission_list_falls_back_to_the_role():
    """This fallback is what makes migration 0029 safe: pre-existing rows have no explicit list
    and must keep behaving exactly as their role implies."""
    from nexus.billing.permissions import ROLE_PRESETS, effective_permissions

    class Row:
        platform_role = "finance"
        permissions: list = []

    assert effective_permissions(Row()) == set(ROLE_PRESETS["finance"])


def test_an_explicit_list_wins_over_the_preset():
    from nexus.billing.permissions import BILLING_READ, effective_permissions

    class Row:
        platform_role = "superadmin"
        permissions = [BILLING_READ]

    assert effective_permissions(Row()) == {BILLING_READ}


# ---- enforcement ----------------------------------------------------------------------------

async def test_support_cannot_reprice_a_plan(client, monkeypatch):
    """The headline defect."""
    boss = await _bootstrap_admin(client, monkeypatch, "rb1")
    support = await _make_role(client, boss, "help@nexus.com", "support", "rb1s")

    r = await client.patch(
        "/api/admin/billing/plans/growth", headers=auth(support),
        json={"base_price_cents": 1},
    )
    assert r.status_code == 403


async def test_support_can_still_read_the_console(client, monkeypatch):
    boss = await _bootstrap_admin(client, monkeypatch, "rb2")
    support = await _make_role(client, boss, "help2@nexus.com", "support", "rb2s")

    assert (await client.get("/api/admin/billing/plans", headers=auth(support))).status_code == 200
    assert (await client.get("/api/admin/billing/rates", headers=auth(support))).status_code == 200


async def test_finance_can_reprice_but_not_manage_admins(client, monkeypatch):
    boss = await _bootstrap_admin(client, monkeypatch, "rb3")
    finance = await _make_role(client, boss, "cfo@nexus.com", "finance", "rb3s")

    ok = await client.patch(
        "/api/admin/billing/plans/starter", headers=auth(finance),
        json={"base_price_cents": 4200},
    )
    assert ok.status_code == 200, ok.text

    denied = await client.post(
        "/api/admin/billing/admins", headers=auth(finance),
        json={"email": "sneaky@nexus.com", "platform_role": "superadmin"},
    )
    assert denied.status_code == 403


async def test_support_cannot_replay_dead_letter_jobs(client, monkeypatch):
    boss = await _bootstrap_admin(client, monkeypatch, "rb4")
    support = await _make_role(client, boss, "help4@nexus.com", "support", "rb4s")
    r = await client.get("/api/admin/jobs/dead-letters", headers=auth(support))
    assert r.status_code == 403


# ---- capped credit grants -------------------------------------------------------------------

async def _tenant_id(slug: str) -> str:
    from sqlalchemy import select

    from nexus.core.db import get_sessionmaker
    from nexus.models.identity import Tenant

    async with get_sessionmaker()() as s:
        return (await s.scalars(select(Tenant.id).where(Tenant.slug == slug))).first()


async def test_support_can_grant_goodwill_credits_within_the_cap(client, monkeypatch):
    """Goodwill credits are the commonest support action; forcing an escalation for every one
    turns the escalation into a rubber stamp."""
    boss = await _bootstrap_admin(client, monkeypatch, "rb5")
    support = await _make_role(client, boss, "help5@nexus.com", "support", "rb5s")
    tid = await _tenant_id("rb5")

    r = await client.post(
        f"/api/admin/billing/tenants/{tid}/credits", headers=auth(support),
        json={"amount": 500, "reason": "goodwill", "idempotency_key": "rb5-1"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["applied"] is True


async def test_support_cannot_exceed_the_cap(client, monkeypatch):
    boss = await _bootstrap_admin(client, monkeypatch, "rb6")
    support = await _make_role(client, boss, "help6@nexus.com", "support", "rb6s")
    tid = await _tenant_id("rb6")

    r = await client.post(
        f"/api/admin/billing/tenants/{tid}/credits", headers=auth(support),
        json={"amount": 1_000_000, "reason": "oops", "idempotency_key": "rb6-1"},
    )
    assert r.status_code == 403
    # Specific, not generic: they hold a real grant permission and need to know an escalation
    # is required rather than thinking they have no access at all.
    assert "credits.grant" in r.text


async def test_finance_has_no_cap(client, monkeypatch):
    boss = await _bootstrap_admin(client, monkeypatch, "rb7")
    finance = await _make_role(client, boss, "cfo7@nexus.com", "finance", "rb7s")
    tid = await _tenant_id("rb7")

    r = await client.post(
        f"/api/admin/billing/tenants/{tid}/credits", headers=auth(finance),
        json={"amount": 1_000_000, "reason": "enterprise top-up", "idempotency_key": "rb7-1"},
    )
    assert r.status_code == 200, r.text


# ---- compatibility --------------------------------------------------------------------------

async def test_the_bootstrap_allowlist_keeps_full_power(client, monkeypatch):
    """It exists to solve "nobody can reach the console yet". Narrowing it would reintroduce the
    lockout it was added to prevent."""
    boss = await _bootstrap_admin(client, monkeypatch, "rb8")
    repriced = await client.patch(
        "/api/admin/billing/plans/growth", headers=auth(boss),
        json={"base_price_cents": 7900},
    )
    assert repriced.status_code == 200, repriced.text
    jobs = await client.get("/api/admin/jobs/dead-letters", headers=auth(boss))
    assert jobs.status_code == 200, jobs.text


async def test_whoami_reports_the_permission_set(client, monkeypatch):
    """The SPA uses this to hide controls a role cannot use. The server is still the boundary."""
    boss = await _bootstrap_admin(client, monkeypatch, "rb9")
    support = await _make_role(client, boss, "help9@nexus.com", "support", "rb9s")

    me = await client.get("/api/admin/billing/whoami", headers=auth(support))
    assert me.status_code == 200
    perms = me.json()["permissions"]
    assert "billing.read" in perms
    assert "pricing.write" not in perms


async def test_whoami_reports_the_grant_ceiling(client, monkeypatch):
    """The console states the real limit instead of hardcoding a number config can change."""
    from nexus.core.config import get_settings

    boss = await _bootstrap_admin(client, monkeypatch, "rb12")
    monkeypatch.setattr(get_settings(), "billing_support_credit_cap", 250.0)
    support = await _make_role(client, boss, "help12@nexus.com", "support", "rb12s")
    finance = await _make_role(client, boss, "cfo12@nexus.com", "finance", "rb12f")

    capped = await client.get("/api/admin/billing/whoami", headers=auth(support))
    assert capped.json()["credit_grant_cap"] == 250.0
    # None means "no ceiling", not "cannot grant" — the permission list answers that.
    unlimited = await client.get("/api/admin/billing/whoami", headers=auth(finance))
    assert unlimited.json()["credit_grant_cap"] is None
    allowlisted = await client.get("/api/admin/billing/whoami", headers=auth(boss))
    assert allowlisted.json()["credit_grant_cap"] is None


async def test_a_tenant_owner_still_reaches_nothing(client):
    token = await signup(client, slug="rb10", email="o@rb10.com", company="RB10")
    for method, path in (
        ("get", "/api/admin/billing/plans"),
        ("get", "/api/admin/jobs/dead-letters"),
    ):
        r = await getattr(client, method)(path, headers=auth(token))
        assert r.status_code in (401, 403)


async def test_the_legacy_gate_is_no_longer_flat(client, monkeypatch):
    """``require_platform_admin`` used to accept any active row regardless of role, so an endpoint
    written against it would silently reopen the hole M14 closed. It now means ``billing.read``."""
    boss = await _bootstrap_admin(client, monkeypatch, "rb13")
    r = await client.post(
        "/api/admin/billing/admins", headers=auth(boss),
        json={"email": "nobody@nexus.com", "platform_role": "support",
              "permissions": ["users.manage"]},   # deliberately withholds billing.read
    )
    assert r.status_code == 200, r.text
    nobody = await signup(client, slug="rb13s", email="nobody@nexus.com", company="RB13S")

    from nexus.api.deps import require_platform_admin
    from tests.conftest import principal_from_token

    with pytest.raises(HTTPException) as excinfo:
        await require_platform_admin(principal_from_token(nobody))
    assert excinfo.value.status_code == 403

    # ...and the permission they DO hold still works, so this is a narrowing, not a lockout.
    from nexus.api.deps import platform_permissions

    assert await platform_permissions(principal_from_token(nobody)) == {"users.manage"}


async def test_granting_an_unknown_permission_is_refused(client, monkeypatch):
    """A typo'd permission would silently grant nothing, which is worse than a clear refusal."""
    boss = await _bootstrap_admin(client, monkeypatch, "rb11")
    r = await client.post(
        "/api/admin/billing/admins", headers=auth(boss),
        json={"email": "x@nexus.com", "platform_role": "support",
              "permissions": ["billing.read", "pricing.wrote"]},
    )
    assert r.status_code == 422
    assert "pricing.wrote" in r.text
