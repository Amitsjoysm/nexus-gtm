"""Segment Campaign Engine: a Campaign aggregates per-account outreach over a saved List.

A :class:`Campaign` targets a ProspectList; the service creates one :class:`CampaignTarget`
per account, drives an autonomous draft phase (research→score→compose, no send), parks the
whole campaign at a single human approval, then runs a send phase per approved target. All
tables are tenant-scoped — a campaign never reads or writes across tenant boundaries.
"""
from __future__ import annotations

from sqlalchemy import Boolean, ForeignKey, Index, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from nexus.core.db import Base, IdMixin, TimestampMixin
from nexus.core.tenancy import TenantScoped

# Campaign lifecycle.
CAMP_DRAFT_PENDING = "draft_pending"        # created, targets not yet enumerated
CAMP_DRAFTING = "drafting"                  # draft phase running
CAMP_AWAITING_APPROVAL = "awaiting_approval"  # all targets resolved; waiting on human
CAMP_APPROVED = "approved"                  # human approved; send phase queued
CAMP_SENDING = "sending"                    # send phase running
CAMP_COMPLETED = "completed"                # all targets terminal
CAMP_CANCELLED = "cancelled"
CAMP_FAILED = "failed"
CAMP_TERMINAL = frozenset({CAMP_COMPLETED, CAMP_CANCELLED, CAMP_FAILED})

# CampaignTarget lifecycle.
TARGET_PENDING = "pending"
TARGET_DRAFTING = "drafting"
TARGET_DRAFTED = "drafted"
TARGET_SKIPPED = "skipped"        # un-actionable (see skip_reason); terminal
TARGET_APPROVED = "approved"
TARGET_SENT = "sent"              # terminal
TARGET_FAILED = "failed"          # unexpected error (see error); terminal
TARGET_TERMINAL = frozenset({TARGET_SKIPPED, TARGET_SENT, TARGET_FAILED})

# Skip reasons — the fixed contract sub-project B (Contact Sourcing) consumes.
SKIP_NO_CONTACT = "no_deliverable_contact"
SKIP_UNGROUNDED = "ungrounded_draft"
SKIP_UNDELIVERABLE = "undeliverable_address"
SKIP_RESEARCH_FAILED = "research_failed"
SKIP_UNVERIFIED = "unverified_contact"   # sourced address below the send-confidence bar
SKIP_RISKY = "risky_address"             # risky verdict, campaign did not opt into send_risky


class Campaign(IdMixin, TimestampMixin, TenantScoped, Base):
    __tablename__ = "campaigns"
    __table_args__ = (Index("ix_campaign_tenant_status", "tenant_id", "status"),)

    name: Mapped[str] = mapped_column(String(200))
    list_id: Mapped[str] = mapped_column(ForeignKey("prospect_lists.id"), index=True)
    icp: Mapped[dict] = mapped_column(JSON, default=dict)
    sequence: Mapped[str] = mapped_column(String(120), default="ai-orchestrated-outbound")
    status: Mapped[str] = mapped_column(String(24), default=CAMP_DRAFT_PENDING, index=True)
    # Rolled-up counts: {"total","drafted","skipped","sent","failed","skips":{reason:count}}.
    report: Mapped[dict] = mapped_column(JSON, default=dict)
    # Per-campaign opt-in: send to addresses graded "risky" by the verifier (held by default).
    send_risky: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Channel & Cadence (sub-project C). NULL cadence_id = the existing single-touch send
    # path (fully backward-compatible). review_each_touch opts into a per-touch manual gate.
    cadence_id: Mapped[str | None] = mapped_column(
        ForeignKey("cadences.id"), nullable=True
    )
    review_each_touch: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    created_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )


class CampaignTarget(IdMixin, TimestampMixin, TenantScoped, Base):
    __tablename__ = "campaign_targets"
    __table_args__ = (Index("ix_camptarget_campaign_status", "campaign_id", "status"),)

    campaign_id: Mapped[str] = mapped_column(ForeignKey("campaigns.id"), index=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    # The research_compose run that produced this target's draft (plain ref, nullable).
    run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(24), default=TARGET_PENDING, index=True)
    skip_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # Snapshot copied off the run blackboard so approval UI + report survive the run.
    draft: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
