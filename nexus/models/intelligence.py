"""AI-produced intelligence: account scores and agent run audit records."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from nexus.core.db import Base, IdMixin, TimestampMixin, utcnow
from nexus.core.tenancy import TenantScoped


class AccountScore(IdMixin, TimestampMixin, TenantScoped, Base):
    __tablename__ = "account_scores"

    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    icp_fit: Mapped[int] = mapped_column(Integer, default=0)      # 0..100
    intent: Mapped[int] = mapped_column(Integer, default=0)       # 0..100
    health: Mapped[int] = mapped_column(Integer, default=0)       # 0..100
    composite: Mapped[int] = mapped_column(Integer, default=0, index=True)
    rationale: Mapped[str] = mapped_column(Text, default="")
    model_version: Mapped[str] = mapped_column(String(40), default="v1")
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AgentRun(IdMixin, TimestampMixin, TenantScoped, Base):
    """Audit trail for every agent invocation (inputs, output, status, cost)."""

    __tablename__ = "agent_runs"

    agent: Mapped[str] = mapped_column(String(40), index=True)
    account_id: Mapped[str | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="completed")  # completed|failed
    input: Mapped[dict] = mapped_column(JSON, default=dict)
    output: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
