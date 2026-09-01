"""Forgot / reset password service.

  * ``request_password_reset`` — issues a single-use, time-boxed token for a *verified* account and
    emails a reset link. Always returns the same generic result (enumeration-resistant).
  * ``reset_password`` — verifies the token (constant-time, with expiry + single-use) and sets the
    new password.

Token security mirrors the OTP design: a high-entropy ``secrets.token_urlsafe`` value, stored only
as an HMAC hash, never plaintext, never reversible.
"""
from __future__ import annotations

import logging
import secrets
from datetime import timedelta
from urllib.parse import urlencode

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nexus.auth.otp import hash_otp, otp_secret, verify_otp
from nexus.auth.registration import RegistrationError
from nexus.core.config import get_settings
from nexus.core.db import utcnow
from nexus.core.security import hash_password
from nexus.models.identity import PasswordReset, User

logger = logging.getLogger("nexus.auth.password_reset")


def _norm_email(email: str) -> str:
    return (email or "").strip().lower()


def build_reset_email(link: str) -> tuple[str, str, str]:
    """``(subject, text, html)`` for the password-reset email.

    A BUTTON rather than a bare URL. A raw reset URL is long, opaque and full of query parameters —
    it reads exactly like the phishing attempt people are warned about, in the one message they are
    most primed to distrust. A styled call to action is what a real product sends.

    Both parts are built here, together, because they have to agree: the HTML shows the button, and
    the TEXT carries the URL. That is not redundancy —

    * corporate mail gateways strip HTML and some clients render text only, so an HTML-only reset
      email is unopenable for those users, and this is the message that must not fail;
    * if the button does not render or a client blocks it, the URL in the text part is the user's
      only route back into their account.

    The markup is table-based with inline styles because email clients are not browsers: Outlook
    ignores `<style>` blocks and most modern layout CSS, so a `<div>` styled as a button collapses
    to unstyled text there — which is the failure this change exists to remove. An anchor, not a
    `<button>`: a `<button>` outside a form does nothing in a mail client.
    """
    minutes = max(1, get_settings().password_reset_ttl_s // 60)
    subject = "Reset your InfoJoy GTM password"

    text = (
        "We received a request to reset your InfoJoy GTM password.\n\n"
        f"Reset it here (the link expires in {minutes} minutes):\n{link}\n\n"
        "If you didn't request this, you can safely ignore this email — your password is unchanged."
    )

    html = f"""<html><body style="margin:0;padding:0;background:#f5f6f8;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
         style="background:#f5f6f8;padding:32px 12px;">
    <tr><td align="center">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
             style="max-width:520px;background:#ffffff;border-radius:8px;padding:32px;
                    font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
                    color:#14181f;">
        <tr><td style="font-size:18px;font-weight:600;padding-bottom:12px;">
          Reset your password
        </td></tr>
        <tr><td style="font-size:15px;line-height:1.6;color:#3f4854;padding-bottom:24px;">
          We received a request to reset your InfoJoy GTM password.
          This link expires in {minutes} minutes.
        </td></tr>
        <tr><td>
          <table role="presentation" cellpadding="0" cellspacing="0" border="0">
            <tr><td align="center" bgcolor="#1d6e5a" style="border-radius:6px;">
              <a href="{link}"
                 style="display:inline-block;padding:13px 28px;font-size:15px;font-weight:600;
                        color:#ffffff;text-decoration:none;border-radius:6px;
                        font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">
                Reset password
              </a>
            </td></tr>
          </table>
        </td></tr>
        <tr><td style="font-size:13px;line-height:1.6;color:#6b7280;padding-top:28px;">
          If you didn't request this, you can safely ignore this email &mdash; your password is
          unchanged.
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>"""
    return subject, text, html


async def send_reset_email(to: str, link: str) -> bool:
    """Email the password-reset button via the system SMTP mailbox. Module-level so tests can patch it."""
    from nexus.integrations import email_sender

    s = get_settings()
    cfg = {
        "provider": s.system_smtp_provider,
        "username": s.system_smtp_username,
        "password": s.system_smtp_password,
        "from_email": s.system_smtp_from or s.system_smtp_username,
        "from_name": s.system_smtp_from_name,
    }
    subject, body, html = build_reset_email(link)
    result = await email_sender.send_email(cfg, to=to, subject=subject, body=body, html=html)
    if not result.ok:
        logger.warning("password-reset email to %s not sent: %s", to, result.detail)
    return result.ok


def _reset_link(email: str, token: str) -> str:
    base = (get_settings().app_base_url or "").rstrip("/")
    qs = urlencode({"email": email, "token": token})
    return f"{base}/reset-password?{qs}"


async def request_password_reset(db: AsyncSession, *, email: str) -> None:
    """Create + email a reset token for a verified account. Generic + cooldown-limited; never
    reveals whether the email exists (no exception on unknown email)."""
    s = get_settings()
    email = _norm_email(email)
    user = (await db.scalars(select(User).where(User.email == email))).first()
    if user is None:
        logger.info("password-reset requested for unknown email — generic response")
        return  # silent: identical outcome to the success path (no enumeration)

    now = utcnow()
    existing = (
        await db.scalars(select(PasswordReset).where(PasswordReset.email == email))
    ).first()
    if existing is not None:
        if existing.last_sent_at and (now - existing.last_sent_at).total_seconds() < s.password_reset_cooldown_s:
            return  # within cooldown: stay silent (don't leak timing either)
        await db.delete(existing)
        await db.flush()

    token = secrets.token_urlsafe(32)
    db.add(
        PasswordReset(
            email=email,
            user_id=user.id,
            token_hash=hash_otp(token, otp_secret()),
            expires_at=now + timedelta(seconds=s.password_reset_ttl_s),
            used=False,
            last_sent_at=now,
        )
    )
    await db.flush()
    await send_reset_email(email, _reset_link(email, token))
    await db.commit()


async def reset_password(db: AsyncSession, *, email: str, token: str, new_password: str) -> None:
    """Verify the reset token and set the new password. Raises RegistrationError on any failure."""
    email = _norm_email(email)
    pr = (await db.scalars(select(PasswordReset).where(PasswordReset.email == email))).first()
    generic = RegistrationError(400, "Invalid or expired reset link. Request a new one.")
    if pr is None or pr.used:
        raise generic
    if utcnow() > pr.expires_at:
        await db.delete(pr)
        await db.commit()
        raise generic
    if not verify_otp(token, pr.token_hash, otp_secret()):
        raise generic

    user = await db.get(User, pr.user_id)
    if user is None:
        raise generic
    user.password_hash = hash_password(new_password)
    pr.used = True
    # A reset is normally somebody reacting to a compromise. Leaving the attacker's existing token
    # valid for the rest of its TTL makes the reset a formality.
    from nexus.api.deps import clear_session_version_cache
    from nexus.auth.sessions import revoke_user_sessions

    await revoke_user_sessions(db, user.id)
    await db.commit()
    clear_session_version_cache()
