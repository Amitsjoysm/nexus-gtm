"""Auth endpoints: signup (provision a tenant) and login (issue a JWT)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nexus.api.deps import Principal, get_db_session, get_principal
from nexus.api.schemas import (
    ForgotPasswordRequest,
    LoginRequest,
    MessageResponse,
    NewWorkspaceRequest,
    RegisterResendRequest,
    RegisterStartRequest,
    RegisterStartResponse,
    RegisterVerifyRequest,
    ResetPasswordRequest,
    SignupRequest,
    SwitchTenantRequest,
    TenantOut,
    TokenResponse,
)
from nexus.auth.password_reset import request_password_reset, reset_password
from nexus.auth.registration import (
    RegistrationError,
    resend_otp,
    start_registration,
    verify_and_create,
)
from nexus.core.config import get_settings
from nexus.core.ratelimit import rate_limit
from nexus.core.security import create_access_token, hash_password, verify_password
from nexus.models.identity import Membership, Tenant, User, Workspace

router = APIRouter(prefix="/auth", tags=["auth"])

# Generic acknowledgement reused by the forgot/reset flows so responses never reveal whether an
# email is registered.
_RESET_ACK = "If an account exists for that email, a password-reset link has been sent."


@router.post(
    "/register/start", response_model=RegisterStartResponse, status_code=status.HTTP_202_ACCEPTED
)
async def register_start(
    req: RegisterStartRequest,
    db: AsyncSession = Depends(get_db_session),
    _rl: None = Depends(rate_limit("register_start")),
) -> RegisterStartResponse:
    """Step 1: validate the signup and email a one-time code. No account is created yet."""
    try:
        result = await start_registration(
            db,
            company_name=req.company_name,
            company_slug=req.company_slug,
            full_name=req.full_name,
            email=req.email,
            password=req.password,
        )
    except RegistrationError as exc:
        raise HTTPException(exc.status_code, exc.detail)
    return RegisterStartResponse(
        email=result.email, expires_in_s=result.expires_in_s, resend_in_s=result.resend_in_s
    )


@router.post(
    "/register/resend", response_model=RegisterStartResponse, status_code=status.HTTP_202_ACCEPTED
)
async def register_resend(
    req: RegisterResendRequest,
    db: AsyncSession = Depends(get_db_session),
    _rl: None = Depends(rate_limit("register_resend")),
) -> RegisterStartResponse:
    """Re-send the verification code for an in-flight registration (cooldown-limited)."""
    try:
        result = await resend_otp(db, email=req.email)
    except RegistrationError as exc:
        raise HTTPException(exc.status_code, exc.detail)
    return RegisterStartResponse(
        email=result.email, expires_in_s=result.expires_in_s, resend_in_s=result.resend_in_s
    )


@router.post(
    "/register/verify", response_model=TokenResponse, status_code=status.HTTP_201_CREATED
)
async def register_verify(
    req: RegisterVerifyRequest,
    db: AsyncSession = Depends(get_db_session),
    _rl: None = Depends(rate_limit("register_verify")),
) -> TokenResponse:
    """Step 2: verify the code and provision the tenant + owner. Returns a session token."""
    try:
        user, tenant = await verify_and_create(db, email=req.email, code=req.code)
    except RegistrationError as exc:
        raise HTTPException(exc.status_code, exc.detail)
    token = create_access_token(user_id=user.id, tenant_id=tenant.id, role="owner")
    return TokenResponse(access_token=token, tenant_id=tenant.id, role="owner")


@router.post("/signup", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def signup(req: SignupRequest, db: AsyncSession = Depends(get_db_session)) -> TokenResponse:
    # When OTP registration is enabled, direct single-step signup is closed: clients must go
    # through /auth/register/start -> /auth/register/verify so every account is email-verified.
    if get_settings().otp_registration_enabled:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Email verification required. Use /auth/register/start to receive a code.",
        )
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
async def login(
    req: LoginRequest,
    db: AsyncSession = Depends(get_db_session),
    _rl: None = Depends(rate_limit("login")),
) -> TokenResponse:
    user = (await db.scalars(select(User).where(User.email == req.email))).first()
    if user is None or not verify_password(req.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")

    memberships = list(
        (
            await db.scalars(
                select(Membership)
                .where(Membership.user_id == user.id)
                .order_by(Membership.created_at.asc())  # earliest = the user's home workspace
            )
        ).all()
    )
    if not memberships:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "User has no tenant memberships")

    membership = await _resolve_membership(db, memberships, req.tenant_slug)
    token = create_access_token(
        user_id=user.id, tenant_id=membership.tenant_id, role=membership.role
    )
    return TokenResponse(access_token=token, tenant_id=membership.tenant_id, role=membership.role)


@router.post("/forgot-password", response_model=MessageResponse, status_code=status.HTTP_202_ACCEPTED)
async def forgot_password(
    req: ForgotPasswordRequest,
    db: AsyncSession = Depends(get_db_session),
    _rl: None = Depends(rate_limit("forgot_password")),
) -> MessageResponse:
    """Email a single-use password-reset link to the account holder. Always returns the same
    generic acknowledgement so it can't be used to discover which emails are registered."""
    await request_password_reset(db, email=req.email)
    return MessageResponse(message=_RESET_ACK)


@router.post("/reset-password", response_model=MessageResponse)
async def reset_password_endpoint(
    req: ResetPasswordRequest,
    db: AsyncSession = Depends(get_db_session),
    _rl: None = Depends(rate_limit("reset_password")),
) -> MessageResponse:
    """Complete a password reset using the emailed token. The token is single-use and time-boxed."""
    try:
        await reset_password(db, email=req.email, token=req.token, new_password=req.new_password)
    except RegistrationError as exc:
        raise HTTPException(exc.status_code, exc.detail)
    return MessageResponse(message="Your password has been reset. You can now sign in.")


async def _resolve_membership(
    db: AsyncSession, memberships: list[Membership], tenant_slug: str | None
) -> Membership:
    """Pick which workspace a login lands in.

    Without an explicit tenant_slug, default to the user's first (home) workspace and let them
    switch in-app afterwards — NEVER error. Erroring here was a lockout: the moment a user
    belonged to a second workspace, plain email+password login (the only thing the login form
    sends) 400'd and they could not get in at all. ``memberships`` is pre-ordered by created_at.
    """
    if tenant_slug is None:
        return memberships[0]
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
