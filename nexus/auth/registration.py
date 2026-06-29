"""Two-step OTP registration service.

Flow:
  1. ``start_registration`` validates the request, stores a :class:`PendingRegistration` (with the
     password already bcrypt-hashed and the OTP stored only as an HMAC hash), and emails the code.
  2. ``verify_and_create`` checks the code (constant-time, with expiry + attempt limits) and, on
     success, provisions the real Tenant + User + Workspace + owner Membership and deletes the
     pending row.

All abuse controls live here: per-email overwrite (no stale-row lockout), resend cooldown + cap,
wrong-attempt cap, and short code TTL. The router maps :class:`RegistrationError` to HTTP codes.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from nexus.auth.otp import generate_otp, hash_otp, otp_secret, verify_otp
from nexus.core.config import get_settings
from nexus.core.db import utcnow
from nexus.core.security import hash_password
from nexus.models.identity import (
    Membership,
    PendingRegistration,
    Tenant,
    User,
    Workspace,
)

logger = logging.getLogger("nexus.auth.registration")

_MAX_RESENDS = 5  # hard cap on code emails per pending registration (anti-abuse / cost)


class RegistrationError(Exception):
    """A registration failure with the HTTP status the router should return."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(slots=True)
class StartResult:
    email: str
    expires_in_s: int
    resend_in_s: int


async def send_otp_email(to: str, code: str) -> bool:
    """Email a verification code via the system SMTP mailbox. Module-level so tests can patch it.

    Returns True on success. Never raises (the sender already swallows SMTP errors)."""
    from nexus.integrations.email_sender import send_email

    s = get_settings()
    cfg = {
        "provider": s.system_smtp_provider,
        "username": s.system_smtp_username,
        "password": s.system_smtp_password,
        "from_email": s.system_smtp_from or s.system_smtp_username,
        "from_name": s.system_smtp_from_name,
    }
    minutes = max(1, s.otp_ttl_s // 60)
    body = (
        "Welcome to Infojoy GTM.\n\n"
        f"Your verification code is: {code}\n\n"
        f"This code expires in {minutes} minute{'s' if minutes != 1 else ''}. "
        "If you didn't request this, you can safely ignore this email."
    )
    result = await send_email(cfg, to=to, subject="Your Infojoy GTM verification code", body=body)
    if not result.ok:
        logger.warning("OTP email to %s not sent: %s", to, result.detail)
    return result.ok


def _norm_email(email: str) -> str:
    return (email or "").strip().lower()


async def start_registration(
    db: AsyncSession,
    *,
    company_name: str,
    company_slug: str,
    full_name: str,
    email: str,
    password: str,
) -> StartResult:
    """Validate + persist a pending registration and email an OTP. Overwrites any prior pending
    row for the same email so a stale attempt never blocks a fresh one."""
    s = get_settings()
    email = _norm_email(email)
    slug = (company_slug or "").strip().lower()

    if (await db.scalars(select(Tenant).where(Tenant.slug == slug))).first():
        # Slugs are public workspace URLs, so reporting "taken" leaks nothing.
        raise RegistrationError(409, "Company slug already taken")
    if (await db.scalars(select(User).where(User.email == email))).first():
        # Enumeration-resistant: return the SAME 202 as success without creating a pending row
        # or sending a code, so an attacker can't probe which emails are registered.
        logger.info("register/start for already-registered email — returning generic response")
        return StartResult(email=email, expires_in_s=s.otp_ttl_s, resend_in_s=s.otp_resend_cooldown_s)

    existing = (
        await db.scalars(select(PendingRegistration).where(PendingRegistration.email == email))
    ).first()
    now = utcnow()
    if existing is not None:
        # Respect the resend cooldown even on a fresh "start" so it can't be used to bypass it.
        if existing.last_sent_at and (now - existing.last_sent_at).total_seconds() < s.otp_resend_cooldown_s:
            wait = int(s.otp_resend_cooldown_s - (now - existing.last_sent_at).total_seconds())
            raise RegistrationError(429, f"Please wait {max(1, wait)}s before requesting another code")
        await db.delete(existing)
        await db.flush()

    code = generate_otp(s.otp_length)
    pending = PendingRegistration(
        email=email,
        full_name=full_name.strip(),
        company_name=company_name.strip(),
        company_slug=slug,
        password_hash=hash_password(password),
        otp_hash=hash_otp(code, otp_secret()),
        expires_at=now + timedelta(seconds=s.otp_ttl_s),
        attempts=0,
        resends=0,
        last_sent_at=now,
    )
    db.add(pending)
    try:
        await db.flush()
    except IntegrityError:
        # Lost a concurrent race to create the same pending email — treat as "code already sent".
        await db.rollback()
        raise RegistrationError(429, "A verification code was just sent. Check your email.")

    await send_otp_email(email, code)
    await db.commit()
    return StartResult(email=email, expires_in_s=s.otp_ttl_s, resend_in_s=s.otp_resend_cooldown_s)


async def resend_otp(db: AsyncSession, *, email: str) -> StartResult:
    """Issue a fresh code for an in-flight registration, honoring the cooldown + resend cap."""
    s = get_settings()
    email = _norm_email(email)
    pending = (
        await db.scalars(select(PendingRegistration).where(PendingRegistration.email == email))
    ).first()
    if pending is None:
        raise RegistrationError(404, "No registration in progress for that email")

    now = utcnow()
    if pending.last_sent_at and (now - pending.last_sent_at).total_seconds() < s.otp_resend_cooldown_s:
        wait = int(s.otp_resend_cooldown_s - (now - pending.last_sent_at).total_seconds())
        raise RegistrationError(429, f"Please wait {max(1, wait)}s before requesting another code")
    if pending.resends >= _MAX_RESENDS:
        raise RegistrationError(429, "Too many codes requested. Start registration again later.")

    code = generate_otp(s.otp_length)
    pending.otp_hash = hash_otp(code, otp_secret())
    pending.expires_at = now + timedelta(seconds=s.otp_ttl_s)
    pending.attempts = 0  # a new code resets the wrong-attempt counter
    pending.resends += 1
    pending.last_sent_at = now
    await db.flush()
    await send_otp_email(email, code)
    await db.commit()
    return StartResult(email=email, expires_in_s=s.otp_ttl_s, resend_in_s=s.otp_resend_cooldown_s)


async def verify_and_create(db: AsyncSession, *, email: str, code: str) -> tuple[User, Tenant]:
    """Verify the OTP and, on success, provision the account. Raises RegistrationError otherwise."""
    s = get_settings()
    email = _norm_email(email)
    pending = (
        await db.scalars(select(PendingRegistration).where(PendingRegistration.email == email))
    ).first()
    if pending is None:
        raise RegistrationError(400, "Invalid or expired code")

    now = utcnow()
    if now > pending.expires_at:
        await db.delete(pending)
        await db.commit()
        raise RegistrationError(400, "Code expired. Request a new one.")
    if pending.attempts >= s.otp_max_attempts:
        await db.delete(pending)
        await db.commit()
        raise RegistrationError(429, "Too many incorrect attempts. Request a new code.")

    if not verify_otp(code, pending.otp_hash, otp_secret()):
        pending.attempts += 1
        remaining = s.otp_max_attempts - pending.attempts
        if remaining <= 0:
            await db.delete(pending)
            await db.commit()
            raise RegistrationError(429, "Too many incorrect attempts. Request a new code.")
        await db.commit()
        raise RegistrationError(400, f"Invalid code. {remaining} attempt{'s' if remaining != 1 else ''} left.")

    # Correct code — provision the tenant/user/workspace/owner membership, then drop the pending row.
    try:
        tenant = Tenant(name=pending.company_name, slug=pending.company_slug)
        db.add(tenant)
        await db.flush()
        user = User(email=pending.email, full_name=pending.full_name, password_hash=pending.password_hash)
        db.add(user)
        await db.flush()
        workspace = Workspace(tenant_id=tenant.id, name=f"{pending.company_name} Workspace")
        db.add(workspace)
        await db.flush()
        db.add(Membership(tenant_id=tenant.id, user_id=user.id, workspace_id=workspace.id, role="owner"))
        await db.delete(pending)
        await db.commit()
    except IntegrityError:
        # Someone claimed the slug/email between start and verify.
        await db.rollback()
        raise RegistrationError(409, "Company slug or email already registered")
    return user, tenant
