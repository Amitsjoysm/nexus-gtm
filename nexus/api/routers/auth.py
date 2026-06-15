"""Auth endpoints: signup (provision a tenant) and login (issue a JWT)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nexus.api.deps import Principal, get_db_session, get_principal
from nexus.api.schemas import (
    LoginRequest,
    NewWorkspaceRequest,
    SignupRequest,
    SwitchTenantRequest,
    TenantOut,
    TokenResponse,
)
from nexus.core.security import create_access_token, hash_password, verify_password
from nexus.models.identity import Membership, Tenant, User, Workspace

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/signup", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def signup(req: SignupRequest, db: AsyncSession = Depends(get_db_session)) -> TokenResponse:
    if (await db.scalars(select(Tenant).where(Tenant.slug == req.company_slug))).first():
        raise HTTPException(status.HTTP_409_CONFLICT, "Company slug already taken")
    if (await db.scalars(select(User).where(User.email == req.email))).first():
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")

    # The slug/email pre-checks above are check-then-insert: under concurrency two signups
    # can both pass them, and the loser hits the unique constraint at flush or COMMIT —
    # the latter normally fires in the session dependency AFTER this handler returns, i.e.
    # an unhandled 500. Guard the whole insert sequence and commit here so the race always
    # surfaces as a clean 409.
    try:
        tenant = Tenant(name=req.company_name, slug=req.company_slug)
        db.add(tenant)
        await db.flush()

        user = User(
            email=req.email, full_name=req.full_name, password_hash=hash_password(req.password)
        )
        db.add(user)
        await db.flush()

        workspace = Workspace(tenant_id=tenant.id, name=f"{req.company_name} Workspace")
        db.add(workspace)
        await db.flush()

        membership = Membership(
            tenant_id=tenant.id, user_id=user.id, workspace_id=workspace.id, role="owner"
        )
        db.add(membership)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Company slug or email already registered"
        )

    token = create_access_token(user_id=user.id, tenant_id=tenant.id, role="owner")
    return TokenResponse(access_token=token, tenant_id=tenant.id, role="owner")


@router.post("/login", response_model=TokenResponse)
async def login(req: LoginRequest, db: AsyncSession = Depends(get_db_session)) -> TokenResponse:
    user = (await db.scalars(select(User).where(User.email == req.email))).first()
    if user is None or not verify_password(req.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")

    memberships = list(
        (await db.scalars(select(Membership).where(Membership.user_id == user.id))).all()
    )
    if not memberships:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "User has no tenant memberships")

    membership = await _resolve_membership(db, memberships, req.tenant_slug)
    token = create_access_token(
        user_id=user.id, tenant_id=membership.tenant_id, role=membership.role
    )
    return TokenResponse(access_token=token, tenant_id=membership.tenant_id, role=membership.role)


async def _resolve_membership(
    db: AsyncSession, memberships: list[Membership], tenant_slug: str | None
) -> Membership:
    if len(memberships) == 1:
        return memberships[0]
    if not tenant_slug:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "User belongs to multiple tenants; specify tenant_slug",
        )
    tenant = (await db.scalars(select(Tenant).where(Tenant.slug == tenant_slug))).first()
    if tenant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown tenant '{tenant_slug}'")
    for m in memberships:
        if m.tenant_id == tenant.id:
            return m
    raise HTTPException(status.HTTP_403_FORBIDDEN, "No membership in the requested tenant")


@router.get("/tenants", response_model=list[TenantOut])
async def list_tenants(
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db_session),
) -> list[TenantOut]:
    """Tenants the authenticated user is a member of (for the workspace switcher)."""
    rows = (
        await db.execute(
            select(Tenant, Membership.role)
            .join(Membership, Membership.tenant_id == Tenant.id)
            .where(Membership.user_id == principal.user_id)
            .order_by(Tenant.name)
        )
    ).all()
    return [
        TenantOut(tenant_id=t.id, name=t.name, slug=t.slug, role=role) for (t, role) in rows
    ]


@router.post("/workspaces", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def create_workspace(
    req: NewWorkspaceRequest,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db_session),
) -> TokenResponse:
    """Provision a new tenant (workspace/org) owned by the authenticated user and switch into it.

    This is what makes the topbar workspace switcher useful for an existing user: signup only
    creates a tenant for a brand-new account, so without this a one-tenant owner could never
    reach a second workspace. Returns a fresh JWT pinned to the new tenant."""
    try:
        tenant = Tenant(name=req.name, slug=req.slug)
        db.add(tenant)
        await db.flush()

        workspace = Workspace(tenant_id=tenant.id, name=f"{req.name} Workspace")
        db.add(workspace)
        await db.flush()

        membership = Membership(
            tenant_id=tenant.id,
            user_id=principal.user_id,
            workspace_id=workspace.id,
            role="owner",
        )
        db.add(membership)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "That workspace URL is already taken")

    token = create_access_token(user_id=principal.user_id, tenant_id=tenant.id, role="owner")
    return TokenResponse(access_token=token, tenant_id=tenant.id, role="owner")


@router.post("/switch", response_model=TokenResponse)
async def switch_tenant(
    req: SwitchTenantRequest,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db_session),
) -> TokenResponse:
    """Re-verify membership server-side and re-issue a JWT pinned to the requested tenant."""
    membership = (
        await db.scalars(
            select(Membership).where(
                Membership.user_id == principal.user_id,
                Membership.tenant_id == req.tenant_id,
            )
        )
    ).first()
    if membership is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "No membership in the requested tenant")
    token = create_access_token(
        user_id=principal.user_id, tenant_id=membership.tenant_id, role=membership.role
    )
    return TokenResponse(
        access_token=token, tenant_id=membership.tenant_id, role=membership.role
    )
