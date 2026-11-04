"""What deleting accounts leaves behind (migration 0076).

``discovery_suppressions`` is the workspace's own "don't suggest these again": domains it deleted
and does not want Populate or the daily sweep to offer back. Tenant-scoped.

``company_reports`` is a workspace telling us a shared company row is wrong. Platform-global like
``companies``: the reporting workspace is ``subject_tenant_id`` (the name ``billing_audit_log`` uses)
so ``apply_rls.py``, which enrols any table with a ``tenant_id``, leaves it readable to the people
who review it. A report is a flag for review and nothing else: one workspace never quarantines a row
every workspace reads.
"""
from __future__ import annotations

from sqlalchemy import ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from nexus.core.db import Base, IdMixin, TimestampMixin
from nexus.core.tenancy import TenantScoped

SUPPRESSION_REASONS = ("deleted",)
REPORT_STATUSES = ("open", "resolved")


class DiscoverySuppression(IdMixin, TimestampMixin, TenantScoped, Base):
    __tablename__ = "discovery_suppressions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "domain", name="uq_discovery_suppression_domain"),
    )

    #: Normalised, as `accounts.domain` and `companies.domain` are stored.
    domain: Mapped[str] = mapped_column(String(255))
    reason: Mapped[str] = mapped_column(String(40), default="deleted")
    created_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)


class CompanyReport(IdMixin, TimestampMixin, Base):
    __tablename__ = "company_reports"
    __table_args__ = (
        UniqueConstraint("company_id", "subject_tenant_id", name="uq_company_report"),
        Index("ix_company_reports_status", "status"),
    )

    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"))
    subject_tenant_id: Mapped[str] = mapped_column(String(32))
    reported_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="open")
