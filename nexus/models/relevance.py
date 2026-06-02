"""The Relevance Engine's per-tenant context store.

One profile per tenant captures the GTM motion: who we sell to (ICP), why they buy
(value props), and what we sell (product context). Every agent output is conditioned on it.
"""
from __future__ import annotations

from sqlalchemy import JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from nexus.core.db import Base, IdMixin, TimestampMixin
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
