"""Durable orchestration: runs, steps, append-only events, and human approval gates.

An :class:`OrchestrationRun` is a planned, resumable unit of work. The planner authors a
DAG of :class:`RunStep` rows; the engine drives them to terminal state, parking outbound
steps at an :class:`Approval` until a human decides. Every state transition appends a
:class:`RunEvent` (the durable record that powers SSE replay).

All tables are tenant-scoped — a run never reads or writes across tenant boundaries.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    Index,
)
from sqlalchemy.orm import Mapped, mapped_column

from nexus.core.db import Base, IdMixin, TimestampMixin
from nexus.core.tenancy import TenantScoped

# Run lifecycle.
RUN_PLANNING = "planning"
RUN_RUNNING = "running"
RUN_AWAITING_APPROVAL = "awaiting_approval"
RUN_COMPLETED = "completed"
RUN_FAILED = "failed"
RUN_CANCELLED = "cancelled"
RUN_TERMINAL = frozenset({RUN_COMPLETED, RUN_FAILED, RUN_CANCELLED})

# Step lifecycle.
STEP_PENDING = "pending"
STEP_RUNNING = "running"
STEP_AWAITING_APPROVAL = "awaiting_approval"
STEP_COMPLETED = "completed"
STEP_FAILED = "failed"
STEP_SKIPPED = "skipped"
STEP_REJECTED = "rejected"
STEP_TERMINAL = frozenset({STEP_COMPLETED, STEP_FAILED, STEP_SKIPPED, STEP_REJECTED})

# Approval lifecycle.
APPROVAL_PENDING = "pending"
APPROVAL_APPROVED = "approved"
APPROVAL_REJECTED = "rejected"


class OrchestrationRun(IdMixin, TimestampMixin, TenantScoped, Base):
    __tablename__ = "orchestration_runs"
    __table_args__ = (
        # One in-flight run per idempotency key per tenant: dedupes a goal that
        # would otherwise be planned twice (double submit, retry, race).
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_run_idempotency"),
        Index("ix_run_tenant_status", "tenant_id", "status"),
    )

    goal: Mapped[str] = mapped_column(String(60), index=True)
    goal_input: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(24), default=RUN_PLANNING, index=True)
    # The authored plan (list of step descriptors), kept for audit/debug.
    plan: Mapped[list] = mapped_column(JSON, default=list)
    # Shared run context the subagents read/write — the "blackboard" that makes
    # each agent aware of what the others have produced.
    blackboard: Mapped[dict] = mapped_column(JSON, default=dict)
    account_id: Mapped[str | None] = mapped_column(
        ForeignKey("accounts.id"), nullable=True, index=True
    )
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class RunStep(IdMixin, TimestampMixin, TenantScoped, Base):
    __tablename__ = "run_steps"
    __table_args__ = (
        # A step index is unique within its run — the engine's idempotency anchor
        # so a resumed/retried run never double-executes the same node.
        UniqueConstraint("run_id", "idx", name="uq_step_run_idx"),
        Index("ix_step_run_status", "run_id", "status"),
    )

    run_id: Mapped[str] = mapped_column(ForeignKey("orchestration_runs.id"), index=True)
    idx: Mapped[int] = mapped_column(Integer)
    tool: Mapped[str] = mapped_column(String(60), index=True)
    inputs: Mapped[dict] = mapped_column(JSON, default=dict)
    depends_on: Mapped[list] = mapped_column(JSON, default=list)  # list[int] of step idx
    status: Mapped[str] = mapped_column(String(24), default=STEP_PENDING, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    requires_approval: Mapped[bool] = mapped_column(Boolean, default=False)
    # Plain reference (not an FK) to avoid a circular run_steps<->approvals dependency
    # that would force ALTER-based table creation on SQLite.
    approval_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    output: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class RunEvent(IdMixin, TimestampMixin, TenantScoped, Base):
    """Append-only event log per run. ``seq`` is monotonic within a run so an SSE
    client can resume from its ``Last-Event-ID`` without gaps or duplicates."""

    __tablename__ = "run_events"
    __table_args__ = (
        UniqueConstraint("run_id", "seq", name="uq_event_run_seq"),
        Index("ix_event_run_seq", "run_id", "seq"),
    )

    run_id: Mapped[str] = mapped_column(ForeignKey("orchestration_runs.id"), index=True)
    seq: Mapped[int] = mapped_column(BigInteger)
    type: Mapped[str] = mapped_column(String(60))
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class Approval(IdMixin, TimestampMixin, TenantScoped, Base):
    """A human-in-the-loop gate. Outbound steps (send, schedule) park here with the
    exact payload that will go out, so a reviewer can approve, edit, or reject."""

    __tablename__ = "approvals"
    __table_args__ = (Index("ix_approval_tenant_status", "tenant_id", "status"),)

    run_id: Mapped[str] = mapped_column(ForeignKey("orchestration_runs.id"), index=True)
    step_id: Mapped[str] = mapped_column(ForeignKey("run_steps.id"), index=True)
    kind: Mapped[str] = mapped_column(String(60))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default=APPROVAL_PENDING, index=True)
    decided_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    edits: Mapped[dict] = mapped_column(JSON, default=dict)
