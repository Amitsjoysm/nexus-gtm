""""How many companies now?": fill a workspace with N ICP companies, charged per company delivered.

Asked when an ICP is saved (product owner, 2026-09-24). The request is checked against the credit
balance for the FULL count before anything is bought (``preflight``), runs as a job because the
LinkedIn step can take minutes, and charges ``discovery.account_added`` once, for the companies
actually delivered, in the same transaction that creates their accounts: both land or neither does.

One populate at a time per workspace. A second while one runs would buy the same pages twice and
could double the spend the person agreed to.
"""
from __future__ import annotations

import logging
from collections import Counter

from nexus.core.db import utcnow

logger = logging.getLogger("nexus.prospecting.populate")

CAPABILITY = "discovery.account_added"
MAX_COUNT = 500
ACTIVE = ("queued", "running")


class PopulateRefused(Exception):
    """A request that cannot start. ``status`` is the HTTP status it maps to."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


async def quote(ts, count: int) -> dict:
    """What ``count`` companies would cost and whether the balance covers it. Charges nothing."""
    from nexus.billing.credits import balance
    from nexus.models.billing import BillingRateCard

    count = max(0, int(count))
    card = await ts.session.get(BillingRateCard, CAPABILITY)
    per = float(card.credits_per_unit or 0) if card is not None and card.active else 0.0
    try:
        held = float(await balance(ts))
    except Exception:
        held = None
    total = per * count
    return {"count": count, "credits_per_company": per, "total_credits": total,
            "balance": held, "enough": held is None or per == 0 or held >= total}


async def start(ts, *, count: int, user_id: str | None):
    """Validate, check the balance for the whole request, and record a queued run."""
    from nexus.billing.entitlements import preflight
    from nexus.models.prospecting import ProspectRun
    from nexus.relevance.engine import get_profile

    if count < 1 or count > MAX_COUNT:
        raise PopulateRefused(422, f"Choose between 1 and {MAX_COUNT} companies.")
    profile = await get_profile(ts)
    if profile is None or not profile.icp:
        raise PopulateRefused(400, "Save an ICP first: it is what the companies are matched on.")
    running = await ts.first(ProspectRun, ProspectRun.kind == "populate",
                             ProspectRun.status.in_(ACTIVE))
    if running is not None:
        raise PopulateRefused(409, "Companies are already being added for this workspace.")
    # For the whole count: whatever is then delivered was affordable when asked (402 otherwise).
    (await preflight(ts, CAPABILITY, quantity=count)).raise_if_blocked()
    run = ProspectRun(tenant_id=ts.tenant_id, kind="populate", user_id=user_id,
                      requested=count, delivered=0, status="queued", sources={},
                      discarded={}, notes={}, account_ids=[])
    ts.add(run)
    await ts.flush()
    return run


def run_out(run) -> dict:
    return {
        "id": run.id, "status": run.status, "requested": run.requested,
        "delivered": run.delivered, "sources": run.sources or {}, "discarded": run.discarded or {},
        "notes": run.notes or {},
        "account_ids": run.account_ids or [], "error": run.error,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "created_at": run.created_at.isoformat() if run.created_at else None,
    }


async def execute(tenant_id: str, run_id: str, *, client=None, web_search=None) -> dict:
    """The job: find, deliver and charge. Never raises; a failure is written onto the run.

    Three short tenant transactions rather than one, so the run reads "running" while the actor
    works, and so a failure can still be recorded after the work's transaction rolled back.
    """
    from nexus.core.config import get_settings
    from nexus.models.prospecting import ProspectRun
    from nexus.workers.tasks import tenant_session

    async with tenant_session(tenant_id) as ts:
        run = await ts.get(ProspectRun, run_id)
        if run is None or run.status != "queued":
            return {"skipped": "not_queued", "run_id": run_id}
        run.status, run.started_at = "running", utcnow()

    try:
        async with tenant_session(tenant_id) as ts:
            outcome = await _deliver_run(ts, run_id, client=client, web_search=web_search,
                                         min_fit=get_settings().icp_discovery_min_fit)
    except Exception as exc:
        logger.warning("populate %s failed", run_id, exc_info=True)
        message = getattr(exc, "detail", None) or str(exc) or exc.__class__.__name__
        async with tenant_session(tenant_id) as ts:
            run = await ts.get(ProspectRun, run_id)
            if run is not None:
                run.status, run.finished_at = "failed", utcnow()
                run.error = str(message)[:500]
        return {"failed": True, "run_id": run_id}
    return outcome


async def _deliver_run(ts, run_id: str, *, client, web_search, min_fit: int) -> dict:
    from nexus.billing.meter import metered
    from nexus.models.prospecting import ProspectRun
    from nexus.prospecting.companies import find_icp_companies
    from nexus.prospecting.deliver import deliver
    from nexus.relevance.engine import get_profile

    run = await ts.get(ProspectRun, run_id)
    profile = await get_profile(ts)
    if profile is None or not profile.icp:
        raise RuntimeError("The ICP was removed before companies could be added.")

    chain = await find_icp_companies(
        ts, profile.icp, run.requested, client=client, web_search=web_search,
        web_pool=run.requested * 3, product_context=profile.product_context or "",
    )
    discarded = Counter(chain.discarded)
    account_ids, _ = await deliver(
        ts, profile, chain.candidates, limit=run.requested, source="prospecting",
        min_fit=min_fit, discarded=discarded, owner_user_id=run.user_id,
    )
    if account_ids:
        created = await _domains(ts, account_ids)
        delivered_sources = Counter(c.source for c in chain.candidates if c.domain in created)
        # Keyed on the run: a retried job re-derives the same charge and pays once.
        async with metered(ts, CAPABILITY, quantity=len(account_ids), user_id=run.user_id,
                           source="api", idempotency_key=f"populate:{run_id}",
                           attrs={"run_id": run_id, "sources": dict(delivered_sources)}):
            pass
    else:
        delivered_sources = Counter()
    run.delivered = len(account_ids)
    run.account_ids = account_ids
    run.sources = dict(delivered_sources)
    run.discarded = dict(discarded)
    run.notes = dict(chain.notes)
    run.status, run.finished_at = "done", utcnow()
    return {"run_id": run_id, "delivered": len(account_ids), "sources": dict(delivered_sources)}


async def _domains(ts, account_ids: list[str]) -> set[str]:
    from sqlalchemy import select

    from nexus.models.account import Account

    rows = await ts.session.scalars(select(Account.domain).where(Account.id.in_(account_ids)))
    return {d for d in rows.all() if d}
