# nexus/models/integration.py
"""Per-tenant integration credentials.

One row per tenant per integration. ``secret`` is a write-only seam: it holds a Fernet envelope
(:mod:`nexus.ingestion.crm_crypto`) and is never serialized into a response model. Being
``TenantScoped`` means ``scripts/apply_rls.py`` picks the table up automatically on deploy — it
walks ``Base.metadata.sorted_tables`` — so no manual RLS policy work is needed.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from nexus.core.db import Base, IdMixin, TimestampMixin, TZDateTime
from nexus.core.tenancy import TenantScoped


class CrmConnection(IdMixin, TimestampMixin, TenantScoped, Base):
    """A tenant's own CRM credentials. Overrides the deployment-wide env configuration."""

    __tablename__ = "crm_connections"
    __table_args__ = (UniqueConstraint("tenant_id", name="uq_crm_connection_tenant"),)

    provider: Mapped[str] = mapped_column(String(16))          # hubspot | salesforce
    secret: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)  # {"enc": "..."}
    api_base: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    # unverified: saved, not yet tested. connected: last test passed. error: last test failed.
    status: Mapped[str] = mapped_column(String(16), default="unverified", nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    updated_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
