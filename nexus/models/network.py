"""Relationship Graph (A1–A5): a member's personal network, deduped per tenant.

Four tables form an additive subsystem alongside the account-intelligence loop:
  * NetworkSourceAccount — a member's connected provider account (google/microsoft/...).
  * NetworkPerson — a resolved, deduped person (the dedupe anchor).
  * NetworkIdentity — a raw per-source contact record, resolved to a NetworkPerson.
  * NetworkEdge — an owner(member)↔person relationship with MATERIALIZED connection strength.

Privacy: ``pooling_enabled`` defaults False on the source account and is mirrored onto edges so a
single indexed column gates cross-member visibility (owner == me OR pooling_enabled).
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from nexus.core.db import Base, IdMixin, TimestampMixin, TZDateTime
from nexus.core.tenancy import TenantScoped

PROVIDERS = ("google", "microsoft", "linkedin", "fixture")


class NetworkSourceAccount(IdMixin, TimestampMixin, TenantScoped, Base):
    """A member's connected provider account. OAuth is write-only — never serialized out."""

    __tablename__ = "network_source_accounts"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "member_id", "provider", "external_account_id",
            name="uq_network_source",
        ),
        Index("ix_network_source_member", "tenant_id", "member_id"),
    )

    member_id: Mapped[str] = mapped_column(ForeignKey("memberships.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    provider: Mapped[str] = mapped_column(String(16))
    external_account_id: Mapped[str] = mapped_column(String(255))
    display_email: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(16), default="connected")
    pooling_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    oauth: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)  # write-only seam
    sync_cursor: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)


class NetworkPerson(IdMixin, TimestampMixin, TenantScoped, Base):
    """A resolved, deduped person in the tenant graph. ``primary_email`` is the dedupe anchor."""

    __tablename__ = "network_persons"
    __table_args__ = (
        Index("ix_network_person_email", "tenant_id", "primary_email"),
        Index("ix_network_person_domain", "tenant_id", "company_domain"),
    )

    primary_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    full_name: Mapped[str] = mapped_column(String(200), default="")
    first_name: Mapped[str] = mapped_column(String(100), default="")
    last_name: Mapped[str] = mapped_column(String(100), default="")
    title: Mapped[str] = mapped_column(String(200), default="")
    company: Mapped[str] = mapped_column(String(200), default="")
    company_domain: Mapped[str] = mapped_column(String(200), default="")
    location: Mapped[str] = mapped_column(String(200), default="")
    country: Mapped[str] = mapped_column(String(100), default="")
    linkedin_url: Mapped[str | None] = mapped_column(String(300), nullable=True)
    twitter_handle: Mapped[str | None] = mapped_column(String(100), nullable=True)
    photo_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    profile: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    search_text: Mapped[str] = mapped_column(String(600), default="", index=True)
    identity_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    edge_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class NetworkIdentity(IdMixin, TimestampMixin, TenantScoped, Base):
    """A raw per-source contact record, resolved to a NetworkPerson. Upserted by (source, ext id)."""

    __tablename__ = "network_identities"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "source_account_id", "external_id", name="uq_network_identity"
        ),
        Index("ix_network_identity_key", "tenant_id", "resolution_key"),
        Index("ix_network_identity_person", "tenant_id", "person_id"),
    )

    source_account_id: Mapped[str] = mapped_column(
        ForeignKey("network_source_accounts.id"), index=True
    )
    person_id: Mapped[str | None] = mapped_column(ForeignKey("network_persons.id"), nullable=True)
    provider: Mapped[str] = mapped_column(String(16))
    external_id: Mapped[str] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    company: Mapped[str | None] = mapped_column(String(200), nullable=True)
    handle: Mapped[str | None] = mapped_column(String(100), nullable=True)
    raw: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    resolution_key: Mapped[str] = mapped_column(String(255), default="")


class NetworkEdge(IdMixin, TimestampMixin, TenantScoped, Base):
    """An owner(member)↔person relationship with materialized strength + touchpoint stats."""

    __tablename__ = "network_edges"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "owner_member_id", "person_id", "provider", name="uq_network_edge"
        ),
        Index(
            "ix_network_edge_person", "tenant_id", "person_id", "pooling_enabled", "strength"
        ),
        Index("ix_network_edge_owner", "tenant_id", "owner_member_id"),
    )

    owner_member_id: Mapped[str] = mapped_column(ForeignKey("memberships.id"), index=True)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    person_id: Mapped[str] = mapped_column(ForeignKey("network_persons.id"), index=True)
    source_account_id: Mapped[str] = mapped_column(ForeignKey("network_source_accounts.id"))
    provider: Mapped[str] = mapped_column(String(16))
    relation: Mapped[str] = mapped_column(String(16), default="contact")
    strength: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    email_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sent_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    received_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    meeting_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    first_touch_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    last_touch_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    mutual_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pooling_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
