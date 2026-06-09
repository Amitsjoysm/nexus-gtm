"""Channel & Cadence: NEXUS-native multi-touch email cadences.

A :class:`Cadence` is a reusable, ordered list of :class:`CadenceStep` rows (each a delay +
an AI compose ``angle``). A :class:`CadenceEnrollment` puts one campaign target through a
cadence over time; the advance tick fires one :class:`CadenceTouch` per step. All tables are
tenant-scoped (RLS). Email-only in v1 (``channel`` is guarded to ``"email"`` in the service).
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from nexus.core.db import Base, IdMixin, TimestampMixin
from nexus.core.tenancy import TenantScoped

# Enrollment lifecycle.
ENROLL_ACTIVE = "active"
ENROLL_PAUSED = "paused"
ENROLL_COMPLETED = "completed"   # steps exhausted, natural finish
ENROLL_STOPPED = "stopped"       # halted early (see stop_reason)
ENROLL_TERMINAL = frozenset({ENROLL_COMPLETED, ENROLL_STOPPED})

# Why an enrollment stopped early.
STOP_REPLIED = "replied"
STOP_UNDELIVERABLE = "undeliverable"
STOP_MANUAL = "manual"
STOP_MAX_TOUCHES = "max_touches"   # duration cap exceeded

# Per-touch outcome.
TOUCH_SENT = "sent"
TOUCH_SKIPPED = "skipped"
TOUCH_FAILED = "failed"
TOUCH_AWAITING_APPROVAL = "awaiting_approval"


class Cadence(IdMixin, TimestampMixin, TenantScoped, Base):
    __tablename__ = "cadences"
    __table_args__ = (Index("ix_cadence_tenant", "tenant_id"),)

    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Soft-disable: off keeps the definition but blocks new enrollments.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )


class CadenceStep(IdMixin, TimestampMixin, TenantScoped, Base):
    __tablename__ = "cadence_steps"
    __table_args__ = (
        UniqueConstraint("cadence_id", "step_index", name="uq_cadence_step_index"),
    )

    cadence_id: Mapped[str] = mapped_column(ForeignKey("cadences.id"), index=True)
    step_index: Mapped[int] = mapped_column(Integer)          # 0-based, contiguous
    delay_days: Mapped[int] = mapped_column(Integer, default=0)  # wait before this step fires
    angle: Mapped[str] = mapped_column(Text, default="")     # per-touch compose angle
    channel: Mapped[str] = mapped_column(String(16), default="email")  # v1: email only


class CadenceEnrollment(IdMixin, TimestampMixin, TenantScoped, Base):
    __tablename__ = "cadence_enrollments"
    __table_args__ = (
        # The claim query's index: WHERE status=active AND next_touch_at <= now.
        Index("ix_enrollment_status_due", "status", "next_touch_at"),
        Index("ix_enrollment_campaign", "campaign_id"),
    )

    campaign_id: Mapped[str] = mapped_column(ForeignKey("campaigns.id"), index=True)
    campaign_target_id: Mapped[str | None] = mapped_column(
        ForeignKey("campaign_targets.id"), nullable=True
    )
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    contact_id: Mapped[str | None] = mapped_column(ForeignKey("contacts.id"), nullable=True)
    cadence_id: Mapped[str] = mapped_column(ForeignKey("cadences.id"), index=True)
    current_step_index: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default=ENROLL_ACTIVE, index=True)
    stop_reason: Mapped[str | None] = mapped_column(String(16), nullable=True)
    next_touch_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class CadenceTouch(IdMixin, TimestampMixin, TenantScoped, Base):
    __tablename__ = "cadence_touches"
    __table_args__ = (
        # Structural idempotency: a step is touched exactly once per enrollment.
        UniqueConstraint("enrollment_id", "step_index", name="uq_touch_enrollment_step"),
    )

    enrollment_id: Mapped[str] = mapped_column(
        ForeignKey("cadence_enrollments.id"), index=True
    )
    step_index: Mapped[int] = mapped_column(Integer)
    run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(20))
    skip_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    draft: Mapped[dict] = mapped_column(JSON, default=dict)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
