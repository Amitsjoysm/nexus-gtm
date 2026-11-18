"""Every path that creates a workspace binds it before writing a tenant-scoped row (2026-11-18).

The real proof is `tests_integration/test_signup_rls.py` (Postgres, the app role, RLS). This guard
runs in the offline suite, which is SQLite and has no RLS, so the ordering that broke every new
workspace on Postgres cannot quietly come back: the consent row was staged before the tenant was
bound, and the statement that bound it autoflushed the row with no tenant set.
"""
from __future__ import annotations

import inspect

import pytest

from nexus.api.routers import auth
from nexus.auth import registration


@pytest.mark.parametrize("function", [auth.signup, auth.create_workspace,
                                      registration.verify_and_create],
                         ids=["signup", "create_workspace", "register_verify"])
def test_the_new_tenant_is_bound_before_any_tenant_row(function):
    source = inspect.getsource(function)
    bound = source.find("apply_rls(db, tenant.id)")
    assert bound != -1, "the new tenant is never bound to the session"
    for staged in ("add_signup_decision(", "start_subscription_for(", "Membership("):
        at = source.find(staged)
        assert at == -1 or bound < at, f"{staged} comes before the tenant is bound"
