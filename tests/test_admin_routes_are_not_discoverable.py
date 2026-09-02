# tests/test_admin_routes_are_not_discoverable.py
"""A stranger must not be able to learn that the admin surface exists.

A 403 answers the attacker's question. Enumerating `/api/admin/...` against a deployment that
returns 403 for real paths and 404 for invented ones hands over a complete map of the staff
surface — provider keys, payment credentials, the runtime panel, the customer directory — without
a single valid credential. The interactive docs were already turned off outside local/test for
exactly this reason; this closes the same leak through status codes.

The distinction that matters, and why this is not simply "return 404 everywhere":

* **A stranger** — not a platform admin at all — gets **404**. Indistinguishable from a route that
  was never registered, so enumeration yields nothing.
* **A platform admin who has proven who they are** keeps informative errors. Someone locked out by
  the IP allowlist needs to see the address we observed, or they cannot fix their own lockout — and
  they already know the surface exists. Turning that into a 404 would make a self-inflicted lockout
  unrecoverable without shell access.

So: hide existence from people who have not proven anything; stay honest with people who have.
"""
from __future__ import annotations

import pytest

from tests.conftest import auth, signup

# A representative spread rather than every route: provider keys, payment credentials, the runtime
# panel and the customer directory each sit behind a different permission.
ADMIN_PATHS = [
    "/api/admin/provider-keys",
    "/api/admin/payment-credentials",
    "/api/admin/runtime/settings",
    "/api/admin/billing/customers",
    "/api/admin/billing/overview",
]


@pytest.mark.parametrize("path", ADMIN_PATHS)
async def test_a_normal_user_cannot_tell_an_admin_route_from_a_missing_one(
    client, fresh_db, path
):
    token = await signup(client, slug="nd1", email="a@nd1.com", company="ND1")
    real = await client.get(path, headers=auth(token))
    invented = await client.get("/api/admin/no-such-thing-here", headers=auth(token))

    assert real.status_code == 404, (
        f"{path} returned {real.status_code} to a normal user, which confirms the route exists"
    )
    assert real.status_code == invented.status_code, (
        "a real admin path and an invented one must be indistinguishable"
    )


@pytest.mark.parametrize("path", ADMIN_PATHS)
async def test_an_anonymous_caller_learns_nothing_either(client, fresh_db, path):
    """No token at all. 401 is fine — it says "authenticate", not "this admin route exists" — but
    it must not be a 403 that distinguishes real paths from invented ones."""
    real = await client.get(path)
    assert real.status_code in (401, 404), (
        f"{path} returned {real.status_code} anonymously"
    )


async def test_a_platform_admin_still_gets_through(client, fresh_db, monkeypatch):
    """THE compatibility line. Hiding the surface from strangers must not hide it from staff."""
    from nexus.core.config import get_settings

    email = "boss@nd2.com"
    monkeypatch.setattr(get_settings(), "platform_admin_emails", email)
    token = await signup(client, slug="nd2", email=email, company="ND2")

    r = await client.get("/api/admin/runtime/settings", headers=auth(token))
    assert r.status_code == 200, r.text


async def test_a_platform_admin_lacking_one_permission_still_gets_a_403(
    client, fresh_db, monkeypatch
):
    """Someone who has proven they are staff should be told they lack a permission, not sent on a
    hunt for a route that plainly exists. 404 there would make a permission problem look like a
    deployment problem."""
    from nexus.billing.permissions import permissions_for_role
    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.billing import PlatformAdmin

    email = "support@nd3.com"
    token = await signup(client, slug="nd3", email=email, company="ND3")

    # A real platform admin, but on the narrowest preset.
    async with get_platform_sessionmaker()() as s:
        s.add(PlatformAdmin(
            email=email, platform_role="support", active=True,
            permissions=[p for p in permissions_for_role("support")],
        ))
        await s.commit()

    r = await client.get("/api/admin/provider-keys", headers=auth(token))
    assert r.status_code == 403, (
        f"a known platform admin lacking providers.manage got {r.status_code}; they have proven "
        f"who they are and should be told what they lack"
    )
