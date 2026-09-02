"""Auth endpoints: signup (provision a tenant), login (issue a JWT), and MFA.

Login is two-step **only** for a user who has confirmed a second factor. Everyone else — the
overwhelming majority, and every existing integration — gets exactly the response they always
got. That compatibility line is enforced by a single predicate,
``mfa_service.has_confirmed_mfa``, and by ``tests/test_mfa_login.py``.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nexus.api.deps import Principal, get_db_session, get_principal
from nexus.api.schemas import (
    ForgotPasswordRequest,
    LoginRequest,
    MessageResponse,
    MFAChallengeResendRequest,
    MFAChallengeResponse,
    MFACodeRequest,
    MFAConfirmRequest,
    MFAEnrollRequest,
    MFAEnrollResponse,
    MFARecoveryCodesResponse,
    MFAStatusResponse,
    MFAVerifyRequest,
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
from nexus.auth import mfa_service
from nexus.auth.sessions import current_token_version
from nexus.auth.mfa_service import MFAError
from nexus.auth.password_reset import request_password_reset, reset_password
from nexus.auth.registration import (
    RegistrationError,
    resend_otp,
    start_registration,
    verify_and_create,
)
from nexus.core.config import get_settings
from nexus.core.ratelimit import rate_limit
from nexus.core.security import (
    create_access_token,
    create_mfa_challenge_token,
    decode_mfa_challenge_token,
    hash_password,
    verify_password,
)
from nexus.billing.subscriptions import start_subscription_for
from nexus.models.identity import Membership, Tenant, User, Workspace

logger = logging.getLogger("nexus.api.auth")

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
    token = create_access_token(
        user_id=user.id, tenant_id=tenant.id, role="owner",
        token_version=await current_token_version(db, user.id),
    )
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
        # Start the workspace on the free plan, in the SAME transaction that creates it.
        # Without this a tenant exists with no subscription at all, the entitlement engine's
        # "no subscription -> allow" default grants it everything, and the startup backfill later
        # grandfathers it onto legacy-unlimited permanently. Never raises — see start_subscription.
        await start_subscription_for(db, tenant.id)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Company slug or email already registered"
        )

    token = create_access_token(
        user_id=user.id, tenant_id=tenant.id, role="owner",
        token_version=await current_token_version(db, user.id),
    )
    return TokenResponse(access_token=token, tenant_id=tenant.id, role="owner")


# ``response_model=None`` (not a union) on purpose. Declaring a union here would route every
# response through union validation and risk reshaping the one that must never change; leaving it
# off means the non-MFA branch is serialized straight from the same ``TokenResponse`` instance it
# always returned — identical keys, identical order, identical 200. The MFA branch is the only
# thing that looks different, and only for users who chose it.
@router.post("/login", response_model=None)
async def login(
    req: LoginRequest,
    db: AsyncSession = Depends(get_db_session),
    _rl: None = Depends(rate_limit("login")),
) -> TokenResponse | MFAChallengeResponse:
    user = (await db.scalars(select(User).where(User.email == req.email))).first()
    if user is None or not verify_password(req.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")

    # Checked AFTER the password, on purpose. Refusing a suspended account before verifying the
    # password turns this endpoint into an account-existence oracle: an attacker learns which
    # addresses are real by which ones answer differently. The extra hash costs a few milliseconds
    # on a path that is already rate-limited.
    #
    # A suspension that does not stop login is decorative — this is the whole control.
    if user.suspended_at is not None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "This account is suspended. Contact your workspace administrator.",
        )

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

    # The one branch. Only a *confirmed* second factor makes login two-step; a user who never
    # enrolled — or who enrolled and never confirmed — falls straight through to the response
    # below, unchanged.
    if await mfa_service.has_confirmed_mfa(db, user.id):
        return await _mfa_challenge(db, user, membership)

    token = create_access_token(
        user_id=user.id, tenant_id=membership.tenant_id, role=membership.role,
        token_version=await current_token_version(db, user.id),
    )
    return TokenResponse(access_token=token, tenant_id=membership.tenant_id, role=membership.role)


async def _mfa_challenge(
    db: AsyncSession, user: User, membership: Membership
) -> MFAChallengeResponse:
    """Hand back a second-factor challenge instead of a session.

    The membership resolved in step one is carried in the challenge so the second step lands in
    the same workspace the user asked for — but the role is re-read at verify time, never trusted
    from the challenge.
    """
    methods = await mfa_service.confirmed_methods(db, user.id)
    if "email" in methods:
        try:
            await mfa_service.send_challenge_code(db, user, "email")
        except MFAError:
            # A mail-delivery problem must not turn a correct password into a 500. The user can
            # ask for another code, or use their authenticator/recovery code.
            logger.warning("could not send MFA email code during login", exc_info=True)
    return MFAChallengeResponse(
        challenge_token=create_mfa_challenge_token(
            user_id=user.id, tenant_id=membership.tenant_id, role=membership.role
        ),
        methods=methods,
        expires_in_s=get_settings().mfa_challenge_ttl_s,
    )


@router.post("/mfa/verify", response_model=TokenResponse)
async def mfa_verify(
    req: MFAVerifyRequest,
    db: AsyncSession = Depends(get_db_session),
    _rl: None = Depends(rate_limit("mfa_verify")),
) -> TokenResponse:
    """Step two: exchange a challenge plus a second-factor code for a real session token."""
    claims = decode_mfa_challenge_token(req.challenge_token)
    if claims is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "That sign-in attempt expired. Start again."
        )
    user = await db.get(User, claims["sub"])
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Unknown user")

    # Re-read the membership rather than trusting the role baked into the challenge: between the
    # two steps an admin may have changed the role or revoked access entirely.
    membership = (
        await db.scalars(
            select(Membership).where(
                Membership.user_id == user.id, Membership.tenant_id == claims["tid"]
            )
        )
    ).first()
    if membership is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "No membership in the requested tenant")

    try:
        await mfa_service.verify_code(db, user.id, req.code, method=req.method)
    except MFAError as exc:
        raise HTTPException(exc.status_code, exc.detail)

    token = create_access_token(
        user_id=user.id, tenant_id=membership.tenant_id, role=membership.role,
        token_version=await current_token_version(db, user.id),
    )
    return TokenResponse(access_token=token, tenant_id=membership.tenant_id, role=membership.role)


@router.post(
    "/mfa/challenge/resend", response_model=MessageResponse, status_code=status.HTTP_202_ACCEPTED
)
async def mfa_challenge_resend(
    req: MFAChallengeResendRequest,
    db: AsyncSession = Depends(get_db_session),
    _rl: None = Depends(rate_limit("mfa_resend")),
) -> MessageResponse:
    """Re-send the mailed sign-in code for an in-flight challenge.

    The code is derived from the seed and the clock, so a resend inside the same step delivers the
    same digits — deliberately: it makes a lost email recoverable without invalidating the code
    the user may be about to type.
    """
    claims = decode_mfa_challenge_token(req.challenge_token)
    if claims is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "That sign-in attempt expired. Start again."
        )
    user = await db.get(User, claims["sub"])
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Unknown user")
    try:
        await mfa_service.send_challenge_code(db, user, req.method)
    except MFAError as exc:
        raise HTTPException(exc.status_code, exc.detail)
    return MessageResponse(message="A new sign-in code has been sent.")


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


# --------------------------------------------------------------------------------------------
# Multi-factor authentication (opt-in, per user)
#
# Enrolment is authenticated with an ordinary access token: you must already be signed in to add
# a second factor. Everything that *changes* the factor (confirm, disable, regenerate) also costs
# a live code, so a stolen session cannot quietly swap the second factor for the attacker's own.
# --------------------------------------------------------------------------------------------
async def _load_user(db: AsyncSession, user_id: str) -> User:
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Unknown user")
    return user


@router.get("/mfa", response_model=MFAStatusResponse)
async def mfa_status(
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db_session),
) -> MFAStatusResponse:
    """What the settings screen renders: which factors are live, which are half-enrolled."""
    factors = await mfa_service.confirmed_factors(db, principal.user_id)
    methods = await mfa_service.confirmed_methods(db, principal.user_id)
    pending = sorted(
        {f.method for f in await mfa_service.all_factors(db, principal.user_id)}
        - {f.method for f in factors}
    )
    return MFAStatusResponse(
        enabled=bool(methods),
        methods=methods,
        pending_methods=pending,
        recovery_codes_remaining=await mfa_service.unused_recovery_code_count(db, principal.user_id),
    )


@router.post("/mfa/enroll", response_model=MFAEnrollResponse, status_code=status.HTTP_201_CREATED)
async def mfa_enroll(
    req: MFAEnrollRequest,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db_session),
    _rl: None = Depends(rate_limit("mfa_enroll")),
) -> MFAEnrollResponse:
    """Begin enrolment. The factor does nothing until ``/auth/mfa/confirm`` proves a code works.

    For ``totp`` the seed and QR URI are in this response and nowhere else — the server stores
    only the sealed copy. Recovery codes come back on the first enrolment of any method.
    """
    user = await _load_user(db, principal.user_id)
    try:
        result = await mfa_service.enroll(db, user, req.method)
    except MFAError as exc:
        raise HTTPException(exc.status_code, exc.detail)
    return MFAEnrollResponse(
        method=result.method,
        secret=result.secret,
        provisioning_uri=result.provisioning_uri,
        recovery_codes=result.recovery_codes,
        code_sent=result.code_sent,
        expires_in_s=result.expires_in_s,
    )


@router.post("/mfa/confirm", response_model=MFAStatusResponse)
async def mfa_confirm(
    req: MFAConfirmRequest,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db_session),
    _rl: None = Depends(rate_limit("mfa_confirm")),
) -> MFAStatusResponse:
    """Verify the first code and arm the factor. Only now does login become two-step."""
    user = await _load_user(db, principal.user_id)
    try:
        methods = await mfa_service.confirm(db, user, req.method, req.code)
    except MFAError as exc:
        raise HTTPException(exc.status_code, exc.detail)
    return MFAStatusResponse(
        enabled=bool(methods),
        methods=methods,
        pending_methods=[],
        recovery_codes_remaining=await mfa_service.unused_recovery_code_count(db, user.id),
    )


@router.delete("/mfa", response_model=MessageResponse)
async def mfa_disable(
    req: MFACodeRequest,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db_session),
    _rl: None = Depends(rate_limit("mfa_disable")),
) -> MessageResponse:
    """Turn MFA off. Costs a valid current code (or a recovery code) — an access token alone is
    not enough, otherwise session theft would be a complete MFA bypass."""
    user = await _load_user(db, principal.user_id)
    try:
        await mfa_service.disable(db, user, req.code)
    except MFAError as exc:
        raise HTTPException(exc.status_code, exc.detail)
    return MessageResponse(message="Two-factor authentication has been turned off.")


@router.post("/mfa/recovery-codes/regenerate", response_model=MFARecoveryCodesResponse)
async def mfa_regenerate_recovery_codes(
    req: MFACodeRequest,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db_session),
    _rl: None = Depends(rate_limit("mfa_recovery")),
) -> MFARecoveryCodesResponse:
    """Mint a fresh set and invalidate every previous code. Requires a code from a real factor —
    a leaked printout must not be able to renew itself."""
    user = await _load_user(db, principal.user_id)
    try:
        codes = await mfa_service.regenerate_recovery_codes(db, user, req.code)
    except MFAError as exc:
        raise HTTPException(exc.status_code, exc.detail)
    return MFARecoveryCodesResponse(recovery_codes=codes)


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
        # Start the workspace on the free plan, in the SAME transaction that creates it.
        # Without this a tenant exists with no subscription at all, the entitlement engine's
        # "no subscription -> allow" default grants it everything, and the startup backfill later
        # grandfathers it onto legacy-unlimited permanently. Never raises — see start_subscription.
        await start_subscription_for(db, tenant.id)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "That workspace URL is already taken")

    token = create_access_token(
        user_id=principal.user_id, tenant_id=tenant.id, role="owner",
        token_version=await current_token_version(db, principal.user_id),
    )
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
        user_id=principal.user_id, tenant_id=membership.tenant_id, role=membership.role,
        token_version=await current_token_version(db, principal.user_id),
    )
    return TokenResponse(
        access_token=token, tenant_id=membership.tenant_id, role=membership.role
    )
