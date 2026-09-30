"""Job handlers. Each handler runs inside a tenant-bound session."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import timedelta
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
        return await process_account(ts, account, scheduled=bool(payload.get("scheduled")))


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

    from sqlalchemy import select

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
    batch = settings.account_refresh_batch_size

    async with get_sessionmaker()() as session:
        # Claims on the stored due-time rather than deriving one. The old predicate
        # (`last_refreshed_at IS NULL OR <= cutoff`, ordered ASC NULLS FIRST) could not use a
        # btree: measured at 500k accounts it seq-scanned the table and sorted 261k rows through a
        # 26 MB external merge on disk to return 100, every tick. This is an index scan that stops
        # at the limit — 489 ms -> 44 ms on the same rows, and O(batch) instead of O(estate).
        stmt = (
            select(Account.tenant_id, Account.id)
            .join(Tenant, Tenant.id == Account.tenant_id)
            .where(
                Tenant.automation_enabled == True,  # noqa: E712
                Account.next_refresh_at <= now,
            )
            .order_by(Account.next_refresh_at.asc())
            .limit(batch)
        )
        if settings.is_postgres:
            stmt = stmt.with_for_update(skip_locked=True, of=Account)
        rows = (await session.execute(stmt)).all()

    # group selected account ids by tenant
    by_tenant: dict[str, list[str]] = {}
    for tenant_id, account_id in rows:
        by_tenant.setdefault(tenant_id, []).append(account_id)

    # The claim's conservative default. The pipeline re-stamps this with the account's real tier
    # once it knows what the crawl found; this value is what protects an account whose processing
    # never completes — it comes back on the old 6h cycle rather than stalling forever, which is
    # exactly the pre-tiering behaviour.
    claim_due = now + timedelta(seconds=settings.account_refresh_interval_s)

    refreshed = 0
    for tid, account_ids in by_tenant.items():
        async with tenant_session(tid) as ts:
            for aid in account_ids:
                account = await ts.get(Account, aid)
                if account is None:
                    continue
                account.last_refreshed_at = now
                account.next_refresh_at = claim_due  # claim — excludes it from the next tick
                # The daily scan: enriches from source databases and the B2B actor only, never Exa.
                await enqueue_process_account(tid, aid, scheduled=True)
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
    from nexus.ingestion import crm_credentials
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

    synced = 0
    for tid, account_ids in by_tenant.items():
        async with tenant_session(tid) as ts:
            # Resolve ONCE per tenant, inside that tenant's session: each tenant syncs to its own
            # CRM. Resolving once for the whole sweep — as this did before per-tenant credentials
            # — pushed every tenant's accounts into whichever portal the deployment env named.
            # Hoisted out of the account loop so a tenant with N due accounts still costs one
            # credential lookup, not N.
            connector = await crm_credentials.resolve_crm_connector(ts)
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
    from nexus.ingestion import crm_credentials
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
            ts, account,
            connector=await crm_credentials.resolve_crm_connector(ts),
            now=datetime.now(timezone.utc),
        )
    return {"account_id": aid, "ok": res.ok}


async def handle_alert_digests(payload: dict) -> dict:
    """Per-USER digest sweep: deliver the alerts routing held back.

    Distinct from `send_daily_digests`, which is a tenant-level activity summary. This closes the
    gap where `routing.py` decided an alert should wait for a digest and nothing ever sent it — the
    alert existed, the routing was right, and the person was never told.

    Scoped to tenants that actually hold digest-mode preferences, so the common case is one indexed
    scan. Never raises: one bad tenant must not stop the sweep for the rest.
    """
    from sqlalchemy import distinct, select

    from nexus.alerts.digest import run_digest_sweep
    from nexus.models.notification_preference import NotificationPreference

    async with get_sessionmaker()() as session:
        tenant_ids = list(
            (
                await session.scalars(
                    select(distinct(NotificationPreference.tenant_id)).where(
                        NotificationPreference.mode == "digest"
                    )
                )
            ).all()
        )

    totals = {"tenants": 0, "sent": 0, "empty": 0, "alerts": 0}
    for tid in tenant_ids:
        try:
            async with tenant_session(tid) as ts:
                res = await run_digest_sweep(ts)
            totals["tenants"] += 1
            for k in ("sent", "empty", "alerts"):
                totals[k] += res.get(k, 0)
        except Exception:
            logger.warning("alert digest sweep failed for tenant %s", tid, exc_info=True)
    return totals


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
    """Daily ICP auto-discovery: for each opted-in tenant, add net-new accounts that strictly match
    the saved ICP, up to the number the workspace set. ``now`` is overridable via
    payload['now_iso'] for tests.

    ``Tenant.icp_discovery_last_run_at`` is the START of the current interval. Within it a workspace
    that is still short of its number gets another pass, at most ``icp_discovery_max_attempts`` in
    all and ``icp_discovery_retry_hours`` apart, until the number is met or a clean pass finds
    nothing new. It used to be one pass per interval that consumed the slot whatever it delivered,
    which is how a workspace set to 10 a day received 1-3 for two weeks with nothing saying why.
    Every pass is recorded as a ``ProspectRun(kind="daily")``: the tally, the retry decision and
    the reason Settings shows are all read from those rows.

    One workspace failing never stops the others: each runs in its own try, and a failure is
    recorded so the retry gap applies to it too rather than re-running it on every tick.
    """
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select

    from nexus.core.config import get_settings
    from nexus.core.db import ensure_aware
    from nexus.discovery import auto
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
    retry_gap = timedelta(hours=max(1, settings.icp_discovery_retry_hours))
    max_attempts = max(1, settings.icp_discovery_max_attempts)

    async with get_sessionmaker()() as session:
        tenant_ids = (
            await session.scalars(
                select(Tenant.id).where(Tenant.automation_enabled == True)  # noqa: E712
            )
        ).all()

    discovered = 0
    for tid in tenant_ids:
        need = 0
        try:
            async with tenant_session(tid) as ts:
                # Row-lock the tenant so two workers can't both decide to run a pass (double search
                # spend). A concurrent worker blocks here until we commit, then sees the new run.
                # On SQLite this is a no-op; writes already serialize there.
                tenant = await ts.session.get(Tenant, tid, with_for_update=True)
                if tenant is None:
                    continue
                target = tenant.icp_daily_count or settings.icp_discovery_daily_count
                start = tenant.icp_discovery_last_run_at
                new_window = start is None or ensure_aware(start) <= now - interval
                runs = [] if new_window else await auto.runs_since(ts, start)
                if runs:
                    delivered = sum(r.delivered or 0 for r in runs)
                    last = runs[-1]
                    if (delivered >= target or len(runs) >= max_attempts
                            or not auto.pass_is_worth_repeating(last)
                            or ensure_aware(last.created_at) > now - retry_gap):
                        continue
                    need = target - delivered
                else:
                    need = target
                pool = max(need * settings.icp_discovery_pool_multiplier, need)
                res = await auto.auto_discover_for_tenant(
                    ts, target_count=need, min_fit=settings.icp_discovery_min_fit,
                    pool_limit=pool,
                )
                if res.get("skipped") == "no_icp":
                    # The slot is not consumed: discovery fires the moment an ICP is added. The
                    # paused state is surfaced in-app, the #1 reason "no new accounts" reaches
                    # support.
                    await _ensure_icp_paused_alert(ts)
                    continue
                if new_window:
                    tenant.icp_discovery_last_run_at = now
                ts.add(_daily_run(tid, now, need, res))
                await _resolve_icp_paused_alert(ts)
                discovered += res.get("discovered", 0)
                logger.info(
                    "icp discovery %s: asked %s, delivered %s, sources %s, discarded %s, notes %s",
                    tid, need, res.get("discovered", 0), res.get("sources"), res.get("discarded"),
                    res.get("notes") or res.get("skipped"),
                )
        except Exception as exc:
            logger.exception("icp discovery failed for tenant %s", tid)
            await _record_failed_pass(tid, now, need, exc, interval)
    return {"discovered": discovered, "tenants": len(tenant_ids)}


def _daily_run(tenant_id: str, now, need: int, res: dict):
    from nexus.models.prospecting import ProspectRun

    return ProspectRun(
        tenant_id=tenant_id, kind="daily", requested=need,
        delivered=int(res.get("discovered", 0) or 0),
        status="done" if not res.get("skipped") else str(res["skipped"])[:16],
        sources=res.get("sources") or {}, discarded=res.get("discarded") or {},
        notes=res.get("notes") or {}, account_ids=list(res.get("account_ids") or []),
        started_at=now, finished_at=now, created_at=now,
    )


async def _record_failed_pass(tenant_id: str, now, need: int, exc: BaseException, interval) -> None:
    """Record a pass that raised, in its own transaction (the pass's own was rolled back), so the
    retry gap applies to it. Opens the interval if none is open, or a failing workspace would be
    re-run on every tick. Never raises."""
    from nexus.core.db import ensure_aware
    from nexus.models.identity import Tenant
    from nexus.models.prospecting import ProspectRun

    try:
        async with tenant_session(tenant_id) as ts:
            tenant = await ts.session.get(Tenant, tenant_id)
            start = tenant.icp_discovery_last_run_at if tenant else None
            if tenant is not None and (start is None or ensure_aware(start) <= now - interval):
                tenant.icp_discovery_last_run_at = now
            ts.add(ProspectRun(
                tenant_id=tenant_id, kind="daily", requested=need, delivered=0, status="failed",
                sources={}, discarded={}, notes={}, account_ids=[],
                error=f"{type(exc).__name__}: {exc}"[:500],
                started_at=now, finished_at=now, created_at=now,
            ))
    except Exception:
        logger.warning("could not record the failed discovery pass for %s", tenant_id,
                       exc_info=True)


async def handle_populate_accounts(payload: dict) -> dict:
    """A person's "add N companies now" (``nexus/prospecting/populate.py``).

    The run is committed before this job is queued; if a worker still cannot see it, raising lets
    the durability layer retry with backoff rather than dropping the request with the run stuck at
    "queued" forever.
    """
    from nexus.models.prospecting import ProspectRun
    from nexus.prospecting.populate import execute

    tenant_id, run_id = payload["tenant_id"], payload["run_id"]
    async with tenant_session(tenant_id) as ts:
        if await ts.get(ProspectRun, run_id) is None:
            raise RuntimeError(f"populate run {run_id} is not visible yet")
    return await execute(tenant_id, run_id)


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


async def handle_prune_web_cache(payload: dict) -> dict:
    """Delete expired `web_cache` rows.

    Idempotent and served by the index on `expires_at`, so the heartbeat may enqueue it every tick.
    Never raises: `cache.prune` returns 0 on any failure, because a cache that cannot tidy itself
    must not fail a job other work is queued behind.
    """
    from nexus.fetching import cache

    return {"pruned": await cache.prune()}


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


#: A call younger than this may still be about to be logged by the rep, who charges it then.
UNLOGGED_CALL_GRACE = timedelta(hours=2)
#: After this, a call Twilio still cannot describe is given up on: nothing measured, nothing charged.
UNLOGGED_CALL_GIVE_UP = timedelta(days=7)
#: Twilio statuses that mean the call is over and its duration final.
_FINISHED = {"completed", "busy", "no-answer", "failed", "canceled"}


async def handle_charge_unlogged_calls(payload: dict) -> dict:
    """Charge platform calls nobody logged an outcome for (decided with the product owner 2026-09-23).

    A platform call was charged only at disposition, so a call nobody logged was free. Every live
    platform call is recorded at dial (`placed_calls`); this charges the ones still uncharged two
    hours on, on Twilio's MEASURED duration, under the same `call:<id>` key the disposition uses —
    so the two can never both charge. Self-filtering and idempotent, so the scheduler enqueues it
    every tick like the other billing sweeps.

    Asks the PLATFORM provider, never a workspace's: these calls were placed on our account, and a
    workspace that has since connected its own Twilio has no record of them.
    """
    from sqlalchemy import select

    from nexus.calling.connection import platform_call_provider
    from nexus.calling.service import _charge_minutes, _mark_charged
    from nexus.core.db import ensure_aware, get_platform_sessionmaker, utcnow
    from nexus.models.calling import PlacedCall

    now = utcnow()
    # Cross-tenant, so the platform role: under the RLS-bound one this would see zero rows and
    # report nothing owed. Ids only; the charging happens in each tenant's own session below.
    async with get_platform_sessionmaker()() as session:
        rows = (await session.execute(
            select(PlacedCall.tenant_id, PlacedCall.provider_call_id)
            .where(PlacedCall.charged_at.is_(None),
                   PlacedCall.placed_at <= now - UNLOGGED_CALL_GRACE)
            .order_by(PlacedCall.placed_at.asc())
            .limit(200)
        )).all()
    if not rows:
        return {"charged": 0}

    provider = await platform_call_provider()
    charged = given_up = 0
    by_tenant: dict[str, list[str]] = {}
    for tenant_id, call_id in rows:
        by_tenant.setdefault(tenant_id, []).append(call_id)
    for tenant_id, call_ids in by_tenant.items():
        async with tenant_session(tenant_id) as ts:
            for call_id in call_ids:
                row = await ts.first(PlacedCall, PlacedCall.provider_call_id == call_id)
                if row is None or row.charged_at is not None:
                    continue
                status = await provider.get_call_status(call_id)
                if not status:
                    if ensure_aware(row.placed_at) <= now - UNLOGGED_CALL_GIVE_UP:
                        await _mark_charged(ts, call_id, None)
                        given_up += 1
                    continue
                if str(status.get("status") or "").lower() not in _FINISHED:
                    continue   # still ringing or talking: its duration is not final yet
                await _charge_minutes(ts, call_id, status.get("duration_s"), user_id=row.user_id)
                # A call Twilio measured at 0s was never answered: settled, nothing to charge.
                await _mark_charged(ts, call_id, None)
                charged += 1
    return {"charged": charged, "given_up": given_up}


async def handle_expire_trials(payload: dict) -> dict:
    """Periodic driver: resolve trials whose end date has passed.

    A trial with no transition sits in ``trialing`` forever — a live subscription contributing zero
    revenue, which is the product being given away to everyone who ever signed up. Scoped to
    tenants that actually hold a trialing subscription, so the common case is one indexed scan.

    Never raises: one bad tenant must not stop the sweep for the rest.
    """
    from sqlalchemy import distinct, select

    from nexus.billing.lifecycle import run_trial_sweep
    from nexus.models.billing import BillingSubscription

    async with get_sessionmaker()() as session:
        tenant_ids = list(
            (
                await session.scalars(
                    select(distinct(BillingSubscription.tenant_id)).where(
                        BillingSubscription.status == "trialing",
                        BillingSubscription.trial_end.is_not(None),
                    )
                )
            ).all()
        )

    totals = {"tenants": 0, "examined": 0, "converted": 0, "cancelled": 0}
    for tid in tenant_ids:
        try:
            async with tenant_session(tid) as ts:
                res = await run_trial_sweep(ts)
            totals["tenants"] += 1
            for k in ("examined", "converted", "cancelled"):
                totals[k] += res.get(k, 0)
        except Exception:
            logger.warning("trial sweep failed for tenant %s", tid, exc_info=True)
    return totals


async def handle_grant_plan_credits(payload: dict) -> dict:
    """Deliver a paid plan's included credits — the REPLAY path for a grant the webhook lost.

    `billing/webhooks.py::_grant_plan_credits` swallows every failure on purpose, so a Stripe
    retry storm cannot follow a real payment. That leaves the grant undelivered, so the failure is
    parked in `dead_letter_jobs` under this name and an operator replays it from
    `/admin/jobs/dead-letters`. The replay endpoint enqueues `Job(name=..., payload=...)` and the
    worker dispatches by name, which is why this has to be registered rather than merely written.

    Idempotent by construction: `apply_plan_change_credits` keys its grant per tenant/plan/period,
    so replaying a grant that actually succeeded is a no-op rather than a double credit.

    Returns `{"error": ...}` for a terminal problem — an unknown plan, a vanished tenant — rather
    than raising. `dispatch` treats a returned error as a normal outcome and a RAISE as retryable,
    and a dead letter naming a retired plan would otherwise be retried forever.
    """
    from nexus.billing.subscriptions import apply_plan_change_credits
    from nexus.core.tenancy import TenantSession, apply_rls
    from nexus.models.billing import BillingPlan

    tenant_id = str(payload.get("tenant_id") or "")
    plan_id = str(payload.get("plan_id") or "")
    if not tenant_id or not plan_id:
        return {"error": "tenant_id and plan_id are required"}

    async with get_sessionmaker()() as session:
        plan = await session.get(BillingPlan, plan_id)
        if plan is None:
            return {"error": f"unknown plan: {plan_id}"}
        # Bound even though the worker holds a privileged session, for the same reason the webhook
        # does: this function does not choose the role it runs under.
        await apply_rls(session, tenant_id)
        granted = await apply_plan_change_credits(TenantSession(session, tenant_id), plan)
        await session.commit()
    return {"tenant_id": tenant_id, "plan_id": plan_id, "granted": bool(granted)}


async def handle_billing_reconcile(payload: dict) -> dict:
    """Periodic driver: report where our subscription state disagrees with the provider's.

    Webhooks keep the two in step, but a delivery can fail past its retry budget or an endpoint
    can be misconfigured for a window, and those gaps are otherwise invisible until a customer
    complains. Scoped to tenants that actually have a provider-managed subscription, so the
    common case is one indexed scan. Reports only -- see nexus/billing/reconcile.py for why
    repairing automatically would be worse. Never raises.
    """
    from sqlalchemy import distinct, select

    from nexus.billing.reconcile import reconcile_tenant
    from nexus.models.billing import BillingSubscription

    async with get_sessionmaker()() as session:
        tenant_ids = list(
            (
                await session.scalars(
                    select(distinct(BillingSubscription.tenant_id)).where(
                        BillingSubscription.psp_subscription_id.isnot(None)
                    )
                )
            ).all()
        )

    totals = {"tenants": 0, "checked": 0, "drifted": 0}
    for tid in tenant_ids:
        try:
            async with tenant_session(tid) as ts:
                res = await reconcile_tenant(ts)
            totals["tenants"] += 1
            totals["checked"] += res.get("checked", 0)
            totals["drifted"] += res.get("drifted", 0)
        except Exception:
            logger.warning("reconciliation sweep failed for tenant %s", tid, exc_info=True)
    if totals["drifted"]:
        logger.warning("billing reconciliation found %d drifted subscription(s)",
                       totals["drifted"])
    return totals


async def handle_crawl_companies(payload: dict) -> dict:
    """Shared company crawl + fan-out driver.

    The crawl runs whenever there are companies to crawl — it is shadow work, consumed by nobody
    until `shared_company_crawl_enabled` is set, so it is safe to gather data continuously and
    decide later. Fan-out self-disables on that flag.
    """
    from nexus.companies.crawl import crawl_due_companies
    from nexus.companies.fanout import fanout_due_companies

    crawled = await crawl_due_companies(
        limit=payload.get("limit", 20), max_age_hours=payload.get("max_age_hours", 6)
    )
    delivered = await fanout_due_companies(limit=payload.get("limit", 20))
    return {"crawl": crawled, "fanout": delivered}


async def handle_backfill_companies(payload: dict) -> dict:
    """Link unlinked accounts to shared company records. Idempotent, so the heartbeat can enqueue
    it freely; it only ever fills a NULL."""
    from nexus.companies.backfill import backfill_companies

    return await backfill_companies(limit=payload.get("limit", 1000))


async def handle_refresh_mailbox_tokens(payload: dict) -> dict:
    """Refresh each connected SDR mailbox at least once a day (spec §9 mailbox status).

    A revoked grant is otherwise discovered at the moment a campaign tries to send. Refreshing
    marks it ``needs_reauth`` a day earlier, where the SDR sees Reconnect. Microsoft refresh tokens
    also lapse after 90 days unused; a daily refresh keeps an idle mailbox connected.

    The scan reads only ids across tenants (the worker connects as the owner role); each refresh
    runs inside that tenant's own session."""
    from collections import defaultdict

    from sqlalchemy import select

    from nexus.engagement.mailboxes.provider import AuthExpired, ProviderError
    from nexus.engagement.mailboxes.tokens import fresh_access_token, liveness_due, unseal
    from nexus.models.engagement import MailboxConnection

    async with get_sessionmaker()() as session:
        rows = (await session.execute(
            select(MailboxConnection.tenant_id, MailboxConnection.id)
            .where(MailboxConnection.status == "connected")
        )).all()
    by_tenant: dict[str, list[str]] = defaultdict(list)
    for tenant_id, mailbox_id in rows:
        by_tenant[tenant_id].append(mailbox_id)

    refreshed = needs_reauth = failed = 0
    for tenant_id, mailbox_ids in by_tenant.items():
        async with tenant_session(tenant_id) as ts:
            for mailbox_id in mailbox_ids:
                connection = await ts.get(MailboxConnection, mailbox_id)
                if connection is None or connection.status != "connected":
                    continue
                if not liveness_due(unseal(connection.tokens)):
                    continue
                try:
                    await fresh_access_token(ts, connection, force=True)
                    refreshed += 1
                except AuthExpired:
                    needs_reauth += 1
                except ProviderError:
                    failed += 1
    return {"refreshed": refreshed, "needs_reauth": needs_reauth, "failed": failed}


async def handle_ship_ledger(payload: dict) -> dict:
    """Ship consented events to the archive, then drop what has been archived for a week.

    Raises when a store refused, so the batch retries and finally dead-letters with its evidence.
    Rows carry their own backoff, so the ticks in between find nothing due and return quietly rather
    than dead-lettering once a minute for the length of an outage (spec §18.2)."""
    from nexus.engagement.ledger import shipper

    result = await shipper.ship()
    if not result.get("skipped"):
        result["purged"] = await shipper.purge_shipped()
    return result


async def handle_build_ledger_datasets(payload: dict) -> dict:
    """Turn newly archived events into training examples and insights facts, hourly.

    Self-limiting on the watermark's own `built_at`, so the heartbeat can enqueue it every tick: the
    worker is not the only process that could run this, and an hour kept in a module variable would
    be wrong in the second replica."""
    from datetime import timedelta

    from nexus.core.db import utcnow
    from nexus.engagement.ledger import builder
    from nexus.engagement.ledger.stores import StoreNotConfigured

    try:
        last = await builder.last_built_at()
    except StoreNotConfigured:
        return {"skipped": "the training store is not configured"}
    except Exception:
        last = None
    if last is not None and utcnow() - last < timedelta(hours=1):
        return {"skipped": "built less than an hour ago"}
    return await builder.build()


async def handle_ledger_delete_workspace(payload: dict) -> dict:
    """A workspace switched training off: remove what it already contributed (spec §18.6)."""
    from nexus.engagement.ledger import deletion

    tenant_id = payload.get("tenant_id") or ""
    if not tenant_id:
        return {"error": "no tenant"}
    report = await deletion.delete_workspace(tenant_id)
    async with tenant_session(tenant_id) as ts:
        from nexus.core.audit import record_audit

        await record_audit(ts, "ledger.workspace_deleted", target_type="tenant",
                           target_id=tenant_id, meta=report)
    return report


async def handle_ledger_erase_person(payload: dict) -> dict:
    """Erase one person everywhere, by key. Ships the outbox first so an event recorded seconds
    before the request cannot land in the archive seconds after the erasure."""
    from nexus.engagement.ledger import deletion, shipper

    person_key = payload.get("person_key") or ""
    if not person_key:
        return {"error": "no person key"}
    try:
        await shipper.ship()
    except Exception:
        logger.warning("could not flush the outbox before an erasure", exc_info=True)
    return await deletion.erase_person(person_key)


async def handle_advance_engagement(payload: dict) -> dict:
    """Send what is due in running engagement campaigns (spec §8). Does nothing while the engine
    is dark: the switch is re-read here, not only when the heartbeat enqueues."""
    from nexus.engagement import config
    from nexus.engagement.sequences.advance import advance

    if not config.campaigns_enabled():
        return {"skipped": "engagement campaigns are switched off"}
    return await advance()


async def handle_remind_replies(payload: dict) -> dict:
    """Nudge SDRs whose interested buyers are still waiting (spec §19). Reads every workspace that
    has an open reply, so it runs on the platform sessionmaker to find them and per tenant to act."""
    from sqlalchemy import select

    from nexus.core.db import get_platform_sessionmaker, utcnow
    from nexus.engagement import config
    from nexus.engagement.desk.service import remind_unanswered
    from nexus.models.engagement import ReplyClassification

    if not config.campaigns_enabled():
        return {"skipped": "engagement campaigns are switched off"}
    now = utcnow()
    async with get_platform_sessionmaker()() as session:
        tenant_ids = (await session.execute(
            select(ReplyClassification.tenant_id)
            .where(ReplyClassification.status == "open")
            .where(ReplyClassification.reminded_at.is_(None))
            .distinct().limit(500))).scalars().all()
    reminded = 0
    for tenant_id in tenant_ids:
        try:
            async with tenant_session(tenant_id) as ts:
                reminded += await remind_unanswered(ts, now=now)
        except Exception:  # one workspace's reminder must not stop the rest
            logger.warning("could not remind %s about waiting replies", tenant_id, exc_info=True)
    return {"tenants": len(tenant_ids), "reminded": reminded}


async def handle_sync_mailbox(payload: dict) -> dict:
    """Read what arrived in one mailbox (a notification said something changed)."""
    from nexus.engagement import config
    from nexus.engagement.replies.ingest import sync_mailbox
    from nexus.models.engagement import MailboxConnection

    if not config.campaigns_enabled():
        return {"skipped": "engagement campaigns are switched off"}
    tenant_id, mailbox_id = payload.get("tenant_id"), payload.get("mailbox_id")
    if not tenant_id or not mailbox_id:
        return {"error": "tenant_id and mailbox_id are required"}
    async with tenant_session(tenant_id) as ts:
        mailbox = await ts.get(MailboxConnection, mailbox_id)
        if mailbox is None:
            return {"error": "mailbox_not_found"}
        return await sync_mailbox(ts, mailbox)


async def handle_sync_mailboxes(payload: dict) -> dict:
    """The fallback poll (spec §6): every connected mailbox not read in the last few minutes,
    and notification subscriptions renewed a day before they lapse."""
    from datetime import timedelta

    from sqlalchemy import or_, select

    from nexus.core.db import get_platform_sessionmaker, utcnow
    from nexus.engagement import config
    from nexus.engagement.replies.ingest import sync_mailbox
    from nexus.engagement.replies.notifications import renew, renewal_due
    from nexus.models.engagement import MailboxConnection

    if not config.campaigns_enabled():
        return {"skipped": "engagement campaigns are switched off"}
    now = utcnow()
    stale = now - timedelta(minutes=5)
    async with get_platform_sessionmaker()() as session:
        rows = (await session.execute(
            select(MailboxConnection.tenant_id, MailboxConnection.id)
            .where(MailboxConnection.status == "connected")
            .where(or_(MailboxConnection.last_synced_at.is_(None),
                       MailboxConnection.last_synced_at <= stale))
            .limit(200))).all()
    synced = renewed = 0
    for tenant_id, mailbox_id in rows:
        try:
            async with tenant_session(tenant_id) as ts:
                mailbox = await ts.get(MailboxConnection, mailbox_id)
                if mailbox is None:
                    continue
                if renewal_due(mailbox, now) and await renew(ts, mailbox, now=now) == "renewed":
                    renewed += 1
                await sync_mailbox(ts, mailbox, now=now)
                synced += 1
        except Exception:
            logger.warning("mailbox %s could not be synced", mailbox_id, exc_info=True)
    return {"synced": synced, "renewed": renewed}


async def handle_log_engagement_crm(payload: dict) -> dict:
    """Write sent emails, replies and booked meetings to each workspace's CRM (spec §19).

    Behind the same two switches as every other CRM write: the deployment's `crm_sync_enabled`
    ("Push to CRM") and the workspace's `automation_enabled`. Dark with the engagement engine. The
    connector is resolved once per workspace, inside that workspace's session, for the reason
    `handle_sync_crm_due_accounts` gives: resolving once for the sweep sent every tenant's writes
    to whichever CRM the deployment named.
    """
    from sqlalchemy import select

    from nexus.core.config import get_settings
    from nexus.core.db import get_platform_sessionmaker, utcnow
    from nexus.engagement import config
    from nexus.engagement.enhancements.crm_log import LOOKBACK, log_pending
    from nexus.ingestion import crm_credentials
    from nexus.models.engagement import EngagementMessage, ReplyClassification
    from nexus.models.identity import Tenant

    if not get_settings().crm_sync_enabled:
        return {"skipped": "crm_sync_disabled"}
    if not config.campaigns_enabled():
        return {"skipped": "engagement campaigns are switched off"}
    now = utcnow()
    since = now - LOOKBACK
    async with get_platform_sessionmaker()() as session:
        opted_in = select(Tenant.id).where(Tenant.automation_enabled == True)  # noqa: E712
        tenants = set((await session.execute(
            select(EngagementMessage.tenant_id).distinct()
            .where(EngagementMessage.tenant_id.in_(opted_in))
            .where(EngagementMessage.crm_logged_at.is_(None))
            .where((EngagementMessage.sent_at >= since) | (EngagementMessage.received_at >= since))
        )).scalars().all())
        tenants |= set((await session.execute(
            select(ReplyClassification.tenant_id).distinct()
            .where(ReplyClassification.tenant_id.in_(opted_in))
            .where(ReplyClassification.decision == "meeting")
            .where(ReplyClassification.crm_logged_at.is_(None))
            .where(ReplyClassification.decided_at >= since))).scalars().all())
    totals = {"tenants": 0, "logged": 0, "waiting": 0, "failed": 0}
    for tenant_id in sorted(tenants):
        try:
            async with tenant_session(tenant_id) as ts:
                connector = await crm_credentials.resolve_crm_connector(ts)
                result = await log_pending(ts, connector, now=now)
        except Exception:
            logger.warning("CRM activity log failed for tenant %s", tenant_id, exc_info=True)
            continue
        totals["tenants"] += 1
        for key in ("logged", "waiting", "failed"):
            totals[key] += result[key]
    return totals


async def enqueue_log_engagement_crm(*, queue: TaskQueue | None = None) -> None:
    queue = queue or get_task_queue()
    await queue.enqueue(Job(name="log_engagement_crm", payload={}))


async def enqueue_sync_mailbox(tenant_id: str, mailbox_id: str, *,
                              queue: TaskQueue | None = None) -> None:
    queue = queue or get_task_queue()
    await queue.enqueue(Job(name="sync_mailbox",
                            payload={"tenant_id": tenant_id, "mailbox_id": mailbox_id}))


async def enqueue_sync_mailboxes(*, queue: TaskQueue | None = None) -> None:
    queue = queue or get_task_queue()
    await queue.enqueue(Job(name="sync_mailboxes", payload={}))


async def enqueue_advance_engagement(*, queue: TaskQueue | None = None) -> None:
    queue = queue or get_task_queue()
    await queue.enqueue(Job(name="advance_engagement", payload={}))


async def enqueue_remind_replies(*, queue: TaskQueue | None = None) -> None:
    queue = queue or get_task_queue()
    await queue.enqueue(Job(name="remind_replies", payload={}))


async def enqueue_ship_ledger(*, queue: TaskQueue | None = None) -> None:
    queue = queue or get_task_queue()
    await queue.enqueue(Job(name="ship_ledger", payload={}))


async def enqueue_build_ledger_datasets(*, queue: TaskQueue | None = None) -> None:
    queue = queue or get_task_queue()
    await queue.enqueue(Job(name="build_ledger_datasets", payload={}))


async def enqueue_ledger_delete_workspace(tenant_id: str, *,
                                          queue: TaskQueue | None = None) -> None:
    queue = queue or get_task_queue()
    await queue.enqueue(Job(name="ledger_delete_workspace", payload={"tenant_id": tenant_id}))


async def enqueue_ledger_erase_person(person_key: str, *,
                                      queue: TaskQueue | None = None) -> None:
    queue = queue or get_task_queue()
    await queue.enqueue(Job(name="ledger_erase_person", payload={"person_key": person_key}))


async def enqueue_refresh_mailbox_tokens(*, queue: TaskQueue | None = None) -> None:
    queue = queue or get_task_queue()
    await queue.enqueue(Job(name="refresh_mailbox_tokens", payload={}))


async def enqueue_crawl_companies(*, queue: TaskQueue | None = None) -> None:
    queue = queue or get_task_queue()
    await queue.enqueue(Job(name="crawl_companies", payload={}))


async def enqueue_backfill_companies(*, queue: TaskQueue | None = None) -> None:
    queue = queue or get_task_queue()
    await queue.enqueue(Job(name="backfill_companies", payload={}))


HANDLERS: dict[str, Handler] = {
    "process_account": handle_process_account,
    "run_orchestration": handle_run_orchestration,
    "refresh_due_accounts": handle_refresh_due_accounts,
    "sync_crm_account": handle_sync_crm_account,
    "sync_crm_due_accounts": handle_sync_crm_due_accounts,
    "send_daily_digests": handle_send_daily_digests,
    "alert_digests": handle_alert_digests,
    "discover_icp_accounts": handle_discover_icp_accounts,
    "populate_accounts": handle_populate_accounts,
    "crawl_companies": handle_crawl_companies,
    "backfill_companies": handle_backfill_companies,
    "sync_network_account": handle_sync_network_account,
    "rollup_usage": handle_rollup_usage,
    "prune_web_cache": handle_prune_web_cache,
    "roll_billing_periods": handle_roll_billing_periods,
    "dunning_sweep": handle_dunning_sweep,
    "billing_reconcile": handle_billing_reconcile,
    # Replay target for a credit grant the Stripe webhook could not deliver. Registered so
    # the dead-letter replay button is not a no-op; never enqueued on a schedule.
    "billing_grant_plan_credits": handle_grant_plan_credits,
    "expire_trials": handle_expire_trials,
    "refresh_mailbox_tokens": handle_refresh_mailbox_tokens,
    "ship_ledger": handle_ship_ledger,
    "advance_engagement": handle_advance_engagement,
    "sync_mailbox": handle_sync_mailbox,
    "sync_mailboxes": handle_sync_mailboxes,
    "log_engagement_crm": handle_log_engagement_crm,
    "remind_replies": handle_remind_replies,
    "build_ledger_datasets": handle_build_ledger_datasets,
    "ledger_delete_workspace": handle_ledger_delete_workspace,
    "ledger_erase_person": handle_ledger_erase_person,
    "charge_unlogged_calls": handle_charge_unlogged_calls,
}


async def enqueue_process_account(
    tenant_id: str, account_id: str, *, scheduled: bool = False, queue: TaskQueue | None = None
) -> None:
    """``scheduled`` marks the refresh sweep's jobs — the daily scan — which enrich without web
    search. Omitted (a person added the account, or a job serialized before the flag existed)
    means today's behaviour."""
    queue = queue or get_task_queue()
    payload = {"tenant_id": tenant_id, "account_id": account_id}
    if scheduled:
        payload["scheduled"] = True
    await queue.enqueue(Job(name="process_account", payload=payload))


async def enqueue_run_orchestration(
    tenant_id: str, run_id: str, *, queue: TaskQueue | None = None
) -> None:
    queue = queue or get_task_queue()
    await queue.enqueue(
        Job(name="run_orchestration", payload={"tenant_id": tenant_id, "run_id": run_id})
    )


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


async def enqueue_alert_digests(*, queue: TaskQueue | None = None) -> None:
    queue = queue or get_task_queue()
    await queue.enqueue(Job(name="alert_digests", payload={}))


async def enqueue_send_daily_digests(*, queue: TaskQueue | None = None) -> None:
    queue = queue or get_task_queue()
    await queue.enqueue(Job(name="send_daily_digests", payload={}))


async def enqueue_populate_accounts(
    tenant_id: str, run_id: str, *, queue: TaskQueue | None = None
) -> None:
    queue = queue or get_task_queue()
    await queue.enqueue(
        Job(name="populate_accounts", payload={"tenant_id": tenant_id, "run_id": run_id})
    )


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


async def enqueue_prune_web_cache(*, queue: TaskQueue | None = None) -> None:
    queue = queue or get_task_queue()
    await queue.enqueue(Job(name="prune_web_cache", payload={}))


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


async def enqueue_billing_reconcile(*, queue: TaskQueue | None = None) -> None:
    queue = queue or get_task_queue()
    await queue.enqueue(Job(name="billing_reconcile", payload={}))


async def enqueue_expire_trials(*, queue: TaskQueue | None = None) -> None:
    queue = queue or get_task_queue()
    await queue.enqueue(Job(name="expire_trials", payload={}))


async def enqueue_charge_unlogged_calls(*, queue: TaskQueue | None = None) -> None:
    queue = queue or get_task_queue()
    await queue.enqueue(Job(name="charge_unlogged_calls", payload={}))


async def dispatch(job: Job) -> dict:
    """Route one job to its handler.

    On success the handler's own dict is returned untouched — that contract predates M11 and
    callers rely on it. On a raised exception the failure is *surfaced* (it used to be swallowed
    here, which is how failed jobs went missing) as the same ``{"error": ...}`` shape plus
    ``JOB_FAILED_KEY``, so the worker loop can retry or dead-letter it.
    """
    # Pick up any runtime setting an operator changed in the Control plane. The worker is a
    # separate process with its own `Settings` singleton, so nothing the API does reaches it
    # otherwise. TTL-guarded, so this is a clock comparison on all but one call in thirty seconds,
    # and it never raises — a config read that fails must not stop a job from running.
    try:
        from nexus.runtime_config.service import refresh_if_stale

        await refresh_if_stale()
    except Exception:
        logger.debug("runtime config refresh skipped", exc_info=True)

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
        await _record_job_failure(job, exc)
        return {"error": f"{type(exc).__name__}: {exc}", JOB_FAILED_KEY: True}


async def _record_job_failure(job: Job, exc: Exception) -> None:
    """A ledger ``error.job_failed`` for a tenant job that raised. Never raises: the retry and
    dead-letter path must run whatever happens here."""
    tenant_id = (job.payload or {}).get("tenant_id")
    if not tenant_id:
        return
    try:
        from nexus.engagement.ledger.emit import emit

        async with tenant_session(tenant_id) as ts:
            await emit(ts, "error.job_failed",
                       payload={"job": job.name, "attempt": job.attempts + 1,
                                "max_attempts": job.max_attempts,
                                "error": f"{type(exc).__name__}: {exc}"[:500]})
    except Exception:
        logger.debug("could not record job failure in the ledger", exc_info=True)
