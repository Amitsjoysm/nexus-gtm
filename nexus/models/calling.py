"""Cold Calling: a call queue (CallTask) + logged outcomes (CallActivity).

The SDR's "power list": :class:`CallTask` rows are the queued calls (ranked like the Inbox);
each dial attempt is logged as a :class:`CallActivity`. One task -> many activities (retries).
All tables are tenant-scoped (RLS). v1 has no telephony — dialing is click-to-dial and outcomes
are logged manually — but the activity carries nullable recording/transcript/summary fields so
a real telephony provider (tier 2) can attach them with no schema change.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
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

# Queue task lifecycle.
CALL_OPEN = "open"
CALL_DONE = "done"
CALL_SKIPPED = "skipped"

# How a task entered the queue.
SOURCE_MANUAL = "manual"
SOURCE_CADENCE = "cadence"
SOURCE_PLAY = "play"

# Call outcomes (dispositions).
DISP_CONNECTED = "connected"
DISP_VOICEMAIL = "voicemail"
DISP_NO_ANSWER = "no_answer"
DISP_CALLBACK = "callback"
DISP_MEETING_BOOKED = "meeting_booked"
DISP_NOT_INTERESTED = "not_interested"
DISP_BAD_NUMBER = "bad_number"
DISP_GATEKEEPER = "gatekeeper"

def _dispositions():
    from nexus.calling.dispositions import BY_CODE, LEGACY

    every = {**LEGACY, **BY_CODE}
    return (frozenset(every),
            frozenset(code for code, d in every.items() if d.requeue))


# The team's list (`nexus/calling/dispositions.py`, 2026-10-26) plus the eight that existed before,
# still valid for the rows holding them. Re-queue dispositions keep the rep working the contact:
# the task stays open (due when the rep said) and a cadence is NOT advanced. Everything else is
# terminal: the task closes and any linked cadence advances.
ALL_DISPOSITIONS, REQUEUE_DISPOSITIONS = _dispositions()


class CallTask(IdMixin, TimestampMixin, TenantScoped, Base):
    __tablename__ = "call_tasks"
    __table_args__ = (
        Index("ix_call_task_status", "tenant_id", "status"),
        Index("ix_call_task_contact", "contact_id"),
    )

    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    contact_id: Mapped[str | None] = mapped_column(ForeignKey("contacts.id"), nullable=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    priority: Mapped[int] = mapped_column(Integer, default=0)          # 0-100, ranked like Inbox
    status: Mapped[str] = mapped_column(String(16), default=CALL_OPEN)
    source: Mapped[str] = mapped_column(String(16), default=SOURCE_MANUAL)
    owner_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Cadence linkage (NULL for standalone/manual calls).
    cadence_enrollment_id: Mapped[str | None] = mapped_column(
        ForeignKey("cadence_enrollments.id"), nullable=True
    )
    cadence_step_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Engagement engine linkage (spec §13). The cadence columns above stay as history.
    engagement_enrollment_id: Mapped[str | None] = mapped_column(
        ForeignKey("engagement_enrollments.id"), nullable=True
    )
    # Last generated AI script, cached so re-opening the call panel doesn't re-hit the LLM.
    script_cache: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class CallActivity(IdMixin, TimestampMixin, TenantScoped, Base):
    __tablename__ = "call_activities"
    __table_args__ = (
        Index("ix_call_activity_contact", "contact_id"),
        Index("ix_call_activity_account", "account_id"),
    )

    call_task_id: Mapped[str | None] = mapped_column(
        ForeignKey("call_tasks.id"), nullable=True
    )
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    contact_id: Mapped[str | None] = mapped_column(ForeignKey("contacts.id"), nullable=True)
    disposition: Mapped[str] = mapped_column(String(24))
    notes: Mapped[str] = mapped_column(Text, default="")
    duration_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    next_step: Mapped[str | None] = mapped_column(Text, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Tier-2-ready (telephony fills these; NULL in v1 manual logging).
    recording_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    transcript: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    sentiment: Mapped[str | None] = mapped_column(String(16), nullable=True)
    provider_call_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class PlacedCall(IdMixin, TimestampMixin, TenantScoped, Base):
    """One live call placed on the PLATFORM Twilio, so its minutes are charged even if nobody logs it.

    A platform call was charged only at disposition, so a call nobody logged was free. Written at
    dial; `charged_at` is stamped by whichever charges it first — the disposition or the sweep two
    hours on (decided with the product owner 2026-09-23). Both charge under the key
    `call:<provider_call_id>`, so they can never both charge. Calls on a workspace's own Twilio
    are not recorded: that workspace pays Twilio directly.
    """

    __tablename__ = "placed_calls"
    __table_args__ = (
        UniqueConstraint("tenant_id", "provider_call_id", name="uq_placed_call"),
        Index("ix_placed_calls_uncharged", "charged_at", "placed_at"),
    )

    call_task_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    provider_call_id: Mapped[str] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(16), default="platform")
    user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    placed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    charged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Started minutes charged. NULL with charged_at set: given up, nothing could be measured.
    minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)


class UserCallScript(IdMixin, TimestampMixin, TenantScoped, Base):
    """An SDR's own call script, uploaded from the call console (2026-11-04, migration 0078).

    Asked by the SDR team: "upload a script which is visible to the SDR during calls". One per
    person per workspace, stored exactly as written (`calling/scripts.read_document`), shown in
    the console for every call that person makes, beside the campaign's script when the call's
    campaign has one. Its `{{placeholders}}` are filled per call, as a campaign script's are.
    """

    __tablename__ = "user_call_scripts"
    __table_args__ = (UniqueConstraint("tenant_id", "user_id", name="uq_user_call_script"),)

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    script: Mapped[str] = mapped_column(Text)
    #: `filename`, `uploaded_at`.
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
