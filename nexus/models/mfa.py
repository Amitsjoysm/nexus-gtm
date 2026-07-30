# nexus/models/mfa.py
"""Multi-factor authentication: enrolled second factors and recovery codes.

**User-scoped, deliberately NOT tenant-scoped.** A user may hold memberships in several
workspaces, and their second factor is a property of the *identity*, not of any one workspace —
enrolling in Acme must not leave them unprotected in Globex. These tables therefore carry no
``tenant_id``. That is also what keeps them usable: ``scripts/apply_rls.py`` enrols every table
having a ``tenant_id`` into Row-Level Security, and MFA is read on the **login** path, before any
tenant is chosen and before any RLS binding exists. Under a policy those reads would return zero
rows rather than error — i.e. MFA would silently switch itself off, the exact failure mode
CLAUDE.md warns about. ``users``, ``pending_registrations`` and ``password_resets`` are
tenant-less for the same reason; if a tenant ever needs recording here it must be named
``subject_tenant_id``, per ``billing_audit_log``.

Nothing reversible is stored: the TOTP seed is Fernet-sealed (``nexus.core.crypto``) and recovery
codes are kept only as one-way HMAC digests.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from nexus.core.db import Base, IdMixin, TimestampMixin, TZDateTime

# The enrolment methods. ``totp`` is an authenticator app (RFC 6238, 30s step); ``email`` mails a
# code derived from the same primitive on a longer step, so both share one verification path.
MFA_METHODS = ("totp", "email")


class UserMFA(IdMixin, TimestampMixin, Base):
    """One enrolled second factor for one user. At most one row per (user, method)."""

    __tablename__ = "user_mfa"
    __table_args__ = (UniqueConstraint("user_id", "method", name="uq_user_mfa_method"),)

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    method: Mapped[str] = mapped_column(String(16))
    # Fernet ciphertext of the base32 seed — never the raw secret. A database leak must not hand
    # the attacker a working authenticator.
    secret: Mapped[str] = mapped_column(Text, default="")
    # NULL until the user has proved they can produce a code. An unconfirmed enrolment must never
    # gate login, or a half-finished setup locks the account holder out.
    confirmed_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    # The replay guard. A TOTP code is valid for a whole step (plus drift), so without recording
    # the highest counter already accepted, an observed code could be replayed inside its window.
    last_used_counter: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Lockout state. Persisted rather than kept in process memory: an in-memory counter resets on
    # every deploy and is per-worker, which is exactly when an online guessing attack succeeds.
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)


class MFARecoveryCode(IdMixin, TimestampMixin, Base):
    """A single-use break-glass code. Stored one-way; the plaintext is shown exactly once."""

    __tablename__ = "mfa_recovery_codes"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    # HMAC-SHA256 hex digest, keyed like the registration OTP. Recovery codes are high-entropy,
    # so a keyed hash (not a slow KDF) is the right trade: constant-time and cheap to verify.
    code_hash: Mapped[str] = mapped_column(String(128), index=True)
    # Set on use. Rows are marked, never deleted, so "this code was already spent" stays provable.
    used_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
