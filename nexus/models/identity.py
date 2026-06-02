"""Identity & access: tenants, workspaces, users, memberships."""
from __future__ import annotations

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from nexus.core.db import Base, IdMixin, TimestampMixin
from nexus.core.tenancy import TenantScoped


class Tenant(IdMixin, TimestampMixin, Base):
    """The isolation boundary — one customer organization."""

    __tablename__ = "tenants"

    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(80), unique=True)


class User(IdMixin, TimestampMixin, Base):
    """Global identity. A user may hold memberships in multiple tenants."""

    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(200))
    password_hash: Mapped[str] = mapped_column(String(255))


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
