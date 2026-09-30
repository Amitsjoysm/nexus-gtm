"""Which menu pages a superadmin has hidden from every workspace.

Per PAGE, not per module (decided with the product owner 2026-09-29): Approvals shares
`module.agents` with Orchestrator and AI Runs, so the module switch could not hide it alone. Hiding
is presentation plus the route. The feature behind the page keeps running; switching a feature off
is still the module switch's job.

Platform-global: no ``tenant_id``, so `scripts/apply_rls.py` leaves it alone, like
`feature_switches`. THE ABSENCE OF A ROW MEANS SHOWN, which is what makes the table additive.
"""
from __future__ import annotations

from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column

from nexus.core.db import Base, TimestampMixin


class PageVisibility(TimestampMixin, Base):
    __tablename__ = "page_visibility"

    # The page's key in `nexus.features.pages.HIDEABLE_PAGES`, not its path: a route can be renamed
    # without the stored decision losing its page.
    page_key: Mapped[str] = mapped_column(String(40), primary_key=True)
    hidden: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    updated_by: Mapped[str] = mapped_column(String(80), default="", nullable=False)
