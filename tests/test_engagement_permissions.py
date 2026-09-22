"""Reps run their own outreach; managers set the team's confidence range and see the team desk."""
from __future__ import annotations

from nexus.core.rbac import Permission, Role, has_permission


def test_every_role_can_run_engagement():
    for role in Role:
        assert has_permission(role, Permission.run_engagement)


def test_only_managers_and_above_manage_engagement():
    assert not has_permission(Role.rep, Permission.manage_engagement)
    for role in (Role.manager, Role.admin, Role.owner):
        assert has_permission(role, Permission.manage_engagement)
