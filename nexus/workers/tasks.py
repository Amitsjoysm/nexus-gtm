"""Job handlers. Each handler runs inside a tenant-bound session."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator, Awaitable, Callable

from nexus.core.db import get_sessionmaker
from nexus.core.tenancy import (
    TenantSession,
    apply_rls,
    set_current_tenant,
)
from nexus.models.account import Account
from nexus.pipeline import process_account
from nexus.workers.queue import Job, TaskQueue, get_task_queue

logger = logging.getLogger("nexus.workers")

Handler = Callable[[dict], Awaitable[dict]]


@asynccontextmanager
async def tenant_session(tenant_id: str) -> AsyncIterator[TenantSession]:
    """Bind a tenant and yield a tenant-scoped session (mirrors the API dependency)."""
    set_current_tenant(tenant_id)
    async with get_sessionmaker()() as session:
        await apply_rls(session, tenant_id)
        try:
            yield TenantSession(session, tenant_id)
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            set_current_tenant(None)


async def handle_process_account(payload: dict) -> dict:
    tenant_id = payload["tenant_id"]
    account_id = payload["account_id"]
    async with tenant_session(tenant_id) as ts:
        account = await ts.get(Account, account_id)
        if account is None:
            return {"error": "account_not_found", "account_id": account_id}
        return await process_account(ts, account)


async def handle_run_orchestration(payload: dict) -> dict:
    """Durable path: drive an already-created run to its next stopping point.

    The API creates the run and executes inline to the first gate for snappy feedback;
    this handler is the off-request driver (used after enqueue, or to resume a run that
    a restart left mid-flight). Idempotent: a run in a terminal state is a no-op."""
    tenant_id = payload["tenant_id"]
    run_id = payload["run_id"]
    from nexus.models.orchestration import OrchestrationRun
    from nexus.orchestration.engine import get_orchestration_engine

    async with tenant_session(tenant_id) as ts:
        run = await ts.get(OrchestrationRun, run_id)
        if run is None:
            return {"error": "run_not_found", "run_id": run_id}
        await get_orchestration_engine().execute_run(ts, run)
        return {"run_id": run.id, "status": run.status}


async def handle_run_campaign(payload: dict) -> dict:
    """Off-request campaign driver. ``phase`` selects the work:
    ``"draft"`` runs the draft phase; ``"send"`` runs the send phase. Idempotent — a
    campaign already past the requested phase is a no-op returning its current status."""
    tenant_id = payload["tenant_id"]
    campaign_id = payload["campaign_id"]
    phase = payload.get("phase", "draft")
    from nexus.models.campaign import Campaign, CAMP_AWAITING_APPROVAL, CAMP_TERMINAL
    from nexus.campaigns.service import get_campaign_service

    async with tenant_session(tenant_id) as ts:
        campaign = await ts.get(Campaign, campaign_id)
        if campaign is None:
            return {"error": "campaign_not_found", "campaign_id": campaign_id}
        svc = get_campaign_service()
        if phase == "draft" and campaign.status not in CAMP_TERMINAL \
                and campaign.status != CAMP_AWAITING_APPROVAL:
            await svc.run_draft_phase(ts, campaign)
        elif phase == "send" and campaign.status == "approved":
            await svc.run_send_phase(ts, campaign)
        return {"campaign_id": campaign.id, "status": campaign.status}


HANDLERS: dict[str, Handler] = {
    "process_account": handle_process_account,
    "run_orchestration": handle_run_orchestration,
    "run_campaign": handle_run_campaign,
}


async def enqueue_process_account(
    tenant_id: str, account_id: str, *, queue: TaskQueue | None = None
) -> None:
    queue = queue or get_task_queue()
    await queue.enqueue(
        Job(name="process_account", payload={"tenant_id": tenant_id, "account_id": account_id})
    )


async def enqueue_run_orchestration(
    tenant_id: str, run_id: str, *, queue: TaskQueue | None = None
) -> None:
    queue = queue or get_task_queue()
    await queue.enqueue(
        Job(name="run_orchestration", payload={"tenant_id": tenant_id, "run_id": run_id})
    )


async def enqueue_run_campaign(
    tenant_id: str, campaign_id: str, *, phase: str = "draft", queue: TaskQueue | None = None
) -> None:
    queue = queue or get_task_queue()
    await queue.enqueue(
        Job(
            name="run_campaign",
            payload={"tenant_id": tenant_id, "campaign_id": campaign_id, "phase": phase},
        )
    )


async def dispatch(job: Job) -> dict:
    handler = HANDLERS.get(job.name)
    if handler is None:
        logger.warning("no handler for job %s", job.name)
        return {"error": "unknown_job", "name": job.name}
    try:
        return await handler(job.payload)
    except Exception as exc:  # a bad job must not kill the worker loop
        logger.exception("job %s failed", job.name)
        return {"error": f"{type(exc).__name__}: {exc}"}
