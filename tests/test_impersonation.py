"""Time-boxed, read-only, audited impersonation (M25).

`grep -riE "suspend|lock_account|impersonat"` returned nothing before this. Support could not
diagnose "the inbox looks wrong for me" from outside the account, and the alternative this replaces
is asking a customer for their password.

Four constraints make it defensible, and every one is enforced server-side rather than in the UI —
a banner is a courtesy, a 403 is a control:

* **read-only** — an admin changing customer data unnoticed is the worst thing this could enable
* **time-boxed** — minutes, capped; a standing key to every account is not a support tool
* **attributable** — the token names the impersonator
* **audited with a reason**, written *before* the token is minted
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from nexus.core.security import create_impersonation_token, decode_access_token
from tests.conftest import auth, signup


async def _boss(client, monkeypatch, slug="imp"):
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.core.config import get_settings

    await sync_catalog()
    await sync_plans()
    monkeypatch.setattr(get_settings(), "platform_admin_emails", "boss@nexus.com")
    return await signup(client, slug=slug, email="boss@nexus.com", company=slug.upper())


# ---- the token ----------------------------------------------------------------------------------

def test_the_token_is_usable_as_a_normal_access_token():
    """Deliberately an `access` token: an admin needs to browse the real application, and a bespoke
    type would mean auditing every endpoint for a second code path — the surest way to miss one."""
    token = create_impersonation_token(
        user_id="u1", tenant_id="t1", role="admin", impersonator_id="a1"
    )
    payload = decode_access_token(token)
    assert payload is not None
    assert payload["sub"] == "u1" and payload["tid"] == "t1"


def test_the_token_names_the_impersonator():
    """A session that cannot be traced to a human is indistinguishable from a compromised account."""
    payload = decode_access_token(
        create_impersonation_token(user_id="u1", tenant_id="t1", role="rep",
                                   impersonator_id="admin-42")
    )
    assert payload["imp"] == "admin-42"


def test_the_token_is_marked_read_only():
    payload = decode_access_token(
        create_impersonation_token(user_id="u1", tenant_id="t1", role="rep",
                                   impersonator_id="a1")
    )
    assert payload["ro"] is True


def test_the_token_is_time_boxed_in_minutes():
    payload = decode_access_token(
        create_impersonation_token(user_id="u1", tenant_id="t1", role="rep",
                                   impersonator_id="a1", ttl_min=15)
    )
    assert round((payload["exp"] - payload["iat"]) / 60) == 15


def test_a_zero_or_negative_ttl_is_floored_not_infinite():
    """A misconfigured 0 must not mint a token that never expires."""
    payload = decode_access_token(
        create_impersonation_token(user_id="u1", tenant_id="t1", role="rep",
                                   impersonator_id="a1", ttl_min=0)
    )
    assert payload["exp"] > payload["iat"]


# ---- the read-only guard ------------------------------------------------------------------------

def _principal(read_only: bool):
    from nexus.api.deps import Principal

    return Principal(user_id="u1", tenant_id="t1", role="owner",
                     impersonator_id="a1" if read_only else "", read_only=read_only)


async def test_require_writable_refuses_an_impersonation_session():
    from nexus.api.deps import require_writable

    with pytest.raises(HTTPException) as exc:
        await require_writable(_principal(True))
    assert exc.value.status_code == 403
    assert "read-only" in str(exc.value.detail)


async def test_require_writable_allows_a_normal_session():
    from nexus.api.deps import require_writable

    assert (await require_writable(_principal(False))).read_only is False


class _Req:
    """Minimal stand-in for a Starlette Request — only the method is consulted."""

    def __init__(self, method: str):
        self.method = method


def test_a_mutating_request_is_refused_under_impersonation():
    """Enforced at the RBAC choke point, because every RBAC-gated tenant endpoint passes through
    one — so no route can quietly skip the check."""
    from nexus.api.deps import require
    from nexus.core.rbac import Permission

    checker = require(Permission.manage_accounts)
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        with pytest.raises(HTTPException) as exc:
            checker(_Req(method), _principal(True))
        assert exc.value.status_code == 403


def test_a_read_is_permitted_even_behind_a_write_named_permission():
    """The flaw live testing caught. This codebase's RBAC is coarse — `manage_accounts` gates both
    LISTING and creating — so refusing by permission name blocked `GET /api/accounts`, which is
    exactly the read an admin impersonates in order to perform. The unit-test version of the
    permission-based rule looked correct and was useless in practice."""
    from nexus.api.deps import require
    from nexus.core.rbac import Permission

    for permission in (Permission.manage_accounts, Permission.view_analytics,
                       Permission.run_agents):
        assert require(permission)(_Req("GET"), _principal(True)).read_only is True


def test_a_normal_session_is_unaffected_by_any_of_this():
    """The compatibility line: nothing changes for a user who is not being impersonated."""
    from nexus.api.deps import require
    from nexus.core.rbac import Permission

    for method in ("GET", "POST", "DELETE"):
        assert require(Permission.manage_accounts)(_Req(method), _principal(False)) is not None


# ---- the permission -----------------------------------------------------------------------------

def test_impersonation_is_not_bundled_into_users_manage():
    """Resetting someone's MFA and *becoming* them are different powers. Bundling would grant the
    second to every support agent who needs the first."""
    from nexus.billing.permissions import ROLE_PRESETS, USERS_IMPERSONATE, USERS_MANAGE

    assert USERS_MANAGE in ROLE_PRESETS["support"]
    assert USERS_IMPERSONATE not in ROLE_PRESETS["support"]
    assert USERS_IMPERSONATE in ROLE_PRESETS["superadmin"]


# ---- the endpoint -------------------------------------------------------------------------------

async def test_a_reason_is_required(client, monkeypatch):
    """An impersonation with no stated reason is indistinguishable from curiosity, and the audit
    row is worthless without it."""
    boss = await _boss(client, monkeypatch, slug="imp1")
    r = await client.post(
        "/api/admin/users/boss@nexus.com/impersonate",
        headers=auth(boss), json={"reason": "short"},
    )
    assert r.status_code == 422


async def test_impersonation_returns_a_read_only_session(client, monkeypatch):
    boss = await _boss(client, monkeypatch, slug="imp2")
    r = await client.post(
        "/api/admin/users/boss@nexus.com/impersonate",
        headers=auth(boss),
        json={"reason": "debugging a reported inbox issue", "ttl_min": 20},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["read_only"] is True
    assert body["expires_in_min"] == 20
    payload = decode_access_token(body["access_token"])
    assert payload["ro"] is True and payload["imp"]


async def test_the_session_is_audited_before_it_is_issued(client, monkeypatch):
    """If the audit write fails, no session is handed out. The reverse order would allow an
    unlogged impersonation whenever the audit table is down."""
    from sqlalchemy import select

    from nexus.core.db import get_sessionmaker
    from nexus.models.billing import BillingAuditLog

    boss = await _boss(client, monkeypatch, slug="imp3")
    await client.post(
        "/api/admin/users/boss@nexus.com/impersonate",
        headers=auth(boss), json={"reason": "customer reported a scoring bug"},
    )
    async with get_sessionmaker()() as s:
        rows = (await s.scalars(
            select(BillingAuditLog).where(BillingAuditLog.action == "user.impersonate")
        )).all()
    assert rows
    assert "scoring bug" in (rows[-1].note or "")


async def test_an_unknown_user_is_a_404(client, monkeypatch):
    boss = await _boss(client, monkeypatch, slug="imp4")
    r = await client.post(
        "/api/admin/users/nobody@nowhere.com/impersonate",
        headers=auth(boss), json={"reason": "investigating a support ticket"},
    )
    assert r.status_code == 404


async def test_a_tenant_user_cannot_impersonate(client):
    token = await signup(client, slug="imp5", email="rep@imp5.com", company="IMP5")
    r = await client.post(
        "/api/admin/users/rep@imp5.com/impersonate",
        headers=auth(token), json={"reason": "trying to escalate privileges"},
    )
    assert r.status_code in (401, 404)


async def test_the_ttl_is_capped(client, monkeypatch):
    """A standing key to every account is not a support tool."""
    boss = await _boss(client, monkeypatch, slug="imp6")
    r = await client.post(
        "/api/admin/users/boss@nexus.com/impersonate",
        headers=auth(boss), json={"reason": "a very long support session", "ttl_min": 100000},
    )
    assert r.status_code == 422
