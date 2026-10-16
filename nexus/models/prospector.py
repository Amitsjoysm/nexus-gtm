"""Prospector's three tables: the shared vendor cache, the paid ledger, and the daily browse counter.

* ``ProspectorPage`` is **platform-global** (no ``tenant_id``), like ``web_cache``. A page is the
  InfoJoy Search API's answer to one query, the same fact for every workspace that asks it, so it is
  bought once and reused for 24 hours (decision D10). It holds the vendor's PII, so the payload is
  Fernet-sealed and the row expires; ``prune`` deletes it. Nothing tenant-specific is ever written
  here: the key is a hash of the query, and who asked is not recorded.
* ``ProspectorPurchase`` is **tenant-scoped** (RLS via ``scripts/apply_rls.py``): one row per
  (record, field) this workspace has paid for, so re-adding, re-exporting or retrying never charges
  twice (D6). Hashes only, never a value.
* ``ProspectorDailyRows`` is **tenant-scoped**: rows browsed per workspace per UTC day, the cost
  guard behind the daily budget (D11, D22). Its own table rather than the usage stream, so the guard
  holds whatever ``NEXUS_BILLING_ENFORCEMENT`` says (``off`` records nothing).
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from nexus.core.db import Base, IdMixin, TimestampMixin, TZDateTime
from nexus.core.tenancy import TenantScoped

#: The fields a purchase can be for (D4, D5).
PURCHASE_FIELDS = ("email", "phone", "company")


class ProspectorPage(TimestampMixin, Base):
    __tablename__ = "prospector_pages"
    __table_args__ = (Index("ix_prospector_pages_expires", "expires_at"),)

    #: sha256 of view + canonical filters + page + size: two workers racing on one query write
    #: the same row and one insert loses cleanly.
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    page_number: Mapped[int] = mapped_column(Integer)
    page_size: Mapped[int] = mapped_column(Integer)
    payload_sealed: Mapped[str] = mapped_column(Text)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    total_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(TZDateTime)
    expires_at: Mapped[datetime] = mapped_column(TZDateTime)


class ProspectorPurchase(IdMixin, TimestampMixin, TenantScoped, Base):
    __tablename__ = "prospector_purchases"
    __table_args__ = (
        UniqueConstraint("tenant_id", "fingerprint", "field", name="uq_prospector_purchase"),
    )

    #: sha256 of the record's identity (normalised email, else name + domain; a company's domain).
    fingerprint: Mapped[str] = mapped_column(String(64))
    field: Mapped[str] = mapped_column(String(16))
    contact_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    account_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    request_id: Mapped[str] = mapped_column(String(64), default="")
    first_charged_at: Mapped[datetime] = mapped_column(TZDateTime)
    last_delivered_at: Mapped[datetime] = mapped_column(TZDateTime)


class ProspectorDailyRows(IdMixin, TimestampMixin, TenantScoped, Base):
    __tablename__ = "prospector_daily_rows"
    __table_args__ = (UniqueConstraint("tenant_id", "day", name="uq_prospector_daily_rows"),)

    #: The UTC date, "YYYY-MM-DD".
    day: Mapped[str] = mapped_column(String(10))
    rows: Mapped[int] = mapped_column(Integer, default=0)
