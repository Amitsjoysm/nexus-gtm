"""Rep workflow & automation: inbox tasks, prospect lists, plays."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from nexus.core.db import Base, IdMixin, TimestampMixin
from nexus.core.tenancy import TenantScoped


class InboxTask(IdMixin, TimestampMixin, TenantScoped, Base):
    __tablename__ = "inbox_tasks"

    owner_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), index=True, nullable=True)
    account_id: Mapped[str | None] = mapped_column(ForeignKey("accounts.id"), index=True, nullable=True)
    contact_id: Mapped[str | None] = mapped_column(ForeignKey("contacts.id"), nullable=True)
    signal_id: Mapped[str | None] = mapped_column(ForeignKey("signal_events.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(300))
    reason: Mapped[str] = mapped_column(Text, default="")
    priority: Mapped[int] = mapped_column(Integer, default=0, index=True)  # 0..100
    status: Mapped[str] = mapped_column(String(20), default="open")  # open|snoozed|done
    suggested_action: Mapped[dict] = mapped_column(JSON, default=dict)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ProspectList(IdMixin, TimestampMixin, TenantScoped, Base):
    __tablename__ = "prospect_lists"

    name: Mapped[str] = mapped_column(String(200))
    owner_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    filter: Mapped[dict] = mapped_column(JSON, default=dict)


class ListItem(IdMixin, TimestampMixin, TenantScoped, Base):
    __tablename__ = "list_items"

    list_id: Mapped[str] = mapped_column(ForeignKey("prospect_lists.id"), index=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    contact_id: Mapped[str | None] = mapped_column(ForeignKey("contacts.id"), nullable=True)


class Play(IdMixin, TimestampMixin, TenantScoped, Base):
    """Signal → action automation. trigger and actions are declarative JSON."""

    __tablename__ = "plays"

    name: Mapped[str] = mapped_column(String(200))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # trigger: {"signal_kinds": [...], "min_composite": 60, "min_strength": 0.5}
    trigger: Mapped[dict] = mapped_column(JSON, default=dict)
    # actions: [{"type": "create_task"|"alert"|"sep_push", ...}]
    actions: Mapped[list] = mapped_column(JSON, default=list)


class PlayRun(IdMixin, TimestampMixin, TenantScoped, Base):
    __tablename__ = "play_runs"

    play_id: Mapped[str] = mapped_column(ForeignKey("plays.id"), index=True)
    account_id: Mapped[str | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    signal_id: Mapped[str | None] = mapped_column(ForeignKey("signal_events.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="executed")
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
