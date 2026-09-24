# nexus/models/company.py
"""Shared company records — one row per real-world company, across every tenant.

Today an ``Account`` is per-tenant, so forty workspaces tracking Stripe means forty rows and forty
crawls of the same company. The funding round is the same fact for all of them.

**Platform-global on purpose: no ``tenant_id``.** ``scripts/apply_rls.py`` enrols any table that has
one, and enrolling these would return zero rows to the shared crawler — the failure mode CLAUDE.md
warns about, where RLS misses look like "no data" rather than an error. Per-tenant state stays on
``accounts``; nothing tenant-specific may ever be written here.

Design notes that matter later:

* ``id`` is ``sha1(normalised_domain)`` — a **hash key**, so a future hash-partition split is
  mechanical rather than a redesign. An auto-increment id would force a rewrite.
* ``domain`` is UNIQUE and is the identity key. A company with no domain cannot join this table
  safely, because name collisions across tenants would merge two unrelated businesses.
* ``parent_company_id`` is a plain self-FK. Real depth is 2–3, and a closure table is maintenance
  nobody will do.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from nexus.core.db import Base, IdMixin, TimestampMixin, TZDateTime

# Where a company record came from, for provenance when two sources disagree.
COMPANY_SOURCES = ("crawl", "account_backfill", "source_db", "import")


class Company(TimestampMixin, Base):
    __tablename__ = "companies"
    __table_args__ = (
        # "What is due for a shared refresh?" — the crawler's only scan.
        Index("ix_company_due", "last_crawled_at"),
        # The fan-out sweep's only scan: which companies have earned delivery.
        Index("ix_company_verdict", "crawl_verdict"),
        # The prospecting database step starts from the ICP's industry codes.
        Index("ix_company_linkedin_industry", "linkedin_industry_id"),
    )

    # sha1 of the normalised domain. Deterministic, so two concurrent resolvers racing on the same
    # company produce the same id and one loses the insert cleanly instead of creating a duplicate.
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    domain: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    industry: Mapped[str | None] = mapped_column(String(120), nullable=True)
    country: Mapped[str | None] = mapped_column(String(80), nullable=True)
    employee_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tech_stack: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    # Subsidiary -> parent. A funding round at the parent is a signal for the subsidiary, and that
    # link does not exist anywhere today.
    parent_company_id: Mapped[str | None] = mapped_column(
        ForeignKey("companies.id"), nullable=True, index=True
    )
    source: Mapped[str] = mapped_column(String(30), default="crawl")
    last_crawled_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    # Has the shared crawl been proven equivalent to the per-tenant crawl for this company?
    # unknown (keep crawling per-tenant) | agrees (fan-out may serve it) | disagrees (never).
    # Fan-out AND the per-tenant skip both read this, and they must read the same value: gating one
    # and not the other leaves an account with no crawl at all, which looks like a quiet market.
    crawl_verdict: Mapped[str] = mapped_column(String(20), default="unknown", index=True)
    verdict_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)

    # LinkedIn's view of the company, from the company-search and company-details actors
    # (migration 0061). Written only onto a row whose DOMAIN came from the company's own website:
    # the domain is the identity, and a LinkedIn page is attached to it, never the other way round.
    linkedin_url: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    linkedin_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    linkedin_industry_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # The size BAND LinkedIn filtered on. The headcount beside it can sit outside its own band
    # (measured: 286 employees in 51-200), so matching an ICP size reads the band.
    employee_range_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    employee_range_max: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Where the company is headquartered. Matching an ICP's countries reads `company_countries`
    # instead, because LinkedIn's location filter matches ANY office (measured: a UK search returned
    # US-headquartered companies with a London office), and the database step has to offer what the
    # actor would have.
    hq_country_code: Mapped[str | None] = mapped_column(String(2), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    linkedin_fetched_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    # LinkedIn's "similar pages": [{name, linkedin_url, industry}]. They carry no website, so
    # none becomes an account until a details lookup proves its domain.
    similar_linkedin: Mapped[list | None] = mapped_column(JSON, nullable=True)


class CompanyCountry(Base):
    """One country a company has an office in, from its LinkedIn locations. Platform-global like
    ``companies``: the database step joins it to answer "in the ICP's countries" on an index."""

    __tablename__ = "company_countries"

    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), primary_key=True)
    country_code: Mapped[str] = mapped_column(String(2), primary_key=True, index=True)


class CompanySignal(IdMixin, TimestampMixin, Base):
    """A signal about a company, fetched once and shared.

    Not a replacement for ``signal_events``: that stays per-tenant and keeps carrying scoring,
    inbox state and tenant-specific dedupe. This is the upstream fact; the per-tenant projection is
    derived from it.
    """

    __tablename__ = "company_signals"
    __table_args__ = (
        # The timeline for one company, newest first.
        Index("ix_company_signal_timeline", "company_id", "occurred_at"),
        # Idempotent shared crawl: the same event fetched twice updates rather than duplicates.
        Index("ix_company_signal_dedupe", "company_id", "dedupe_key", unique=True),
    )

    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), index=True)
    kind: Mapped[str] = mapped_column(String(40), index=True)
    source: Mapped[str] = mapped_column(String(60), default="")
    title: Mapped[str] = mapped_column(String(400), default="")
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    strength: Mapped[float] = mapped_column(Float, default=0.5)
    dedupe_key: Mapped[str] = mapped_column(String(200), default="")
    occurred_at: Mapped[datetime] = mapped_column(TZDateTime, index=True)
    # Carried to every tenant by fan-out, so a shared-crawl signal is labelled like a per-tenant one.
    dated: Mapped[str | None] = mapped_column(String(8), nullable=True)
