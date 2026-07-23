"""Accounts (companies) and contacts (people)."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
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
    # Archived (SDR removed as not-relevant, or ICP re-screen). NULL = active. A dedicated,
    # indexable column so the accounts list can filter+paginate in SQL instead of fetching a
    # page then dropping archived rows in Python (which silently shrank the page). The legacy
    # ``custom_fields['archived']`` boolean is kept mirrored during the transition for rollback
    # safety; ``set_archived`` writes both. Index supports the "active, newest-first" list scan.
    archived_at: Mapped[datetime | None] = mapped_column(
        TZDateTime(), nullable=True, index=True
    )

    contacts: Mapped[list["Contact"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )

    @property
    def is_archived(self) -> bool:
        return self.archived_at is not None

    def set_archived(self, archived: bool, *, reason: str | None = None) -> None:
        """Single write-point for archived state. Sets the ``archived_at`` column (the source of
        truth going forward) AND mirrors the legacy ``custom_fields['archived']`` boolean so a
        rollback to code that still reads the JSON flag stays correct (dual-write)."""
        from nexus.core.db import utcnow

        self.archived_at = utcnow() if archived else None
        cf = dict(self.custom_fields or {})
        if archived:
            cf["archived"] = True
            if reason is not None:
                cf["archived_reason"] = reason
        else:
            cf["archived"] = False
            cf.pop("archived_reason", None)
        self.custom_fields = cf


class Contact(IdMixin, TimestampMixin, TenantScoped, Base):
    __tablename__ = "contacts"
    # Backs the workspace Contacts list: tenant-scoped, newest-first, SQL-paginated.
    __table_args__ = (Index("ix_contact_tenant_created", "tenant_id", "created_at"),)

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
    # When the verdict was last refreshed (any status, incl. unknown→unknown re-checks). Shown to
    # the SDR as "Checked <date>" and drives the re-verification cool-down for valid addresses.
    email_checked_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    phone_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    enrichment_source: Mapped[str | None] = mapped_column(String(60), nullable=True)
    # Proprietary per-tenant columns (see CustomFieldDef).
    custom_fields: Mapped[dict] = mapped_column(JSON, default=dict)

    account: Mapped[Account] = relationship(back_populates="contacts")
