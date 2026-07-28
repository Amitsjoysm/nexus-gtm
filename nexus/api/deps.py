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
    return Principal(user_id=payload["sub"], tenant_id=payload["tid"], role=payload.get("role", "rep"))


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


def require(permission: Permission) -> Callable[[Principal], Principal]:
    """Dependency factory enforcing an RBAC permission."""

    def _checker(principal: Principal = Depends(get_principal)) -> Principal:
        if not has_permission(Role(principal.role), permission):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, f"Role '{principal.role}' lacks {permission.value}"
            )
        return principal

    return _checker


async def require_platform_admin(
    principal: Principal = Depends(get_principal),
) -> Principal:
    """Gate for the staff /admin surface.

    Platform admins are operators of the SaaS, NOT tenant members: tenant RBAC (owner/admin)
    deliberately grants nothing here. Membership comes from the ``platform_admins`` table, plus
    an env allowlist (``NEXUS_PLATFORM_ADMIN_EMAILS``) that solves the bootstrap problem.
    """
    from sqlalchemy import select

    from nexus.core.config import get_settings
    from nexus.models.billing import PlatformAdmin
    from nexus.models.identity import User

    async with get_sessionmaker()() as session:
        user = await session.get(User, principal.user_id)
        if user is None:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "platform access denied")
        email = (user.email or "").lower()
        if email in get_settings().platform_admin_email_list:
            return principal
        row = (
            await session.scalars(
                select(PlatformAdmin).where(
                    PlatformAdmin.email == email, PlatformAdmin.active == True  # noqa: E712
                )
            )
        ).first()
    if row is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "platform access denied")
    return principal
