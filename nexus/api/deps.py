"""FastAPI dependencies: DB sessions, authentication, tenant binding, permission checks."""
from __future__ import annotations

import logging

from dataclasses import dataclass
from typing import AsyncIterator, Callable

from fastapi import Depends, HTTPException, Request, status
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

logger = logging.getLogger("nexus.api.deps")

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


# user_id -> (token_version, monotonic deadline). A revocation is a rare event and this is on
# every authenticated request, so it is read through a short TTL rather than per request. The TTL
# is the worst-case delay between revoking and the token stopping — seconds, against the 60
# minutes it was before this existed. Same 30s idiom as the runtime-config and provider-key
# resolvers, and bounded so a large estate cannot grow it without limit.
_SESSION_VERSION_TTL_S = 30.0
_SESSION_VERSION_MAX = 50_000
_session_versions: dict[str, tuple[int, float]] = {}


def clear_session_version_cache() -> None:
    """Drop the cache (revocation endpoints and tests call this for an immediate effect)."""
    _session_versions.clear()


async def _live_token_version(user_id: str) -> int:
    import time

    cached = _session_versions.get(user_id)
    now = time.monotonic()
    if cached is not None and cached[1] > now:
        return cached[0]
    from nexus.auth.sessions import current_token_version

    async with get_sessionmaker()() as session:
        version = await current_token_version(session, user_id)
    if len(_session_versions) >= _SESSION_VERSION_MAX:
        _session_versions.clear()
    _session_versions[user_id] = (version, now + _SESSION_VERSION_TTL_S)
    return version


async def get_principal(
    creds: HTTPAuthorizationCredentials = Depends(_bearer),
) -> Principal:
    payload = decode_access_token(creds.credentials)
    if not payload or "sub" not in payload or "tid" not in payload:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")

    # Session revocation. A token carrying no `tv` claim was issued by the release before this
    # existed and is accepted on purpose — refusing them would log every active user out at the
    # moment of deploy. They age out within one token TTL on their own.
    claimed = payload.get("tv")
    if claimed is not None:
        try:
            if int(claimed) < await _live_token_version(payload["sub"]):
                raise HTTPException(
                    status.HTTP_401_UNAUTHORIZED, "Session ended. Please sign in again."
                )
        except HTTPException:
            raise
        except Exception:
            # A lookup failure must not lock everyone out of the product. Degrading to "accept"
            # restores the pre-existing behaviour rather than inventing a new outage.
            logger.warning("token version check failed for %s", payload["sub"], exc_info=True)

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
    # Converge this process onto any runtime setting changed elsewhere. The API runs uvicorn with
    # two workers and may run several replicas, each holding its OWN `Settings` singleton — a change
    # applied by whichever process handled the write reaches the others only if something on their
    # request path re-reads it. Without this an API replica keeps serving the old value until it
    # restarts, and the panel honestly reports "saved, not yet live" forever.
    #
    # TTL-guarded, so on all but one request in thirty seconds this is a monotonic clock comparison.
    await _refresh_runtime_config()

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


# HTTP methods that cannot change state. An impersonation session is limited by METHOD, not by
# permission name: this codebase's RBAC is coarse — `manage_accounts` gates both *listing* and
# *creating* accounts — so refusing by permission blocked GET /api/accounts, which is exactly the
# read an admin impersonates in order to perform. Verified live; the permission-based version
# looked correct in unit tests and was useless in practice.
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


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

    def _checker(
        request: Request, principal: Principal = Depends(get_principal)
    ) -> Principal:
        if not has_permission(Role(principal.role), permission):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, f"Role '{principal.role}' lacks {permission.value}"
            )
        # An impersonation session may read whatever the user can read, and change nothing.
        # Checked here because every RBAC-gated tenant endpoint passes through this, so no route
        # can quietly skip it — and on the METHOD, because the permissions are too coarse to say
        # whether a given call reads or writes.
        if principal.read_only and request.method not in _SAFE_METHODS:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "this is a read-only impersonation session; end it to make changes",
            )
        return principal

    return _checker


async def _optional_principal(request: Request) -> "Principal | None":
    """The caller's principal, or None when there is no usable token.

    The staff gates need this because `get_principal` raises 403/401 BEFORE they run, and that
    answers the attacker's question: an anonymous probe got 403 for a real admin route and 404 for
    an invented one, so the whole staff surface could be enumerated without any credential at all.

    Resolving the principal ourselves lets the gate return the SAME 404 whether the caller is
    anonymous, holds a normal user token, or holds an expired one. Never raises.
    """
    header = request.headers.get("authorization") or ""
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    try:
        payload = decode_access_token(token.strip())
        if not payload or "sub" not in payload or "tid" not in payload:
            return None
        claimed = payload.get("tv")
        if claimed is not None and int(claimed) < await _live_token_version(payload["sub"]):
            return None
        return Principal(
            user_id=payload["sub"], tenant_id=payload["tid"], role=payload.get("role", "rep"),
        )
    except Exception:
        return None


async def require_platform_admin(
    # Optional for the same reason as `require_platform_permission`: `get_principal` raises before
    # this runs, which let an anonymous caller distinguish a real admin route (403) from an
    # invented one (404) and enumerate the staff surface with no credential.
    principal: "Principal | None" = Depends(_optional_principal),
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

    held = await platform_permissions(principal) if principal is not None else set()
    if not held:
        # Not staff at all -> 404. Same reasoning as `require_platform_permission`: a 403 confirms
        # the route exists, and this gate still guards endpoints that were never individually
        # annotated, so leaving it at 403 would keep the enumeration leak open through whichever
        # routes have not been migrated yet.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")
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


async def _refresh_runtime_config() -> None:
    """Re-apply runtime overrides onto this process, at most once per TTL. Never raises.

    A configuration refresh must never fail a request that had nothing to do with configuration.
    """
    try:
        from nexus.runtime_config.service import refresh_if_stale

        await refresh_if_stale()
    except Exception:
        logger.debug("runtime config refresh skipped", exc_info=True)


def require_platform_permission(permission: str):
    """Gate a staff endpoint on ONE named permission.

    Separate from ``require_platform_admin`` on purpose: that dependency is retained as the
    equivalent of ``billing.read`` so an endpoint's behaviour changes only when it is
    individually annotated. Nobody loses access during the rollout.
    """

    async def _dep(
        request: Request,
        # Deliberately NOT `Depends(get_principal)`: that raises 401/403 before this body runs, and
        # an anonymous probe then got 403 for a real admin route and 404 for an invented one — the
        # whole staff surface enumerable with no credential at all. Resolving it here lets every
        # non-admin caller receive the SAME 404, anonymous or not.
        principal: "Principal | None" = Depends(_optional_principal),
    ) -> Principal:
        # Origin first, and deliberately cheap: it reads a module-level cache, so an unlisted
        # address is refused before it can spend a database query on permissions.
        from nexus.api.deps_ip import check_admin_origin

        # Before the origin check, so an allowlist changed on another process is the one enforced.
        await _refresh_runtime_config()
        check_admin_origin(request)

        held = await platform_permissions(principal) if principal is not None else set()
        if not held:
            # NOT A PLATFORM ADMIN AT ALL -> 404, indistinguishable from a route that was never
            # registered.
            #
            # A 403 answers the attacker's question. Enumerating /api/admin/... against a
            # deployment that returns 403 for real paths and 404 for invented ones hands over a
            # complete map of the staff surface — provider keys, payment credentials, the runtime
            # panel — without a single valid credential. The interactive docs are already off
            # outside local/test for exactly this reason; a status code leaks the same thing more
            # slowly.
            #
            # The distinction below is deliberate: someone who has PROVEN they are staff keeps
            # informative errors, because they already know the surface exists and a 404 would turn
            # a permission problem into a hunt for a missing deployment.
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")
        if permission not in held:
            # A known platform admin missing ONE permission. Same message as the plain admin gate:
            # they learn nothing by probing that they could not learn by trying, but they are told
            # it is an authorisation problem rather than sent looking for a route that plainly
            # exists.
            raise HTTPException(status.HTTP_403_FORBIDDEN, "platform access denied")
        return principal

    return _dep
