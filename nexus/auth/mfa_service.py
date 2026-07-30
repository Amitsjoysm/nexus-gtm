# nexus/auth/mfa_service.py
"""MFA enrolment, verification, and lockout — the stateful half of the second factor.

The primitives live in :mod:`nexus.auth.mfa`; this module owns the ``user_mfa`` /
``mfa_recovery_codes`` rows and the policy around them. The router is a thin translation of
:class:`MFAError` into HTTP, exactly as the registration flow does.

Policy decisions worth knowing:

* **An unconfirmed enrolment is inert.** Only ``confirmed_at is not null`` factors are returned by
  :func:`confirmed_methods`, so a user who scanned a QR code and then closed the tab is not locked
  out of their own account.
* **Re-enrolling a confirmed method is refused.** A stolen session must not be able to silently
  swap the second factor for the attacker's own; rotating requires disabling first, and disabling
  requires a current code.
* **Verification is one function** (:func:`verify_code`) for confirm, disable, regenerate, and
  login. One place holds the replay guard, the recovery-code fallback and the lockout counter, so
  none of the four paths can quietly be the weak one.
* **Lockout is persisted, per user, across every factor.** An attacker who exhausts the budget
  against email OTP must not get a fresh budget by switching to TOTP.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from nexus.auth.mfa import (
    code_for_counter,
    counter_at,
    generate_recovery_codes,
    generate_secret,
    hash_recovery_code,
    provisioning_uri,
    seal_secret,
    unseal_secret,
    verify_recovery_code,
    verify_totp,
)
from nexus.core.config import get_settings
from nexus.core.db import utcnow
from nexus.models.identity import User
from nexus.models.mfa import MFA_METHODS, MFARecoveryCode, UserMFA

logger = logging.getLogger("nexus.auth.mfa")


class MFAError(Exception):
    """An MFA failure carrying the HTTP status the router should return."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(slots=True)
class EnrollResult:
    method: str
    # Populated for TOTP only — the seed and the QR payload are meaningless for a mailed code.
    secret: str | None = None
    provisioning_uri: str | None = None
    # Returned exactly once, at the enrolment that mints them. Never retrievable afterwards.
    recovery_codes: list[str] = field(default_factory=list)
    code_sent: bool = False
    expires_in_s: int = 0


def _step_for(method: str) -> int:
    s = get_settings()
    return s.mfa_email_code_step_s if method == "email" else s.mfa_totp_step_s


async def all_factors(db: AsyncSession, user_id: str) -> list[UserMFA]:
    """Every enrolled factor, confirmed or not."""
    return list((await db.scalars(select(UserMFA).where(UserMFA.user_id == user_id))).all())


async def confirmed_factors(db: AsyncSession, user_id: str) -> list[UserMFA]:
    """Only the factors that actually gate a login."""
    return [f for f in await all_factors(db, user_id) if f.confirmed_at is not None]


async def confirmed_methods(db: AsyncSession, user_id: str) -> list[str]:
    """Method names the user can be challenged with, in a stable order."""
    names = {f.method for f in await confirmed_factors(db, user_id)}
    return [m for m in MFA_METHODS if m in names]


async def has_confirmed_mfa(db: AsyncSession, user_id: str) -> bool:
    """Whether login must take the two-step path for this user.

    This is the single predicate that decides whether the login response changes shape. Everyone
    else keeps the original one-step response, byte for byte.
    """
    row = (
        await db.scalars(
            select(UserMFA.id)
            .where(UserMFA.user_id == user_id, UserMFA.confirmed_at.is_not(None))
            .limit(1)
        )
    ).first()
    return row is not None


async def unused_recovery_code_count(db: AsyncSession, user_id: str) -> int:
    rows = (
        await db.scalars(
            select(MFARecoveryCode.id).where(
                MFARecoveryCode.user_id == user_id, MFARecoveryCode.used_at.is_(None)
            )
        )
    ).all()
    return len(rows)


async def send_mfa_code_email(to: str, code: str) -> bool:
    """Email a sign-in code. Module-level so tests can patch it (as the registration flow does).

    Never raises — the sender already swallows SMTP errors. A send failure must not 500 the login
    endpoint; the user simply does not receive a code and can retry.
    """
    from nexus.integrations.email_sender import send_email

    s = get_settings()
    cfg = {
        "provider": s.system_smtp_provider,
        "username": s.system_smtp_username,
        "password": s.system_smtp_password,
        "from_email": s.system_smtp_from or s.system_smtp_username,
        "from_name": s.system_smtp_from_name,
    }
    minutes = max(1, s.mfa_email_code_step_s // 60)
    body = (
        f"Your InfoJoy GTM sign-in code is: {code}\n\n"
        f"It expires in about {minutes} minute{'s' if minutes != 1 else ''}. "
        "If you didn't try to sign in, change your password — someone has it."
    )
    result = await send_email(cfg, to=to, subject="Your InfoJoy GTM sign-in code", body=body)
    if not result.ok:
        logger.warning("MFA code email to %s not sent: %s", to, result.detail)
    return result.ok


async def _issue_email_code(db: AsyncSession, user: User, factor: UserMFA) -> bool:
    """Mail a code the user can actually use.

    The code is derived, never stored: the seed plus the clock reproduce it, so there is no
    in-flight row to leak, expire, or forget to clean up. The one wrinkle is the replay guard —
    if this step's counter has already been spent (confirming enrolment, then signing in a minute
    later), mailing that code again would deliver digits the guard is bound to refuse and the user
    would be stuck until the step rolled over. So skip to the next unspent counter, which the ±1
    drift window still accepts.
    """
    s = get_settings()
    secret = unseal_secret(factor.secret)
    if not secret:
        logger.error("MFA email factor %s has an unreadable secret", factor.id)
        raise MFAError(500, "Second factor is misconfigured. Re-enrol to continue.")
    counter = counter_at(time.time(), s.mfa_email_code_step_s)
    if factor.last_used_counter is not None and counter <= factor.last_used_counter:
        counter = factor.last_used_counter + 1
    return await send_mfa_code_email(
        user.email, code_for_counter(secret, counter, digits=s.mfa_totp_digits)
    )


async def send_challenge_code(db: AsyncSession, user: User, method: str) -> bool:
    """(Re)send the code for a confirmed method that needs one. No-op for app-based TOTP."""
    if method != "email":
        return False
    factor = next(
        (f for f in await confirmed_factors(db, user.id) if f.method == "email"), None
    )
    if factor is None:
        raise MFAError(400, "Email codes are not enabled for this account")
    return await _issue_email_code(db, user, factor)


async def enroll(db: AsyncSession, user: User, method: str) -> EnrollResult:
    """Start enrolment for ``method``. The factor stays unconfirmed until a code is verified."""
    s = get_settings()
    method = (method or "").strip().lower()
    if method not in MFA_METHODS:
        raise MFAError(400, f"Unknown MFA method '{method}'. Use one of: {', '.join(MFA_METHODS)}")

    existing = next((f for f in await all_factors(db, user.id) if f.method == method), None)
    if existing is not None and existing.confirmed_at is not None:
        # Rotating a live factor from an authenticated session alone would let a stolen session
        # replace the victim's second factor with the attacker's. Disable first — which costs a
        # valid current code.
        raise MFAError(409, f"{method} is already enabled. Disable it first to re-enrol.")

    secret = generate_secret()
    if existing is None:
        factor = UserMFA(user_id=user.id, method=method, secret=seal_secret(secret))
        db.add(factor)
        try:
            await db.flush()
        except IntegrityError:
            # Lost a concurrent enrol race for the same (user, method).
            await db.rollback()
            raise MFAError(409, "An enrolment for that method is already in progress")
    else:
        # Re-starting an *unconfirmed* enrolment: fresh seed, cleared counters. Reusing the old
        # seed would mean a code from an abandoned QR scan still works.
        existing.secret = seal_secret(secret)
        existing.last_used_counter = None
        existing.last_used_at = None
        existing.failed_attempts = 0
        existing.locked_until = None
        factor = existing
        await db.flush()

    # Recovery codes are minted once, on the first enrolment of any method — an email-only user
    # needs a break-glass path just as much as a TOTP user does. Enrolling a second method does
    # not silently invalidate the codes the user has already written down.
    codes: list[str] = []
    if await unused_recovery_code_count(db, user.id) == 0:
        codes = await _replace_recovery_codes(db, user.id)

    result = EnrollResult(method=method, recovery_codes=codes)
    if method == "totp":
        result.secret = secret
        result.provisioning_uri = provisioning_uri(
            secret,
            account_name=user.email,
            issuer=s.mfa_issuer,
            digits=s.mfa_totp_digits,
            step=s.mfa_totp_step_s,
        )
    else:
        result.code_sent = await _issue_email_code(db, user, factor)
        result.expires_in_s = s.mfa_email_code_step_s
    await db.commit()
    return result


async def confirm(db: AsyncSession, user: User, method: str, code: str) -> list[str]:
    """Verify the first code for a pending enrolment and arm the factor. Returns live methods."""
    method = (method or "").strip().lower()
    factor = next((f for f in await all_factors(db, user.id) if f.method == method), None)
    if factor is None:
        raise MFAError(404, f"No pending {method or 'MFA'} enrolment. Start with /auth/mfa/enroll.")
    if factor.confirmed_at is not None:
        raise MFAError(409, f"{method} is already enabled")

    # Confirmation checks the pending factor specifically, and deliberately does NOT accept a
    # recovery code: proving you own the printout is not proof the authenticator works.
    if not _check_factor(factor, code):
        _register_failure(await all_factors(db, user.id))
        await db.commit()
        raise MFAError(400, "That code is not valid. Check the time on your device and try again.")

    factor.confirmed_at = utcnow()
    await db.commit()
    return await confirmed_methods(db, user.id)


async def disable(db: AsyncSession, user: User, code: str) -> None:
    """Turn MFA off for the user. Requires a current code or a recovery code."""
    factors = await confirmed_factors(db, user.id)
    if not factors:
        raise MFAError(404, "MFA is not enabled for this account")
    await verify_code(db, user.id, code)  # raises on failure / lockout
    await _purge(db, user.id)
    await db.commit()


async def regenerate_recovery_codes(db: AsyncSession, user: User, code: str) -> list[str]:
    """Mint a fresh set, invalidating every previous code. Requires a current code."""
    if not await confirmed_factors(db, user.id):
        raise MFAError(404, "MFA is not enabled for this account")
    # allow_recovery=False: a leaked printout must not be able to mint itself a new printout.
    await verify_code(db, user.id, code, allow_recovery=False)
    codes = await _replace_recovery_codes(db, user.id)
    await db.commit()
    return codes


async def admin_reset(db: AsyncSession, user_id: str) -> dict:
    """Clear every factor and recovery code for a user (platform-admin account recovery).

    Returns a before-snapshot for the audit log. Does **not** commit: the caller commits the
    reset and its audit row together, so an unaudited reset cannot happen.
    """
    factors = await all_factors(db, user_id)
    before = {
        "methods": ",".join(sorted(f.method for f in factors)),
        "confirmed": ",".join(sorted(f.method for f in factors if f.confirmed_at)),
        "recovery_codes_unused": await unused_recovery_code_count(db, user_id),
    }
    await _purge(db, user_id)
    return before


async def verify_code(
    db: AsyncSession,
    user_id: str,
    code: str,
    *,
    method: str | None = None,
    allow_recovery: bool = True,
) -> str:
    """Check ``code`` against the user's confirmed factors. Returns the method that matched
    (``"recovery"`` for a break-glass code). Raises :class:`MFAError` on failure or lockout.

    Commits on success and on failure — the spent replay counter and the incremented failure
    counter must both survive whatever the caller does next, including raising.
    """
    s = get_settings()
    factors = await confirmed_factors(db, user_id)
    if not factors:
        raise MFAError(404, "MFA is not enabled for this account")

    now = utcnow()
    locked = [f for f in factors if f.locked_until is not None and f.locked_until > now]
    if locked:
        wait = max(int((f.locked_until - now).total_seconds()) for f in locked)
        raise MFAError(429, f"Too many incorrect codes. Try again in {max(1, wait)}s.")

    candidates = [f for f in factors if method is None or f.method == method]
    if method is not None and not candidates:
        raise MFAError(400, f"'{method}' is not enabled for this account")

    for factor in candidates:
        if _check_factor(factor, code):
            factor.failed_attempts = 0
            factor.locked_until = None
            await db.commit()
            return factor.method

    if allow_recovery and await _consume_recovery_code(db, user_id, code):
        for f in factors:
            f.failed_attempts = 0
            f.locked_until = None
        await db.commit()
        return "recovery"

    remaining = _register_failure(factors)
    await db.commit()
    if remaining <= 0:
        raise MFAError(
            429, f"Too many incorrect codes. Try again in {s.mfa_lockout_s}s."
        )
    raise MFAError(400, f"Invalid code. {remaining} attempt{'s' if remaining != 1 else ''} left.")


# --------------------------------------------------------------------------------------------
# internals
# --------------------------------------------------------------------------------------------
def _check_factor(factor: UserMFA, code: str) -> bool:
    """Verify one factor and, on success, advance its replay counter in memory.

    The caller is responsible for committing; every path through :func:`verify_code` and
    :func:`confirm` does. If the counter were not persisted the replay guard would be decorative.
    """
    s = get_settings()
    secret = unseal_secret(factor.secret)
    if not secret:
        logger.error("MFA factor %s has an unreadable secret", factor.id)
        return False
    counter = verify_totp(
        secret,
        code,
        step=_step_for(factor.method),
        digits=s.mfa_totp_digits,
        drift=s.mfa_totp_drift_steps,
        last_counter=factor.last_used_counter,
    )
    if counter is None:
        return False
    factor.last_used_counter = counter
    factor.last_used_at = utcnow()
    return True


def _register_failure(factors: list[UserMFA]) -> int:
    """Count one wrong code against every factor the user has, and lock them all once the budget
    is gone. Applied across factors on purpose: an attacker must not refill the budget by
    switching methods. Returns attempts remaining (0 or less = now locked)."""
    s = get_settings()
    remaining = s.mfa_max_attempts
    for f in factors:
        f.failed_attempts = (f.failed_attempts or 0) + 1
        remaining = min(remaining, s.mfa_max_attempts - f.failed_attempts)
    if remaining <= 0:
        until = utcnow() + timedelta(seconds=s.mfa_lockout_s)
        for f in factors:
            f.locked_until = until
            f.failed_attempts = 0  # the lock is the penalty; the counter restarts after it lifts
    return remaining


async def _consume_recovery_code(db: AsyncSession, user_id: str, code: str) -> bool:
    """Spend a matching unused recovery code. Marked used, never deleted."""
    rows = (
        await db.scalars(
            select(MFARecoveryCode).where(
                MFARecoveryCode.user_id == user_id, MFARecoveryCode.used_at.is_(None)
            )
        )
    ).all()
    for row in rows:
        if verify_recovery_code(code, row.code_hash):
            row.used_at = utcnow()
            return True
    return False


async def _replace_recovery_codes(db: AsyncSession, user_id: str) -> list[str]:
    """Delete every existing code and mint a fresh set. Plaintext is returned to the caller once
    and never persisted; only the digests are stored."""
    for row in (
        await db.scalars(select(MFARecoveryCode).where(MFARecoveryCode.user_id == user_id))
    ).all():
        await db.delete(row)
    await db.flush()
    codes = generate_recovery_codes(get_settings().mfa_recovery_code_count)
    for c in codes:
        db.add(MFARecoveryCode(user_id=user_id, code_hash=hash_recovery_code(c)))
    await db.flush()
    return codes


async def _purge(db: AsyncSession, user_id: str) -> None:
    for factor in await all_factors(db, user_id):
        await db.delete(factor)
    for row in (
        await db.scalars(select(MFARecoveryCode).where(MFARecoveryCode.user_id == user_id))
    ).all():
        await db.delete(row)
    await db.flush()
