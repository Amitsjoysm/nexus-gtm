"""Accounts (companies) and contacts (people)."""
from __future__ import annotations

from sqlalchemy import JSON, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from nexus.core.db import Base, IdMixin, TimestampMixin
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
    phone_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    enrichment_source: Mapped[str | None] = mapped_column(String(60), nullable=True)

    account: Mapped[Account] = relationship(back_populates="contacts")
