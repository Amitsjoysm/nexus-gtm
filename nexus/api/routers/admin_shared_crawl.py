# nexus/api/routers/admin_shared_crawl.py
"""Approve, per company, that the shared crawl may deliver — the missing rung.

`nexus/companies/` was built as a four-stage rollout: backfill, shadow crawl, **diff**, fan-out.
Stages 1, 2 and 4 shipped. Stage 3 shipped as a library — `diff.record_verdict` — with no endpoint,
no scheduled job and no caller outside tests.

Both gates read the same column:

    fanout.fanout_company        -> deliver only when crawl_verdict == "agrees"
    pipeline._covered_by_shared_crawl -> skip the per-tenant crawl on the same condition

So with nothing writing that column, every company sat at `unknown` forever. Measured on the live
deployment 2026-09-08: 106 companies, all crawled that day, 715 shared signals gathered, **0
delivered**, and the per-tenant crawl still running in full. The shared layer built to reduce cost
was adding it.

**Verdicts are written by a person, and that is the design, not a shortcut.** From `nexus/companies`
own rules: "Do not enable fan-out on assertion. It multiplies any attribution mistake by the number
of subscribing tenants, and four of this subsystem's six attribution bugs were found only by running
against live providers." A job that promoted a company because two crawls happened to match is
promotion on assertion wearing a schedule. So this endpoint reports the evidence and records what an
operator concluded, exactly as `billing/reconcile.py` reports and never repairs.

Gated on `sources.manage`: authorising a data source to reach every tenant is the same act as
registering one, and only the `superadmin` preset holds it. Every verdict is audited with
before/after, like every other platform-admin mutation.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from nexus.api.deps import Principal, require_platform_permission
from nexus.billing.audit import record_admin_action
from nexus.billing.permissions import SOURCES_MANAGE
from nexus.companies import diff as company_diff
from nexus.core.db import get_platform_sessionmaker

logger = logging.getLogger("nexus.api.admin_shared_crawl")

router = APIRouter(prefix="/admin/shared-crawl", tags=["admin-shared-crawl"])


class CompanyComparison(BaseModel):
    company_id: str
    domain: str
    name: str
    verdict: str
    verdict_at: str | None = None
    last_crawled_at: str | None = None
    accounts: int = 0
    accounts_agreeing: int = 0
    accounts_disagreeing: int = 0
    #: Dedupe keys a TENANT holds that the shared crawl does not. `shared_only` is usually fine —
    #: the shared crawl ran more recently. This is the failure, because fan-out would present less
    #: than the tenant already has, which reads as data loss.
    missing_from_shared: list[str] = Field(default_factory=list)
    #: False when the company has never been crawled or has no linked accounts. An "agreement"
    #: between two empty sets is not evidence of anything, and approving on it is the false negative
    #: that would make this whole screen decorative.
    comparable: bool = False
    would_agree: bool = False


class SummaryOut(BaseModel):
    companies: dict[str, int] = Field(default_factory=dict)
    #: Accounts whose company is approved — the only ones fan-out serves and the only ones whose
    #: per-tenant crawl is skipped. While this is 0, both crawls run for every account.
    accounts_served_by_shared_crawl: int = 0
    #: Whether the deployment-level switch is on at all. Approving companies while this is off
    #: records the decision and changes nothing, which an operator should be told before they start.
    fanout_enabled: bool = True


class VerdictIn(BaseModel):
    model_config = {"extra": "forbid"}

    #: True records `agrees` and lets fan-out deliver this company; False records `disagrees`.
    #: A disagreement is RECORDED rather than merely withheld — "we compared and it was wrong" and
    #: "we never compared" call for different actions, and one absent verdict hides the first
    #: behind the second.
    agrees: bool
    note: str = Field(default="", max_length=500)


@router.get("/summary", response_model=SummaryOut)
async def summary(
    principal: Principal = Depends(require_platform_permission(SOURCES_MANAGE)),
) -> SummaryOut:
    from nexus.core.config import get_settings

    counts = await company_diff.verdict_counts()
    return SummaryOut(
        companies=counts["companies"],
        accounts_served_by_shared_crawl=counts["accounts_served_by_shared_crawl"],
        fanout_enabled=bool(get_settings().shared_company_crawl_enabled),
    )


@router.get("/companies", response_model=list[CompanyComparison])
async def companies(
    limit: int = Query(default=25, ge=1, le=200),
    only_unproven: bool = Query(
        default=True,
        description="Only companies still awaiting a verdict — the queue an operator works.",
    ),
    principal: Principal = Depends(require_platform_permission(SOURCES_MANAGE)),
) -> list[CompanyComparison]:
    """What the two crawls found for each company, so a verdict can be an informed one.

    Bounded and comparatively expensive — it diffs every linked account of every company returned —
    so it is a screen an operator opens, never something on a sweep.
    """
    rows = await company_diff.diff_companies(limit=limit, only_unproven=only_unproven)
    return [CompanyComparison(**row) for row in rows]


@router.post("/companies/{company_id}/verdict", response_model=CompanyComparison)
async def set_verdict(
    company_id: str,
    body: VerdictIn,
    principal: Principal = Depends(require_platform_permission(SOURCES_MANAGE)),
) -> CompanyComparison:
    """Record what the operator concluded. This is what lets fan-out deliver.

    Approving a company that has never been crawled is refused: there is nothing to have compared,
    and an approval granted on no evidence is exactly the assertion this gate exists to prevent.
    Recording a DISAGREEMENT is never refused — that is a finding, and withholding it would leave
    a known-bad company indistinguishable from one nobody has looked at.
    """
    from nexus.models.company import Company

    async with get_platform_sessionmaker()() as session:
        company = await session.get(Company, company_id)
        if company is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "company not found")
        before = {"crawl_verdict": company.crawl_verdict}

        if body.agrees and company.last_crawled_at is None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "this company has never been crawled, so there is nothing to have compared",
            )

        verdict = await company_diff.record_verdict(session, company_id, body.agrees)
        await record_admin_action(
            session,
            actor=principal.user_id,
            action="shared_crawl.verdict",
            target=f"company:{company_id}",
            before=before,
            after={"crawl_verdict": verdict},
            note=body.note,
        )
        await session.commit()

    rows = await company_diff.diff_companies(limit=200, only_unproven=False)
    for row in rows:
        if row["company_id"] == company_id:
            return CompanyComparison(**row)
    # The verdict landed; the re-read simply fell outside the bounded window.
    return CompanyComparison(
        company_id=company_id, domain="", name="", verdict=verdict, comparable=True
    )
