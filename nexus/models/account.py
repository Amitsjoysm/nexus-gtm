"""Accounts (companies) and contacts (people)."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from nexus.core.db import Base, IdMixin, TimestampMixin, TZDateTime
from nexus.core.tenancy import TenantScoped


class Account(IdMixin, TimestampMixin, TenantScoped, Base):
    __tablename__ = "accounts"
    __table_args__ = (UniqueConstraint("tenant_id", "domain", name="uq_account_domain"),)

    name: Mapped[str] = mapped_column(String(255), index=True)
    domain: Mapped[str | None] = mapped_column(String(255), index=True, nullable=True)
    industry: Mapped[str | None] = mapped_column(String(120), nullable=True)
    employee_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    country: Mapped[str | None] = mapped_column(String(80), nullable=True)
    tech_stack: Mapped[list] = mapped_column(JSON, default=list)
    crm_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    crm_source: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # Proprietary per-tenant columns (CustomFieldDef metadata gives them labels/kinds).
    custom_fields: Mapped[dict] = mapped_column(JSON, default=dict)
    # Provenance: "discovery" for web-sourced rows so results can filter own vs discovered.
    source: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # Continuous Automation: when the autonomous heartbeat last re-processed this account.
    # NULL = never refreshed (always due). Stamped when the refresh driver claims the account.
    last_refreshed_at: Mapped[datetime | None] = mapped_column(
        TZDateTime(), nullable=True, index=True
    )
    # CRM Auto-Sync: when this account's state was last pushed to the CRM. NULL = never synced
    # (always due). Stamped on a successful push; the account is due again only when updated_at
    # moves past it. Indexed for the NULLS-FIRST due-selection scan.
    crm_synced_at: Mapped[datetime | None] = mapped_column(
        TZDateTime(), nullable=True, index=True
    )

    contacts: Mapped[list["Contact"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )


class Contact(IdMixin, TimestampMixin, TenantScoped, Base):
    __tablename__ = "contacts"

    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    full_name: Mapped[str] = mapped_column(String(200))
    title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    seniority: Mapped[str | None] = mapped_column(String(40), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(40), nullable=True)
    linkedin_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    email_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    # Last email-verification verdict ("valid"|"unknown"|"invalid"), stamped whenever the
    # orchestrator verifies this contact. ``None`` = never verified. Drives inbox triage.
    email_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    phone_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    enrichment_source: Mapped[str | None] = mapped_column(String(60), nullable=True)
    # Proprietary per-tenant columns (see CustomFieldDef).
    custom_fields: Mapped[dict] = mapped_column(JSON, default=dict)

    account: Mapped[Account] = relationship(back_populates="contacts")
