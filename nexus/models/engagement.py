"""SDR engagement engine: mailboxes, sequences, conversations, replies, do-not-contact (spec §4).

Every table is tenant-scoped, so ``scripts/apply_rls.py`` enrols it with no manual policy work.

Three shapes worth knowing before reading the columns:

* **An outbound message row exists BEFORE the provider call.** It is written ``queued`` and becomes
  ``sent`` with the provider's ids, so a send that times out can be reconciled against the Sent
  folder instead of retried blindly. Two unique indexes make a double send structurally impossible:
  ``(enrollment_id, step_index)`` for outbound step messages (partial, direction = 'out') and
  ``(tenant_id, idempotency_key)`` for everything else (re-engagements, responses).
* **Threads are the provider's threads.** ``engagement_threads`` maps one provider thread in one
  mailbox to the conversation; an enrollment's ``current_thread_id`` moves when the person replies
  from a different thread (D16). It is a plain column rather than a foreign key because threads
  also point at enrollments, and a two-way foreign key would make table creation order circular.
* **Do-not-contact is keyed by the normalised address, not the contact.** The same person can be a
  contact in several accounts or be re-imported; a block that lived on one contact row would miss
  the others (D7).

Column names follow the spec, except ``references`` → ``references_header``: REFERENCES is a
reserved word in Postgres.
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from nexus.core.db import Base, IdMixin, TimestampMixin, TZDateTime
from nexus.core.tenancy import TenantScoped

# ---- vocabularies ---------------------------------------------------------------------------
MAILBOX_PROVIDERS = ("google", "microsoft")
MAILBOX_STATUSES = ("connected", "needs_reauth", "revoked", "error")

CAMPAIGN_STATUSES = ("draft", "reviewing", "active", "paused", "completed")
FIRST_SEND_MODES = ("on_approval", "scheduled")
TIMEZONE_MODES = ("contact", "sdr")
STEP_CHANNELS = ("email", "call")
TIMING_MODES = ("auto", "manual")

ENROLLMENT_STATUSES = ("awaiting_review", "active", "paused", "snoozed", "stopped", "completed")
PAUSE_REASONS = (
    "colleague_replied", "out_of_office", "needs_decision", "mailbox_disconnected",
    "out_of_credits", "manual",
)
STOP_REASONS = ("replied", "declined", "unsubscribed", "bounced", "manual")

MESSAGE_DIRECTIONS = ("out", "in")
MESSAGE_STATUSES = ("draft", "approved", "queued", "sent", "failed", "bounced", "received")
MESSAGE_KINDS = ("step", "reengage", "response", "inbound")
INBOUND_KINDS = ("human", "auto_reply", "bounce", "other_auto")

REPLY_CATEGORIES = (
    "interested", "question", "referral", "later", "out_of_office", "declined", "unsubscribe",
    "other_auto", "unclear",
)
REPLY_DECISIONS = ("reengage", "block", "close", "meeting")
LABEL_SOURCES = ("ai", "deterministic", "sdr_confirmed", "sdr_corrected")

DNC_REASONS = ("unsubscribed", "declined", "bounced", "manual")


class MailboxConnection(IdMixin, TimestampMixin, TenantScoped, Base):
    """One SDR's Gmail or Microsoft 365 mailbox, connected by OAuth (D1, D2)."""

    __tablename__ = "mailbox_connections"
    __table_args__ = (UniqueConstraint("tenant_id", "email", name="uq_mailbox_connection_email"),)

    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    provider: Mapped[str] = mapped_column(String(16))
    email: Mapped[str] = mapped_column(String(320), index=True)
    display_name: Mapped[str] = mapped_column(String(200), default="")
    # {"enc": "<fernet>"} over {"access_token", "refresh_token", "expires_at", "token_type"}.
    tokens: Mapped[dict] = mapped_column(JSON, default=dict)
    scopes: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(20), default="connected", index=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Gmail historyId or the Graph deltaLink URL, which is long.
    sync_cursor: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    notifications_expire_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    # Graph subscription id; Gmail watches have no id.
    notification_subscription_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True, index=True
    )
    paused_until: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    signature: Mapped[str] = mapped_column(Text, default="")
    # The SDR's own confidence bar, accepted only inside the workspace range (D23). NULL = default.
    reply_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    # IANA zone of the SDR; captured from the browser at connect, editable.
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")


class SequenceTemplate(IdMixin, TimestampMixin, TenantScoped, Base):
    """A reusable list of step specs a campaign is built from (replaces Cadences)."""

    __tablename__ = "sequence_templates"

    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    # [{"channel", "angle", "timing_mode", "delay_business_days", "send_time_local",
    #   "allowed_weekdays"}], in step order.
    steps: Mapped[list] = mapped_column(JSON, default=list)
    created_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    legacy_cadence_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)


class EngagementCampaign(IdMixin, TimestampMixin, TenantScoped, Base):
    __tablename__ = "engagement_campaigns"
    __table_args__ = (Index("ix_engagement_campaign_tenant_status", "tenant_id", "status"),)

    name: Mapped[str] = mapped_column(String(200))
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    mailbox_connection_id: Mapped[str | None] = mapped_column(
        ForeignKey("mailbox_connections.id"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(20), default="draft")
    pause_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    review_every_touch: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    first_send_mode: Mapped[str] = mapped_column(String(16), default="on_approval")
    first_send_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    timezone_mode: Mapped[str] = mapped_column(String(10), default="contact")
    source_list_id: Mapped[str | None] = mapped_column(
        ForeignKey("prospect_lists.id"), nullable=True
    )
    template_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # {"worst_case": credits, "likely": credits, "by_capability": {...}, "computed_at": iso}
    credit_estimate: Mapped[dict] = mapped_column(JSON, default=dict)
    launched_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    legacy_campaign_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)


class EngagementStep(IdMixin, TimestampMixin, TenantScoped, Base):
    __tablename__ = "engagement_steps"
    __table_args__ = (
        UniqueConstraint("campaign_id", "step_index", name="uq_engagement_step_index"),
    )

    campaign_id: Mapped[str] = mapped_column(ForeignKey("engagement_campaigns.id"), index=True)
    step_index: Mapped[int] = mapped_column(Integer)
    channel: Mapped[str] = mapped_column(String(10), default="email")
    angle: Mapped[str] = mapped_column(Text, default="")
    timing_mode: Mapped[str] = mapped_column(String(10), default="auto")
    delay_business_days: Mapped[int] = mapped_column(Integer, default=0)
    # "HH:MM" in the recipient's (or SDR's, per timezone_mode) local time. Manual timing only.
    send_time_local: Mapped[str | None] = mapped_column(String(5), nullable=True)
    allowed_weekdays: Mapped[list] = mapped_column(JSON, default=lambda: [0, 1, 2, 3, 4])


class EngagementEnrollment(IdMixin, TimestampMixin, TenantScoped, Base):
    __tablename__ = "engagement_enrollments"
    __table_args__ = (
        UniqueConstraint("campaign_id", "contact_id", name="uq_engagement_enrollment_contact"),
        # The due-claim index: WHERE status = 'active' AND next_action_at <= now.
        Index("ix_engagement_enrollment_due", "status", "next_action_at"),
    )

    campaign_id: Mapped[str] = mapped_column(ForeignKey("engagement_campaigns.id"), index=True)
    contact_id: Mapped[str] = mapped_column(ForeignKey("contacts.id"), index=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    mailbox_connection_id: Mapped[str | None] = mapped_column(
        ForeignKey("mailbox_connections.id"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(20), default="awaiting_review")
    status_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    current_step_index: Mapped[int] = mapped_column(Integer, default=0)
    next_action_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    # True when a person moved the next step by hand; automatic rescheduling leaves it alone.
    next_action_override: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    snoozed_until: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    current_thread_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    # The recipient's resolved IANA zone at enrollment (timekeeping.resolve_zone).
    contact_timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    started_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    legacy_enrollment_id: Mapped[str | None] = mapped_column(
        String(32), nullable=True, index=True
    )


class EngagementThread(IdMixin, TimestampMixin, TenantScoped, Base):
    __tablename__ = "engagement_threads"
    __table_args__ = (
        UniqueConstraint(
            "mailbox_connection_id", "provider_thread_id", name="uq_engagement_thread_provider"
        ),
    )

    mailbox_connection_id: Mapped[str] = mapped_column(
        ForeignKey("mailbox_connections.id"), index=True
    )
    provider_thread_id: Mapped[str] = mapped_column(String(255))
    contact_id: Mapped[str | None] = mapped_column(
        ForeignKey("contacts.id"), nullable=True, index=True
    )
    account_id: Mapped[str | None] = mapped_column(
        ForeignKey("accounts.id"), nullable=True, index=True
    )
    enrollment_id: Mapped[str | None] = mapped_column(
        ForeignKey("engagement_enrollments.id"), nullable=True, index=True
    )
    base_subject: Mapped[str] = mapped_column(Text, default="")
    last_message_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)


class EngagementMessage(IdMixin, TimestampMixin, TenantScoped, Base):
    __tablename__ = "engagement_messages"
    __table_args__ = (
        UniqueConstraint(
            "mailbox_connection_id", "provider_message_id", name="uq_engagement_message_provider"
        ),
        # One outbound message per step per enrollment, whatever the retries do.
        Index(
            "uq_engagement_message_out_step", "enrollment_id", "step_index", unique=True,
            postgresql_where=text("direction = 'out'"), sqlite_where=text("direction = 'out'"),
        ),
        Index(
            "uq_engagement_message_idempotency", "tenant_id", "idempotency_key", unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
            sqlite_where=text("idempotency_key IS NOT NULL"),
        ),
        Index("ix_engagement_message_thread_time", "thread_id", "created_at"),
    )

    mailbox_connection_id: Mapped[str] = mapped_column(
        ForeignKey("mailbox_connections.id"), index=True
    )
    thread_id: Mapped[str | None] = mapped_column(
        ForeignKey("engagement_threads.id"), nullable=True
    )
    enrollment_id: Mapped[str | None] = mapped_column(
        ForeignKey("engagement_enrollments.id"), nullable=True, index=True
    )
    contact_id: Mapped[str | None] = mapped_column(
        ForeignKey("contacts.id"), nullable=True, index=True
    )
    direction: Mapped[str] = mapped_column(String(3))
    kind: Mapped[str] = mapped_column(String(10), default="step")
    status: Mapped[str] = mapped_column(String(10))
    step_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    provider_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    rfc_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    in_reply_to: Mapped[str | None] = mapped_column(String(255), nullable=True)
    references_header: Mapped[str] = mapped_column(Text, default="")
    # Our X-Nexus-Ref value (a ULID) — the reconciliation handle.
    ref_header: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    from_addr: Mapped[str] = mapped_column(String(320), default="")
    to_addrs: Mapped[list] = mapped_column(JSON, default=list)
    cc_addrs: Mapped[list] = mapped_column(JSON, default=list)
    subject: Mapped[str] = mapped_column(Text, default="")
    body_text: Mapped[str] = mapped_column(Text, default="")
    # The AI's text as first drafted, kept so an SDR's edits can be measured (draft.edited, §18).
    ai_subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    quality_problems: Mapped[list] = mapped_column(JSON, default=list)
    inbound_kind: Mapped[str | None] = mapped_column(String(12), nullable=True)
    approved_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    received_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class ReplyClassification(IdMixin, TimestampMixin, TenantScoped, Base):
    __tablename__ = "reply_classifications"
    __table_args__ = (
        UniqueConstraint("message_id", name="uq_reply_classification_message"),
        Index("ix_reply_classification_desk", "tenant_id", "status", "category"),
    )

    message_id: Mapped[str] = mapped_column(ForeignKey("engagement_messages.id"))
    mailbox_connection_id: Mapped[str] = mapped_column(
        ForeignKey("mailbox_connections.id"), index=True
    )
    enrollment_id: Mapped[str | None] = mapped_column(
        ForeignKey("engagement_enrollments.id"), nullable=True, index=True
    )
    contact_id: Mapped[str | None] = mapped_column(ForeignKey("contacts.id"), nullable=True)
    account_id: Mapped[str | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    category: Mapped[str] = mapped_column(String(20))
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    date_phrase: Mapped[str | None] = mapped_column(String(200), nullable=True)
    resolved_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    reasoning: Mapped[str] = mapped_column(Text, default="")
    label_source: Mapped[str] = mapped_column(String(16), default="ai")
    corrected_category: Mapped[str | None] = mapped_column(String(20), nullable=True)
    action_taken: Mapped[str] = mapped_column(String(40), default="")
    decision: Mapped[str | None] = mapped_column(String(16), nullable=True)
    decided_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    suggested_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    assigned_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    responded_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    reminded_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    status: Mapped[str] = mapped_column(String(8), default="open")


class DoNotContact(IdMixin, TimestampMixin, TenantScoped, Base):
    __tablename__ = "do_not_contact"
    __table_args__ = (
        # One ACTIVE block per address; a lifted row stays as history and a new block can follow.
        Index(
            "uq_do_not_contact_active", "tenant_id", "email", unique=True,
            postgresql_where=text("lifted_at IS NULL"), sqlite_where=text("lifted_at IS NULL"),
        ),
    )

    email: Mapped[str] = mapped_column(String(320), index=True)
    contact_id: Mapped[str | None] = mapped_column(ForeignKey("contacts.id"), nullable=True)
    reason: Mapped[str] = mapped_column(String(16))
    source_message_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    lifted_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    lifted_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    lift_note: Mapped[str] = mapped_column(Text, default="")
