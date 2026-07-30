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


async def handle_advance_cadences(payload: dict) -> dict:
    """Periodic cadence driver. Scans globally for tenants with a due enrollment, then
    advances each tenant's due enrollments inside its own tenant-bound session.

    The scan uses a raw, tenant-agnostic session (it only reads tenant ids, never ORM
    rows), keeping the per-tenant isolation guarantee for the actual work. Inert unless
    ``cadence_enabled`` is set."""
    from datetime import datetime, timezone

    from sqlalchemy import distinct, select

    from nexus.core.config import get_settings
    from nexus.cadences.service import get_cadence_service
    from nexus.models.cadence import CadenceEnrollment, ENROLL_ACTIVE

    settings = get_settings()
    if not settings.cadence_enabled:
        return {"skipped": "cadence_disabled"}

    now = datetime.now(timezone.utc)
    batch = settings.cadence_batch_size

    async with get_sessionmaker()() as session:
        rows = await session.scalars(
            select(distinct(CadenceEnrollment.tenant_id)).where(
                CadenceEnrollment.status == ENROLL_ACTIVE,
                CadenceEnrollment.next_touch_at <= now,
            )
        )
        tenant_ids = list(rows.all())

    processed = 0
    for tid in tenant_ids:
        async with tenant_session(tid) as ts:
            processed += await get_cadence_service().advance_due_for_tenant(
                ts, now=now, limit=batch
            )
    return {"tenants": len(tenant_ids), "processed": processed}


async def handle_refresh_due_accounts(payload: dict) -> dict:
    """Periodic account-refresh driver. Scans globally for accounts that are due for a
    refresh (stale or never refreshed) and belong to a tenant that has opted in, stamps
    each as claimed, and enqueues a ``process_account`` job per account.

    ``process_account`` runs the full sense→act loop (ingest signals → score → inbox →
    plays → alerts), so this one driver delivers ingestion refresh, rescoring, play
    evaluation, and alerts. Inert unless the global ``automation_enabled`` switch is set.

    The global scan uses a raw, tenant-agnostic session (it reads only ids), preserving
    per-tenant isolation for the actual stamping work below. ``now`` is overridable via
    ``payload['now_iso']`` for deterministic tests."""
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import or_, select

    from nexus.core.config import get_settings
    from nexus.models.account import Account
    from nexus.models.identity import Tenant

    settings = get_settings()
    if not settings.automation_enabled:
        return {"skipped": "automation_disabled"}

    now = (
        datetime.fromisoformat(payload["now_iso"])
        if payload.get("now_iso")
        else datetime.now(timezone.utc)
    )
    cutoff = now - timedelta(seconds=settings.account_refresh_interval_s)
    batch = settings.account_refresh_batch_size

    async with get_sessionmaker()() as session:
        stmt = (
            select(Account.tenant_id, Account.id)
            .join(Tenant, Tenant.id == Account.tenant_id)
            .where(
                Tenant.automation_enabled == True,  # noqa: E712
                or_(
                    Account.last_refreshed_at.is_(None),
                    Account.last_refreshed_at <= cutoff,
                ),
            )
            .order_by(Account.last_refreshed_at.asc().nulls_first())
            .limit(batch)
        )
        if settings.is_postgres:
            stmt = stmt.with_for_update(skip_locked=True, of=Account)
        rows = (await session.execute(stmt)).all()

    # group selected account ids by tenant
    by_tenant: dict[str, list[str]] = {}
    for tenant_id, account_id in rows:
        by_tenant.setdefault(tenant_id, []).append(account_id)

    refreshed = 0
    for tid, account_ids in by_tenant.items():
        async with tenant_session(tid) as ts:
            for aid in account_ids:
                account = await ts.get(Account, aid)
                if account is None:
                    continue
                account.last_refreshed_at = now  # claim — excludes it from the next tick
                await enqueue_process_account(tid, aid)
                refreshed += 1

    return {"tenants": len(by_tenant), "accounts": refreshed}


async def handle_sync_crm_due_accounts(payload: dict) -> dict:
    """Heartbeat backstop: scan globally for accounts that are due for a CRM sync (never synced
    or changed since last sync) in opted-in tenants, and push each to the configured CRM.

    Mirrors handle_refresh_due_accounts: a raw, tenant-agnostic id-scan (reads only ids, so
    per-tenant isolation is preserved by the RLS-scoped work below), then per-tenant sessions do
    the actual push via the shared sync_account_to_crm. Inert unless the global crm_sync_enabled
    switch is set. ``now`` is overridable via payload['now_iso'] for deterministic tests.
    """
    from datetime import datetime, timezone

    from sqlalchemy import or_, select

    from nexus.core.config import get_settings
    from nexus.ingestion.crm import get_crm_connector
    from nexus.ingestion.crm_sync import sync_account_to_crm
    from nexus.models.account import Account
    from nexus.models.identity import Tenant

    settings = get_settings()
    if not settings.crm_sync_enabled:
        return {"skipped": "crm_sync_disabled"}

    now = (
        datetime.fromisoformat(payload["now_iso"])
        if payload.get("now_iso")
        else datetime.now(timezone.utc)
    )
    batch = settings.crm_sync_batch_size

    async with get_sessionmaker()() as session:
        stmt = (
            select(Account.tenant_id, Account.id)
            .join(Tenant, Tenant.id == Account.tenant_id)
            .where(
                Tenant.automation_enabled == True,  # noqa: E712
                or_(
                    Account.crm_synced_at.is_(None),
                    Account.updated_at > Account.crm_synced_at,
                ),
            )
            .order_by(Account.crm_synced_at.asc().nulls_first())
            .limit(batch)
        )
        if settings.is_postgres:
            stmt = stmt.with_for_update(skip_locked=True, of=Account)
        rows = (await session.execute(stmt)).all()

    by_tenant: dict[str, list[str]] = {}
    for tenant_id, account_id in rows:
        by_tenant.setdefault(tenant_id, []).append(account_id)

    connector = get_crm_connector()
    synced = 0
    for tid, account_ids in by_tenant.items():
        async with tenant_session(tid) as ts:
            for aid in account_ids:
                account = await ts.get(Account, aid)
                if account is None:
                    continue
                await sync_account_to_crm(ts, account, connector=connector, now=now)
                synced += 1

    return {"tenants": len(by_tenant), "accounts": synced}


async def handle_sync_crm_account(payload: dict) -> dict:
    """Event fast-path: sync a single account to the CRM. Gated by the global switch AND the
    tenant's automation_enabled opt-in (the authoritative gate)."""
    from datetime import datetime, timezone

    from nexus.core.config import get_settings
    from nexus.ingestion.crm import get_crm_connector
    from nexus.ingestion.crm_sync import sync_account_to_crm
    from nexus.models.account import Account
    from nexus.models.identity import Tenant

    if not get_settings().crm_sync_enabled:
        return {"skipped": "crm_sync_disabled"}

    tid = payload["tenant_id"]
    aid = payload["account_id"]
    async with tenant_session(tid) as ts:
        tenant = await ts.session.get(Tenant, tid)
        if tenant is None or not tenant.automation_enabled:
            return {"skipped": "tenant_opted_out"}
        account = await ts.get(Account, aid)
        if account is None:
            return {"skipped": "account_missing"}
        res = await sync_account_to_crm(
            ts, account, connector=get_crm_connector(), now=datetime.now(timezone.utc)
        )
    return {"account_id": aid, "ok": res.ok}


async def handle_send_daily_digests(payload: dict) -> dict:
    """Daily digest: one email-channel alert per opted-in tenant summarizing the last digest
    interval (new signals, accounts scored, open tasks). Idempotent per interval — the previous
    digest's timestamp is the gate — so the heartbeat can enqueue this every tick. Quiet
    workspaces get no digest (an empty digest trains reps to ignore the real ones).
    ``now`` is overridable via payload['now_iso'] for deterministic tests."""
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import func, select

    from nexus.core.config import get_settings
    from nexus.core.db import ensure_aware
    from nexus.models.alerts import Alert
    from nexus.models.identity import Tenant
    from nexus.models.intelligence import AccountScore
    from nexus.models.signal import SignalEvent
    from nexus.models.workflow import InboxTask

    settings = get_settings()
    if not settings.automation_enabled:
        return {"skipped": "automation_disabled"}

    now = (
        datetime.fromisoformat(payload["now_iso"])
        if payload.get("now_iso")
        else datetime.now(timezone.utc)
    )
    interval = timedelta(hours=settings.digest_interval_hours)
    window_start = now - interval

    async with get_sessionmaker()() as session:
        tenant_ids = (
            await session.scalars(
                select(Tenant.id).where(Tenant.automation_enabled == True)  # noqa: E712
            )
        ).all()

    sent = 0
    for tid in tenant_ids:
        async with tenant_session(tid) as ts:
            last = await ts.session.scalar(
                select(func.max(Alert.created_at)).where(
                    Alert.tenant_id == tid, Alert.source == "digest"
                )
            )
            if last is not None and ensure_aware(last) > window_start:
                continue  # this interval's digest already went out

            signals = int(
                (
                    await ts.session.scalar(
                        select(func.count())
                        .select_from(SignalEvent)
                        .where(
                            SignalEvent.tenant_id == tid,
                            SignalEvent.occurred_at >= window_start,
                        )
                    )
                )
                or 0
            )
            scored = int(
                (
                    await ts.session.scalar(
                        select(func.count())
                        .select_from(AccountScore)
                        .where(
                            AccountScore.tenant_id == tid,
                            AccountScore.computed_at >= window_start,
                        )
                    )
                )
                or 0
            )
            open_tasks = int(
                (
                    await ts.session.scalar(
                        select(func.count())
                        .select_from(InboxTask)
                        .where(InboxTask.tenant_id == tid, InboxTask.status == "open")
                    )
                )
                or 0
            )
            if signals == 0 and scored == 0 and open_tasks == 0:
                continue

            ts.add(
                Alert(
                    tenant_id=tid,
                    title=f"Your NEXUS digest: {signals} new signals, {open_tasks} tasks waiting",
                    body=(
                        f"In the last {settings.digest_interval_hours} hours: {signals} buying "
                        f"signals detected, {scored} accounts (re)scored, {open_tasks} inbox "
                        "tasks open. Open your inbox to work the highest-priority accounts first."
                    ),
                    severity="info",
                    channel="email",
                    source="digest",
                    meta={"signals": signals, "scored": scored, "open_tasks": open_tasks},
                )
            )
            sent += 1

    return {"digests": sent, "tenants": len(tenant_ids)}


async def handle_discover_icp_accounts(payload: dict) -> dict:
    """Daily ICP auto-discovery: for each opted-in tenant, add net-new accounts that strictly
    match the saved ICP. Idempotent per interval via ``Tenant.icp_discovery_last_run_at`` so the
    heartbeat can enqueue it every tick. ``now`` is overridable via payload['now_iso'] for tests."""
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select

    from nexus.core.config import get_settings
    from nexus.core.db import ensure_aware
    from nexus.discovery.auto import auto_discover_for_tenant
    from nexus.models.identity import Tenant

    settings = get_settings()
    if not (settings.automation_enabled and settings.icp_discovery_enabled):
        return {"skipped": "disabled"}

    now = (
        datetime.fromisoformat(payload["now_iso"])
        if payload.get("now_iso")
        else datetime.now(timezone.utc)
    )
    interval = timedelta(hours=settings.icp_discovery_interval_hours)

    async with get_sessionmaker()() as session:
        tenant_ids = (
            await session.scalars(
                select(Tenant.id).where(Tenant.automation_enabled == True)  # noqa: E712
            )
        ).all()

    discovered = 0
    for tid in tenant_ids:
        async with tenant_session(tid) as ts:
            # Row-lock the tenant so two workers can't both pass the interval check and run
            # discovery (double LLM/search spend). A concurrent worker blocks on this lock until
            # we commit our stamp below, then reads the fresh timestamp and skips. On Postgres this
            # is SELECT ... FOR UPDATE; on SQLite it's a no-op (writes already serialize), and the
            # dev deploy runs a single worker, so there is no race there anyway.
            tenant = await ts.session.get(Tenant, tid, with_for_update=True)
            last = tenant.icp_discovery_last_run_at if tenant else None
            if last is not None and ensure_aware(last) > now - interval:
                continue  # already ran this interval (possibly just claimed by another worker)
            # Per-workspace daily target (SDR-selectable in Settings); NULL -> platform default.
            target = (
                tenant.icp_daily_count
                if tenant is not None and tenant.icp_daily_count
                else settings.icp_discovery_daily_count
            )
            pool = max(target * settings.icp_discovery_pool_multiplier, target)
            res = await auto_discover_for_tenant(
                ts,
                target_count=target,
                min_fit=settings.icp_discovery_min_fit,
                pool_limit=pool,
            )
            # Only consume the per-interval slot when discovery actually ran. A tenant with no ICP
            # yet is re-checked cheaply each tick, so discovery fires the moment an ICP is added.
            if res.get("skipped") == "no_icp":
                # Surface the paused state in-app instead of skipping silently — the #1 reason
                # "no new accounts are showing up" reports reach support.
                await _ensure_icp_paused_alert(ts)
            else:
                if tenant is not None:
                    tenant.icp_discovery_last_run_at = now
                await _resolve_icp_paused_alert(ts)
            discovered += res.get("discovered", 0)
    return {"discovered": discovered, "tenants": len(tenant_ids)}


async def handle_sync_network_account(payload: dict) -> dict:
    """Pull a member's network source via its connector and fold the batch into the graph.

    OAuth providers: decrypt the stored token bundle, refresh it if the access token is expired
    (persisting the rotated bundle), then fetch with a valid access token. Connector/API failures
    are captured on the account (status=error, last_error) and surfaced in the UI, not swallowed.
    Idempotent: re-running re-upserts identities/edges and advances the sync cursor.
    """
    import time

    from nexus.models.network import NetworkSourceAccount
    from nexus.network.connectors.base import SourceAccountRef
    from nexus.network.connectors.oauthbase import OAuthConnector
    from nexus.network.connectors.registry import get_network_connector
    from nexus.network.crypto import seal_tokens, unseal_tokens
    from nexus.network.service import ingest_batch

    tid = payload["tenant_id"]
    account_id = payload["account_id"]
    async with tenant_session(tid) as ts:
        acc = await ts.get(NetworkSourceAccount, account_id)
        if acc is None:
            return {"error": "account_not_found", "account_id": account_id}
        try:
            connector = get_network_connector(acc.provider)
        except ValueError:
            # Upload-only / manual source (e.g. linkedin) — no live connector to sync from.
            return {"skipped": "manual_source", "account_id": account_id}

        oauth_for_fetch: dict = {}
        if isinstance(connector, OAuthConnector):
            bundle = unseal_tokens(acc.oauth)
            # No usable token at all (never connected, or fully revoked) — user must reconnect.
            if not bundle.get("access_token") and not bundle.get("refresh_token"):
                acc.status = "error"
                acc.last_error = "not connected (no token) — reconnect required"
                return {"error": "not_connected", "account_id": account_id}
            if _token_expired(bundle, now=int(time.time())) and bundle.get("refresh_token"):
                try:
                    new = await connector.refresh(bundle["refresh_token"])
                except Exception as exc:  # refresh failed → user must reconnect
                    acc.status = "error"
                    acc.last_error = f"token refresh failed: {type(exc).__name__}"
                    return {"error": "refresh_failed", "account_id": account_id}
                bundle["access_token"] = new.get("access_token", bundle.get("access_token"))
                if new.get("refresh_token"):
                    bundle["refresh_token"] = new["refresh_token"]
                if new.get("expires_in"):
                    bundle["expires_at"] = int(time.time()) + int(new["expires_in"])
                acc.oauth = seal_tokens(bundle)
            oauth_for_fetch = {"access_token": bundle.get("access_token", "")}

        ref = SourceAccountRef(
            id=acc.id, provider=acc.provider,
            external_account_id=acc.external_account_id, oauth=oauth_for_fetch,
        )
        try:
            batch = await connector.fetch(ref, acc.sync_cursor)
        except Exception as exc:
            acc.status = "error"
            acc.last_error = f"sync failed: {type(exc).__name__}: {str(exc)[:200]}"
            return {"error": "fetch_failed", "account_id": account_id}
        acc.status = "connected"
        acc.last_error = None
        res = await ingest_batch(ts, acc, batch)
    return {"account_id": account_id, **res}


def _token_expired(bundle: dict, *, now: int, skew_s: int = 60) -> bool:
    """True when there's an expiry and it's within ``skew_s`` of now (refresh proactively)."""
    exp = bundle.get("expires_at")
    return exp is not None and int(exp) <= now + skew_s


async def _ensure_icp_paused_alert(ts) -> None:
    """One standing in-app alert while daily discovery is paused for lack of an ICP.

    Idempotent: created only when no open one exists, so the heartbeat can call this every
    tick without spamming the Alerts feed."""
    from nexus.models.alerts import Alert

    existing = await ts.first(Alert, Alert.source == "icp_discovery", Alert.status == "open")
    if existing is not None:
        return
    ts.add(
        Alert(
            tenant_id=ts.tenant_id,
            title="Daily account discovery is paused — no ICP defined",
            body=(
                "Automation is on, but this workspace has no Ideal Customer Profile, so the "
                "daily net-new account discovery has nothing to match against. Define your "
                "industries, company-size band, and geography on the Relevance page and "
                "discovery starts on the next daily run."
            ),
            severity="warning",
            channel="in_app",
            source="icp_discovery",
        )
    )


async def _resolve_icp_paused_alert(ts) -> None:
    """Auto-ack the standing paused alert once discovery actually runs (ICP was defined)."""
    from nexus.core.db import utcnow as _utcnow
    from nexus.models.alerts import Alert

    existing = await ts.first(Alert, Alert.source == "icp_discovery", Alert.status == "open")
    if existing is not None:
        existing.status = "acked"
        existing.acked_at = _utcnow()


async def handle_rollup_usage(payload: dict) -> dict:
    """Periodic driver: fold each tenant's usage events into rollups.

    Mirrors the existing cross-tenant sweep pattern (a raw, tenant-agnostic id scan, then
    per-tenant sessions for the RLS-scoped work). Idempotent, so the heartbeat may enqueue it
    every tick. Never raises: one bad tenant must not stop the sweep.

    Scoped to the CURRENT billing period on both sides — the tenant scan and the rebuild. This
    job runs on every heartbeat tick forever, so an unbounded rebuild would re-read a tenant's
    entire event history each time: fine at a thousand events, ruinous at ten million. Closed
    periods are already final, and the one case that needs a wider window — a straggler written
    just after a period boundary — is picked up by the full-window rebuild at invoicing time.
    """
    from sqlalchemy import distinct, select

    from nexus.billing.rollups import period_start, rebuild_rollups
    from nexus.core.db import utcnow
    from nexus.models.billing import BillingUsageEvent

    since = period_start(utcnow())
    async with get_sessionmaker()() as session:
        tenant_ids = list(
            (
                await session.scalars(
                    select(distinct(BillingUsageEvent.tenant_id)).where(
                        BillingUsageEvent.occurred_at >= since
                    )
                )
            ).all()
        )

    processed = 0
    for tid in tenant_ids:
        try:
            async with tenant_session(tid) as ts:
                await rebuild_rollups(ts, since=since)
            processed += 1
        except Exception:
            logger.warning("usage rollup failed for tenant %s", tid, exc_info=True)
    return {"tenants": processed}


async def handle_roll_billing_periods(payload: dict) -> dict:
    """Periodic driver: close every billing period that has ended.

    Mirrors ``handle_rollup_usage`` — a raw, tenant-agnostic id scan, then per-tenant sessions
    for the RLS-scoped work. The scan is the pre-filter (only subscriptions whose window has
    actually elapsed); ``roll_period`` re-checks the boundary itself, so an id that goes stale
    between the scan and the roll is a no-op rather than an early close.

    Idempotent, so the heartbeat may enqueue it every tick: a rolled subscription's window is
    already in the future, and the new period's credit grant is keyed by period so a retry can
    never double-grant. Never raises — one bad tenant must not stop the sweep.
    """
    from sqlalchemy import distinct, select

    from nexus.billing.subscriptions import ACTIVE_STATUSES, roll_period
    from nexus.core.db import utcnow
    from nexus.models.billing import BillingSubscription

    now = utcnow()
    async with get_sessionmaker()() as session:
        tenant_ids = list(
            (
                await session.scalars(
                    select(distinct(BillingSubscription.tenant_id)).where(
                        BillingSubscription.status.in_(ACTIVE_STATUSES),
                        BillingSubscription.current_period_end.is_not(None),
                        BillingSubscription.current_period_end <= now,
                    )
                )
            ).all()
        )

    rolled = 0
    for tid in tenant_ids:
        try:
            async with tenant_session(tid) as ts:
                if await roll_period(ts):
                    rolled += 1
        except Exception:
            logger.warning("billing period roll failed for tenant %s", tid, exc_info=True)
    return {"rolled": rolled}


async def handle_dunning_sweep(payload: dict) -> dict:
    """Periodic driver: retry failed collections on schedule, escalate when exhausted.

    Scoped to tenants that actually owe something, so the common case costs one indexed scan.
    Each invoice carries its own next-attempt time, so running this every tick still only
    charges on schedule. Never raises: one bad tenant must not stop the sweep.
    """
    from sqlalchemy import distinct, select

    from nexus.billing.dunning import run_dunning
    from nexus.core.config import get_settings
    from nexus.models.billing import BillingInvoice

    if not get_settings().billing_dunning_enabled:
        return {"skipped": "dunning_disabled"}

    async with get_sessionmaker()() as session:
        tenant_ids = list(
            (
                await session.scalars(
                    select(distinct(BillingInvoice.tenant_id)).where(
                        BillingInvoice.status == "finalized",
                        BillingInvoice.total_cents > 0,
                    )
                )
            ).all()
        )

    totals = {"tenants": 0, "attempted": 0, "recovered": 0, "exhausted": 0}
    for tid in tenant_ids:
        try:
            async with tenant_session(tid) as ts:
                res = await run_dunning(ts)
            totals["tenants"] += 1
            for k in ("attempted", "recovered", "exhausted"):
                totals[k] += res.get(k, 0)
        except Exception:
            logger.warning("dunning sweep failed for tenant %s", tid, exc_info=True)
    return totals


HANDLERS: dict[str, Handler] = {
    "process_account": handle_process_account,
    "run_orchestration": handle_run_orchestration,
    "run_campaign": handle_run_campaign,
    "advance_cadences": handle_advance_cadences,
    "refresh_due_accounts": handle_refresh_due_accounts,
    "sync_crm_account": handle_sync_crm_account,
    "sync_crm_due_accounts": handle_sync_crm_due_accounts,
    "send_daily_digests": handle_send_daily_digests,
    "discover_icp_accounts": handle_discover_icp_accounts,
    "sync_network_account": handle_sync_network_account,
    "rollup_usage": handle_rollup_usage,
    "roll_billing_periods": handle_roll_billing_periods,
    "dunning_sweep": handle_dunning_sweep,
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


async def enqueue_advance_cadences(*, queue: TaskQueue | None = None) -> None:
    queue = queue or get_task_queue()
    await queue.enqueue(Job(name="advance_cadences", payload={}))


async def enqueue_refresh_due_accounts(*, queue: TaskQueue | None = None) -> None:
    queue = queue or get_task_queue()
    await queue.enqueue(Job(name="refresh_due_accounts", payload={}))


async def enqueue_sync_crm_account(
    tenant_id: str, account_id: str, *, queue: TaskQueue | None = None
) -> None:
    queue = queue or get_task_queue()
    await queue.enqueue(
        Job(name="sync_crm_account", payload={"tenant_id": tenant_id, "account_id": account_id})
    )


async def enqueue_sync_crm_due_accounts(*, queue: TaskQueue | None = None) -> None:
    queue = queue or get_task_queue()
    await queue.enqueue(Job(name="sync_crm_due_accounts", payload={}))


async def enqueue_send_daily_digests(*, queue: TaskQueue | None = None) -> None:
    queue = queue or get_task_queue()
    await queue.enqueue(Job(name="send_daily_digests", payload={}))


async def enqueue_discover_icp_accounts(*, queue: TaskQueue | None = None) -> None:
    queue = queue or get_task_queue()
    await queue.enqueue(Job(name="discover_icp_accounts", payload={}))


async def enqueue_sync_network_account(
    tenant_id: str, account_id: str, *, queue: TaskQueue | None = None
) -> None:
    queue = queue or get_task_queue()
    await queue.enqueue(
        Job(name="sync_network_account",
            payload={"tenant_id": tenant_id, "account_id": account_id})
    )


async def enqueue_rollup_usage(*, queue: TaskQueue | None = None) -> None:
    queue = queue or get_task_queue()
    await queue.enqueue(Job(name="rollup_usage", payload={}))


async def enqueue_roll_billing_periods(*, queue: TaskQueue | None = None) -> None:
    queue = queue or get_task_queue()
    await queue.enqueue(Job(name="roll_billing_periods", payload={}))


async def enqueue_dunning_sweep(*, queue: TaskQueue | None = None) -> None:
    queue = queue or get_task_queue()
    await queue.enqueue(Job(name="dunning_sweep", payload={}))


# Marks a dispatch whose handler RAISED. Deliberately not the plain "error" key: handlers
# legitimately *return* {"error": "account_not_found"} as a normal, terminal outcome, and
# retrying those three times would be a new bug rather than a fix for the old one.
JOB_FAILED_KEY = "__job_failed__"


def is_job_failure(result: dict) -> bool:
    """True only for a dispatch whose handler raised — i.e. something worth retrying."""
    return isinstance(result, dict) and bool(result.get(JOB_FAILED_KEY))


async def dispatch(job: Job) -> dict:
    """Route one job to its handler.

    On success the handler's own dict is returned untouched — that contract predates M11 and
    callers rely on it. On a raised exception the failure is *surfaced* (it used to be swallowed
    here, which is how failed jobs went missing) as the same ``{"error": ...}`` shape plus
    ``JOB_FAILED_KEY``, so the worker loop can retry or dead-letter it.
    """
    handler = HANDLERS.get(job.name)
    if handler is None:
        # Not a retryable failure: an unroutable name will not route on the next attempt either,
        # so retrying it is a guaranteed-useless spin ending in a pointless dead letter.
        logger.warning("no handler for job %s", job.name)
        return {"error": "unknown_job", "name": job.name}
    try:
        return await handler(job.payload)
    except Exception as exc:  # a bad job must not kill the worker loop
        logger.exception(
            "job %s failed (attempt %s/%s)", job.name, job.attempts + 1, job.max_attempts
        )
        return {"error": f"{type(exc).__name__}: {exc}", JOB_FAILED_KEY: True}
