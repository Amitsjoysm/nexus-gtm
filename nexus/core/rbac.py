"""Role-based access control. Roles are ordered; higher roles inherit lower permissions."""
from __future__ import annotations

import enum


class Role(str, enum.Enum):
    owner = "owner"
    admin = "admin"
    manager = "manager"
    rep = "rep"


# Higher number = more privilege.
_RANK = {Role.rep: 0, Role.manager: 1, Role.admin: 2, Role.owner: 3}


class Permission(str, enum.Enum):
    manage_workspace = "manage_workspace"      # owner/admin
    manage_relevance = "manage_relevance"      # admin+ (defines ICP/value props)
    manage_plays = "manage_plays"              # manager+
    run_agents = "run_agents"                  # rep+
    manage_accounts = "manage_accounts"        # rep+
    view_analytics = "view_analytics"          # manager+
    run_orchestration = "run_orchestration"    # manager+ (authors multi-agent runs)
    approve_outreach = "approve_outreach"      # manager+ (decides the outbound gate)
    manage_campaigns = "manage_campaigns"      # manager+ (launches segment campaigns)


_MIN_ROLE: dict[Permission, Role] = {
    Permission.manage_workspace: Role.admin,
    Permission.manage_relevance: Role.admin,
    Permission.manage_plays: Role.manager,
    Permission.view_analytics: Role.manager,
    Permission.run_agents: Role.rep,
    Permission.manage_accounts: Role.rep,
    Permission.run_orchestration: Role.manager,
    Permission.approve_outreach: Role.manager,
    Permission.manage_campaigns: Role.manager,
}


def has_permission(role: Role | str, permission: Permission) -> bool:
    role = Role(role)
    return _RANK[role] >= _RANK[_MIN_ROLE[permission]]
