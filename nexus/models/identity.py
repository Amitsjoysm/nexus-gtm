"""Identity & access: tenants, workspaces, users, memberships."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from nexus.core.db import Base, IdMixin, TimestampMixin, TZDateTime
from nexus.core.tenancy import TenantScoped


class Tenant(IdMixin, TimestampMixin, Base):
    """The isolation boundary — one customer organization."""

    __tablename__ = "tenants"

    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(80), unique=True)
    # Continuous Automation opt-in: when True (and the global automation_enabled master
    # switch is on), this tenant's accounts/cadences are driven autonomously by the heartbeat.
    automation_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Daily ICP Auto-Discovery (sub-project H): last time the discovery driver ran for this
    # tenant — the per-interval idempotency gate so the heartbeat doesn't re-run within the day.
    icp_discovery_last_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Per-workspace outbound email (SMTP) config so cadences send from the customer's own
    # Gmail/Outlook mailbox. Shape: {provider, host, port, username, from_email, from_name,
    # password, use_tls, enabled, verified_at}. The password is write-only — never serialized
    # back out of the API. Empty {} means "not configured" (sends stay gated/recorded only).
    email_settings: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class User(IdMixin, TimestampMixin, Base):
    """Global identity. A user may hold memberships in multiple tenants."""

    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(200))
    password_hash: Mapped[str] = mapped_column(String(255))


class PendingRegistration(IdMixin, TimestampMixin, Base):
    """A signup awaiting email-OTP verification (two-step registration).

    Holds the already-bcrypt-hashed password and a one-way HMAC hash of the OTP — never the
    plaintext code. On successful verification it is converted into a real Tenant + User +
    Workspace + owner Membership and deleted. Not tenant-scoped: no tenant exists yet. One row
    per email (re-requesting overwrites it), so a stale attempt can't block a fresh one."""

    __tablename__ = "pending_registrations"

    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(200))
    company_name: Mapped[str] = mapped_column(String(200))
    company_slug: Mapped[str] = mapped_column(String(80))
    password_hash: Mapped[str] = mapped_column(String(255))
    otp_hash: Mapped[str] = mapped_column(String(128))
    expires_at: Mapped[datetime] = mapped_column(TZDateTime())
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    resends: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_sent_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)


class PasswordReset(IdMixin, TimestampMixin, Base):
    """A pending 'forgot password' request: a single-use, time-boxed reset token (stored only as
    an HMAC hash) bound to a user. Verifying the token lets the holder set a new password. One row
    per email (a new request overwrites the old). Not tenant-scoped — reset is account-level."""

    __tablename__ = "password_resets"

    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(128))
    expires_at: Mapped[datetime] = mapped_column(TZDateTime())
    used: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_sent_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)


class Workspace(IdMixin, TimestampMixin, TenantScoped, Base):
    """A team within a tenant (e.g. 'Enterprise AE team')."""

    __tablename__ = "workspaces"

    name: Mapped[str] = mapped_column(String(200))


class Membership(IdMixin, TimestampMixin, TenantScoped, Base):
    """A user's role within a tenant."""

    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("tenant_id", "user_id", name="uq_membership_user"),)

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    workspace_id: Mapped[str | None] = mapped_column(ForeignKey("workspaces.id"), nullable=True)
    role: Mapped[str] = mapped_column(String(20), default="rep")

    user: Mapped[User] = relationship(lazy="joined")
