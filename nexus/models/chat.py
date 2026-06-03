# nexus/models/chat.py
"""Conversational orchestrator: chat sessions, append-only messages, custom-field registry.

A ChatSession is a token-frugal conversation that builds an ICP and launches discovery runs.
Messages are append-only with a monotonic ``seq`` per session (powers SSE replay, mirrors
RunEvent). CustomFieldDef is the per-tenant registry that gives proprietary data on
Account/Contact (stored as JSON ``custom_fields``) its column metadata and a CSV mapping target.

All tables are tenant-scoped — a conversation never reads or writes across tenant boundaries.
"""
from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    ForeignKey,
    Index,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from nexus.core.db import Base, IdMixin, TimestampMixin
from nexus.core.tenancy import TenantScoped

# Session lifecycle.
CHAT_ACTIVE = "active"
CHAT_ARCHIVED = "archived"

# Message roles / kinds.
ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"
ROLE_SYSTEM = "system"
KIND_TEXT = "text"
KIND_CLARIFYING = "clarifying_question"
KIND_RUN_LAUNCHED = "run_launched"
KIND_NOTICE = "notice"

# Custom-field entities / kinds.
ENTITY_ACCOUNT = "account"
ENTITY_CONTACT = "contact"
CF_KINDS = frozenset({"text", "number", "date", "bool", "url"})


class ChatSession(IdMixin, TimestampMixin, TenantScoped, Base):
    __tablename__ = "chat_sessions"
    __table_args__ = (
        Index("ix_chat_session_tenant_account", "tenant_id", "account_id"),
        Index("ix_chat_session_tenant_status", "tenant_id", "status"),
    )

    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    # The account/"client" the conversation centers on; null for pure ICP discovery.
    account_id: Mapped[str | None] = mapped_column(
        ForeignKey("accounts.id"), nullable=True, index=True
    )
    # Branch/continue a prior conversation (inherits its summary + icp_state).
    parent_session_id: Mapped[str | None] = mapped_column(
        ForeignKey("chat_sessions.id"), nullable=True
    )
    title: Mapped[str] = mapped_column(String(160), default="New conversation")
    status: Mapped[str] = mapped_column(String(16), default=CHAT_ACTIVE, index=True)
    target: Mapped[str | None] = mapped_column(String(16), nullable=True)  # companies | contacts
    icp_state: Mapped[dict] = mapped_column(JSON, default=dict)
    missing_slots: Mapped[list] = mapped_column(JSON, default=list)
    context_summary: Mapped[str] = mapped_column(Text, default="")


class ChatMessage(IdMixin, TimestampMixin, TenantScoped, Base):
    """Append-only. ``seq`` is monotonic within a session so an SSE client can resume from
    its ``Last-Event-ID`` without gaps or duplicates (mirrors RunEvent)."""

    __tablename__ = "chat_messages"
    __table_args__ = (
        UniqueConstraint("session_id", "seq", name="uq_chat_msg_seq"),
        Index("ix_chat_msg_session_seq", "session_id", "seq"),
    )

    session_id: Mapped[str] = mapped_column(ForeignKey("chat_sessions.id"), index=True)
    seq: Mapped[int] = mapped_column(BigInteger)
    role: Mapped[str] = mapped_column(String(12))
    kind: Mapped[str] = mapped_column(String(24), default=KIND_TEXT)
    content: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class CustomFieldDef(IdMixin, TimestampMixin, TenantScoped, Base):
    __tablename__ = "custom_field_defs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "entity", "key", name="uq_custom_field_key"),
    )

    entity: Mapped[str] = mapped_column(String(12))  # account | contact
    key: Mapped[str] = mapped_column(String(60))     # machine key
    label: Mapped[str] = mapped_column(String(120))  # display
    kind: Mapped[str] = mapped_column(String(12), default="text")
