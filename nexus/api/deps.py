"""FastAPI dependencies: DB sessions, authentication, tenant binding, permission checks."""
from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Callable

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from nexus.core.db import get_sessionmaker
from nexus.core.rbac import Permission, Role, has_permission
from nexus.core.security import decode_access_token
from nexus.core.tenancy import (
    TenantSession,
    apply_rls,
    set_current_tenant,
)

_bearer = HTTPBearer(auto_error=True)


@dataclass(slots=True)
class Principal:
    user_id: str
    tenant_id: str
    role: str
    # Set only for an impersonation session: the platform admin acting as this user. Present so
    # every request is attributable to a real person — a session that cannot be traced back to a
    # human is indistinguishable from a compromised account.
    impersonator_id: str = ""
    # Impersonation sessions are read-only; `require_writable` refuses mutations carrying this.
    read_only: bool = False


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """Raw session for tenant-less operations (signup/login)."""
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def get_principal(
    creds: HTTPAuthorizationCredentials = Depends(_bearer),
) -> Principal:
    payload = decode_access_token(creds.credentials)
    if not payload or "sub" not in payload or "tid" not in payload:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    return Principal(
        user_id=payload["sub"],
        tenant_id=payload["tid"],
        role=payload.get("role", "rep"),
        impersonator_id=str(payload.get("imp") or ""),
        read_only=bool(payload.get("ro")),
    )


async def get_tenant_session(
    principal: Principal = Depends(get_principal),
) -> AsyncIterator[TenantSession]:
    """Bind the tenant for the request and hand back a tenant-scoped session."""
    set_current_tenant(principal.tenant_id)
    async with get_sessionmaker()() as session:
        await apply_rls(session, principal.tenant_id)
        try:
            yield TenantSession(session, principal.tenant_id)
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            set_current_tenant(None)


# Permissions an impersonating admin may still exercise. Only genuinely read-only ones: every
# other Permission either writes tenant data or spends the customer's money (`run_agents` and
# `run_orchestration` make billable LLM calls, which would appear on their invoice).
_READ_PERMISSIONS = frozenset({Permission.view_analytics})


async def require_writable(principal: Principal = Depends(get_principal)) -> Principal:
    """Refuse a mutation made under an impersonation session.

    Impersonation is **read-only** by design (the plan's "impersonate-read, audited, no writes").
    Support diagnosing a problem never needs to change a customer's data, and an admin silently
    editing inside a customer account is the worst thing this feature could enable. Enforced here
    rather than trusted to the UI: a banner is a courtesy, a 403 is a control.
    """
    if principal.read_only:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "this is a read-only impersonation session; end it to make changes",
        )
    return principal


def require(permission: Permission) -> Callable[[Principal], Principal]:
    """Dependency factory enforcing an RBAC permission."""

    def _checker(principal: Principal = Depends(get_principal)) -> Principal:
        if not has_permission(Role(principal.role), permission):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, f"Role '{principal.role}' lacks {permission.value}"
            )
        # An impersonation session may read whatever the user can read, and change nothing. Placed
        # here because every mutating tenant endpoint already passes through an RBAC check, so
        # there is no route that can quietly skip it.
        if principal.read_only and permission not in _READ_PERMISSIONS:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "this is a read-only impersonation session; end it to make changes",
            )
        return principal

    return _checker


async def require_platform_admin(
    principal: Principal = Depends(get_principal),
) -> Principal:
    """Gate for the staff /admin surface. Prefer ``require_platform_permission``.

    Platform admins are operators of the SaaS, NOT tenant members: tenant RBAC (owner/admin)
    deliberately grants nothing here. Membership comes from the ``platform_admins`` table, plus
    an env allowlist (``NEXUS_PLATFORM_ADMIN_EMAILS``) that solves the bootstrap problem.

    Kept for compatibility, but no longer a *flat* gate: it now means ``billing.read``, the
    weakest permission any platform role holds. It used to accept any active row regardless of
    role, so an endpoint written against it would silently reopen the hole M14 closed — a support
    admin repricing plans. Every endpoint should name the permission it actually needs.
    """
    from nexus.billing.permissions import BILLING_READ

    held = await platform_permissions(principal)
    if BILLING_READ not in held:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "platform access denied")
    return principal


async def platform_permissions(principal: Principal) -> set[str]:
    """The permission set this principal actually holds on the platform surface.

    Returns an empty set for anyone who is not a platform admin, so callers can treat "no
    permissions" and "not staff" identically.
    """
    from sqlalchemy import select

    from nexus.billing.permissions import ALL_PERMISSIONS, effective_permissions
    from nexus.core.config import get_settings
    from nexus.models.billing import PlatformAdmin
    from nexus.models.identity import User

    async with get_sessionmaker()() as session:
        user = await session.get(User, principal.user_id)
        email = (user.email or "").lower() if user else ""
        if not email:
            return set()
        # The bootstrap allowlist keeps FULL power. It exists to solve "nobody can reach the
        # console yet"; narrowing it would reintroduce the lockout it was added to prevent.
        if email in get_settings().platform_admin_email_list:
            return set(ALL_PERMISSIONS)
        row = (
            await session.scalars(
                select(PlatformAdmin).where(
                    PlatformAdmin.email == email, PlatformAdmin.active == True  # noqa: E712
                )
            )
        ).first()
    return effective_permissions(row) if row is not None else set()


def require_platform_permission(permission: str):
    """Gate a staff endpoint on ONE named permission.

    Separate from ``require_platform_admin`` on purpose: that dependency is retained as the
    equivalent of ``billing.read`` so an endpoint's behaviour changes only when it is
    individually annotated. Nobody loses access during the rollout.
    """

    async def _dep(principal: Principal = Depends(get_principal)) -> Principal:
        held = await platform_permissions(principal)
        if permission not in held:
            # Same message and status as the plain admin gate: an admin probing which specific
            # permission they lack learns nothing they could not learn by trying.
            raise HTTPException(status.HTTP_403_FORBIDDEN, "platform access denied")
        return principal

    return _dep
