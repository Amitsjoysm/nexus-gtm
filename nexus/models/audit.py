# nexus/models/audit.py
"""Workspace audit trail: privileged, security-relevant actions a workspace admin can review.

**Tenant-scoped on purpose**, which is the opposite choice from ``billing_audit_log``. That table
is platform-global and deliberately names its column ``subject_tenant_id`` so ``apply_rls.py`` does
*not* enrol it — its reader is the operator who must see across tenants. This table's reader is the
workspace admin who made the change, so it carries ``tenant_id`` and RLS enrolment is exactly what
we want: one workspace must never read another's audit trail.

``meta`` never holds a secret. Callers pass ``token_set=True``, not the token — see
``nexus/core/audit.py``.
"""
from __future__ import annotations

from sqlalchemy import JSON, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from nexus.core.db import Base, IdMixin, TimestampMixin
from nexus.core.tenancy import TenantScoped


class AuditLog(IdMixin, TimestampMixin, TenantScoped, Base):
    """One privileged action: who did what, to which object, when."""

    __tablename__ = "audit_log"
    __table_args__ = (
        # The only read pattern is "this tenant's trail, newest first", optionally narrowed by
        # action — so the composite index matches the query rather than the column list.
        Index("ix_audit_log_tenant_created", "tenant_id", "created_at"),
    )

    action: Mapped[str] = mapped_column(String(64), index=True)
    # Nullable: some audited actions are taken by the system (a sweep, a webhook), not a user.
    actor_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    target_type: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    target_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    meta: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
