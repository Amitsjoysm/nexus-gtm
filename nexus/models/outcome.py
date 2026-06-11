"""Outcome-feedback loop — the Tier-3 learning substrate.

Every meaningful GTM result (a message sent, a reply, a booked meeting, a closed deal) is
captured as an immutable :class:`Outcome` row carrying a *snapshot* of the account's firmographics
at the moment it happened. The snapshot is deliberate: accounts get re-enriched and deleted, but a
won-deal's firmographics must stay frozen so the reweighting that reads them is reproducible.

Reading these rows, :mod:`nexus.outcomes.service` derives per-tenant *learned weights* that lean
the relevance score toward the firmographic axes a tenant's wins actually share — overriding the
static ``0.35/0.30/0.15/0.20`` defaults. The nightly reweight job is a later phase; the capture and
the deterministic read implemented here are the interface it will plug into.
"""
from __future__ import annotations

from sqlalchemy import JSON, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from nexus.core.db import Base, IdMixin, TimestampMixin
from nexus.core.tenancy import TenantScoped


class Outcome(IdMixin, TimestampMixin, TenantScoped, Base):
    __tablename__ = "outcomes"

    # What happened. One of nexus.outcomes.service.STAGES ("sent"|"replied"|"meeting"|"won"|"lost").
    stage: Mapped[str] = mapped_column(String(20), index=True)

    # Who it happened to. Nullable so an outcome can be recorded against an account with no
    # specific contact (or a contact whose account was later removed).
    account_id: Mapped[str | None] = mapped_column(
        ForeignKey("accounts.id"), index=True, nullable=True
    )
    contact_id: Mapped[str | None] = mapped_column(
        ForeignKey("contacts.id"), nullable=True
    )

    # Attribution: which campaign produced this outcome. Lets the campaign report roll up
    # replies/meetings/wins per campaign (the ROI view) instead of outcomes floating free.
    campaign_id: Mapped[str | None] = mapped_column(
        ForeignKey("campaigns.id"), index=True, nullable=True
    )

    # Frozen firmographic snapshot of the account at outcome time. Kept on the row (not joined back
    # to ``accounts``) so reweighting is reproducible even after the account is re-enriched/deleted.
    industry: Mapped[str | None] = mapped_column(String(120), nullable=True)
    employee_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    country: Mapped[str | None] = mapped_column(String(80), nullable=True)
    tech_count: Mapped[int] = mapped_column(Integer, default=0)

    # Free-form provenance (play name, sequence id, run id, reviewer note, …).
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
