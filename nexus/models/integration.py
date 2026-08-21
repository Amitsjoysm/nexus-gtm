# nexus/models/integration.py
"""Per-tenant integration credentials.

One row per tenant per **kind** of integration (``crm``, ``sep``, …). ``secret`` is a write-only
seam: it holds a Fernet envelope (:mod:`nexus.ingestion.crm_crypto`) and is never serialized into
a response model. Being ``TenantScoped`` means ``scripts/apply_rls.py`` picks the table up
automatically on deploy — it walks ``Base.metadata.sorted_tables`` — so no manual RLS policy work
is needed.

**Why one table with a ``kind`` discriminator rather than one table per integration.** This
started as ``crm_connections`` and a deliberate YAGNI call against a generic credential table. That
call was right at the time and wrong the moment SEP arrived: the two rows differ only in which
vendor the token belongs to, and every behaviour around them — write-only secret, status ladder,
verified_at/last_error, per-tenant resolution with an env fallback — is identical. A second table
would have duplicated that whole surface, and the third one would have duplicated it again.
``uq_integration_connection_tenant_kind`` is what keeps "one CRM per tenant" true while letting a
tenant hold a CRM and a SEP credential at once.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from nexus.core.db import Base, IdMixin, TimestampMixin, TZDateTime
from nexus.core.tenancy import TenantScoped

# The kinds of integration a tenant can hold credentials for.
CONNECTION_KINDS = ("crm", "sep")


class IntegrationConnection(IdMixin, TimestampMixin, TenantScoped, Base):
    """A tenant's own credentials for one integration. Overrides the deployment-wide env config."""

    __tablename__ = "integration_connections"
    __table_args__ = (
        UniqueConstraint("tenant_id", "kind", name="uq_integration_connection_tenant_kind"),
    )

    kind: Mapped[str] = mapped_column(String(16), default="crm", nullable=False)
    provider: Mapped[str] = mapped_column(String(16))    # hubspot | salesforce | salesloft | ...
    secret: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)  # {"enc": "..."}
    api_base: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    # unverified: saved, not yet tested. connected: last test passed. error: last test failed.
    status: Mapped[str] = mapped_column(String(16), default="unverified", nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    updated_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
