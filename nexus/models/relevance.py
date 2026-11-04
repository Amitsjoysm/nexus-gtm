"""The Relevance Engine's per-tenant context store.

One profile per tenant captures the GTM motion: who we sell to (ICP), why they buy
(value props), and what we sell (product context). Every agent output is conditioned on it.

**A company that sells several services gets an ICP per service** (owner decision 2026-10-29,
migration 0077). The profile is the DEFAULT service: a workspace that adds none behaves exactly as
before, and everything that was "the ICP" still means the profile. Each further service is an
``IcpService`` row with its own ICP, value props, product context and daily discovery number; an
account records the service that found it, a campaign pitches one, and every account is scored
against every service (``AccountServiceFit``).
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from nexus.core.db import Base, IdMixin, TimestampMixin, TZDateTime, utcnow
from nexus.core.tenancy import TenantScoped


class RelevanceProfile(IdMixin, TimestampMixin, TenantScoped, Base):
    __tablename__ = "relevance_profiles"

    # Exactly one profile per tenant.
    tenant_id: Mapped[str] = mapped_column(String(32), index=True, unique=True, nullable=False)

    # ICP rules, e.g. {"industries": [...], "employee_min": 200, "employee_max": 5000,
    #                  "countries": [...], "required_tech": [...], "weights": {...}}
    icp: Mapped[dict] = mapped_column(JSON, default=dict)

    # [{"name": ..., "description": ..., "pains_solved": [...]}]
    value_props: Mapped[list] = mapped_column(JSON, default=list)

    # Free-form description of the product, differentiators, proof points.
    product_context: Mapped[str] = mapped_column(Text, default="")

    # What the workspace calls the service this profile describes, once it sells more than one
    # (0077). NULL reads as "Main service" (`relevance.services.DEFAULT_NAME`).
    name: Mapped[str | None] = mapped_column(String(120), nullable=True)


class IcpService(IdMixin, TimestampMixin, TenantScoped, Base):
    """One more service the workspace sells, with a full ICP of its own (0077).

    Shaped like ``RelevanceProfile`` on purpose (``icp``, ``value_props``, ``product_context``):
    everything that scores, drafts or discovers against a profile takes a service as it is, so no
    second scoring or drafting path exists to drift. Archived, never deleted: accounts, campaigns
    and runs name it, and a campaign keeps pitching the service it was built for.
    """

    __tablename__ = "icp_services"

    name: Mapped[str] = mapped_column(String(120))
    #: The page the service was drafted from (`/services/abc`), so "Draft from page" can run again.
    source_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    icp: Mapped[dict] = mapped_column(JSON, default=dict)
    value_props: Mapped[list] = mapped_column(JSON, default=list)
    product_context: Mapped[str] = mapped_column(Text, default="")
    #: New companies the daily discovery adds for this service. 0 is off, the default: adding a
    #: service never starts buying companies by itself.
    daily_target: Mapped[int] = mapped_column(Integer, default=0)
    archived_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True, index=True)


class AccountServiceFit(IdMixin, TimestampMixin, TenantScoped, Base):
    """An account's ICP fit against one service: one row per (account, service), overwritten.

    Its own table rather than a service column on ``account_scores``: five readers take "the newest
    score row" as the account's score (the Fit column, lists, the call brief, insights, analytics),
    and a per-service row there would become the account's score for whichever was written last.
    Fit is deterministic, so a row is replaced rather than kept as history. The default service's
    fit stays where it always was, in ``account_scores``.
    """

    __tablename__ = "account_service_fits"
    __table_args__ = (
        UniqueConstraint("account_id", "service_id", name="uq_account_service_fit"),
        Index("ix_account_service_fit_tenant_service", "tenant_id", "service_id"),
    )

    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    service_id: Mapped[str] = mapped_column(ForeignKey("icp_services.id"))
    icp_fit: Mapped[int] = mapped_column(Integer, default=0)            # 0..100
    #: The must-match result (`relevance.gate.IcpGate.as_dict`), or NULL when nothing is mandatory.
    gate: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    reasons: Mapped[list] = mapped_column(JSON, default=list)
    computed_at: Mapped[datetime] = mapped_column(TZDateTime(), default=utcnow)
