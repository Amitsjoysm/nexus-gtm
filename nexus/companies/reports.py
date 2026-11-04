# nexus/companies/reports.py
"""A workspace saying a shared company's data is wrong (`company_reports`, migration 0076).

Offered when accounts are deleted ("Report wrong company data"). A report is a flag for review and
never more: the `companies` row is read by every workspace, so one workspace's opinion must not
quarantine or rewrite it (the quarantine scripts are an operator's act, on evidence). One row per
company per workspace, so a second workspace reporting the same company adds its own row rather
than overwriting the first, and a workspace reporting again updates its reason.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("nexus.companies.reports")

MAX_REASON = 500


async def report_wrong(company_ids, *, tenant_id: str, user_id: str | None, reason: str) -> int:
    """Flag ``company_ids`` for review on behalf of ``tenant_id``. Returns rows written. Never
    raises: a failed report must not fail the delete it rode along with."""
    from sqlalchemy import select

    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.company import Company
    from nexus.models.records import CompanyReport

    ids = sorted({i for i in company_ids if i})
    text = " ".join((reason or "").split())[:MAX_REASON]
    if not ids:
        return 0
    written = 0
    try:
        # Platform-global table, so the owner role: under the RLS-bound role nothing here is
        # tenant-scoped and the row would be written by a session that cannot read it back.
        async with get_platform_sessionmaker()() as s:
            known = set((await s.scalars(select(Company.id).where(Company.id.in_(ids)))).all())
            have = {r.company_id: r for r in (await s.scalars(select(CompanyReport).where(
                CompanyReport.company_id.in_(ids),
                CompanyReport.subject_tenant_id == tenant_id))).all()}
            for company_id in ids:
                if company_id not in known:
                    continue
                row = have.get(company_id)
                if row is None:
                    s.add(CompanyReport(company_id=company_id, subject_tenant_id=tenant_id,
                                        reported_by_user_id=user_id, reason=text, status="open"))
                else:
                    row.reason, row.status, row.reported_by_user_id = text, "open", user_id
                written += 1
            await s.commit()
    except Exception:
        logger.warning("reporting wrong company data failed", exc_info=True)
        return 0
    return written
