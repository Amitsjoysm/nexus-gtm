"""Account intelligence loop: ingest signals → score → inbox → plays.

This is the orchestration seam wired by the API (synchronously) and the worker (async job).
"""
from __future__ import annotations

import logging

from nexus.agents.runtime import AgentRuntime, get_agent_runtime
from nexus.core.config import get_settings
from nexus.core.events import Event, get_event_bus
from nexus.core.tenancy import TenantSession
from nexus.inbox.service import get_inbox_service
from nexus.ingestion.service import get_ingestion_service
from nexus.models.account import Account
from nexus.models.workflow import Play
from nexus.plays.engine import get_plays_engine

logger = logging.getLogger("nexus.pipeline")


async def _covered_by_shared_crawl(account) -> bool:
    """Whether the shared company crawl already covers this account's signals.

    Reads the shared table through the platform sessionmaker: ``companies`` carries no
    ``tenant_id``, so a TenantSession would be the wrong scope and, under RLS, would silently see
    nothing — which here would read as "not covered" and quietly undo the saving.

    Never raises. A lookup failure falls back to crawling per-tenant, which costs money rather than
    losing signals; the opposite default would drop a customer's coverage on a database blip.
    """
    if not get_settings().shared_company_crawl_enabled:
        return False
    company_id = getattr(account, "company_id", None)
    if not company_id:
        return False
    try:
        from nexus.core.db import get_platform_sessionmaker
        from nexus.models.company import Company

        async with get_platform_sessionmaker()() as session:
            company = await session.get(Company, company_id)
            if company is None or company.last_crawled_at is None:
                # Linked but never crawled: backfill runs long before the crawler reaches a
                # company, and skipping on the link alone would black the account out until it
                # caught up.
                return False
            # Same condition fan-out uses. These two MUST agree: gate delivery on a verdict but
            # not the skip, and an unproven company gets neither crawl — signals stop, and it
            # looks exactly like a quiet market.
            return getattr(company, "crawl_verdict", "unknown") == "agrees"
    except Exception:
        logger.warning(
            "shared-crawl coverage check failed for account %s; crawling per-tenant",
            getattr(account, "id", "?"), exc_info=True,
        )
        return False


async def process_account(
    ts: TenantSession, account: Account, *, runtime: AgentRuntime | None = None
) -> dict:
    runtime = runtime or get_agent_runtime()

    # The shared company crawl only saves money if it REPLACES this crawl rather than adding to it.
    # Until it did, forty workspaces tracking Stripe crawled it forty-one times: forty per-tenant
    # crawls plus the shared one.
    #
    # Three conditions, all required, because the failure mode of being wrong here is an account
    # that quietly stops receiving signals — which looks exactly like a quiet market:
    #   * the flag is on (default off; the shadow period must change nothing),
    #   * the account is linked to a shared company, and
    #   * that company has actually been crawled at least once. Backfill links accounts long
    #     before the crawler reaches them, so skipping on the link alone would black out every
    #     newly-linked account until the shared crawl caught up.
    # Fan-out (nexus/companies/fanout.py) then delivers the signals through IngestionService.ingest,
    # so per-tenant dedupe, `signal.created` and same-transaction alerting all still apply.
    shared = await _covered_by_shared_crawl(account)
    new_signals = [] if shared else await get_ingestion_service().run_sources(ts, account)

    # Firmographic enrichment from the web (only when enabled, and only for accounts still
    # missing the basics) — so scoring and the UI see real industry/size/tech instead of blanks.
    # Never breaks the pipeline: the enricher isolates its own failures and returns [].
    if get_settings().account_enrich_enabled and (
        account.industry is None or account.employee_count is None
    ):
        from nexus.enrichment.account import get_account_enricher

        filled = await get_account_enricher().enrich(account)
        if filled:
            await ts.flush()

    # Post-enrichment ICP re-screen: an auto-discovered account whose crawled headcount is now
    # definitively outside the ICP band is archived (not deleted) and skips scoring/inbox/plays —
    # the SDR's daily list must only ever show strict ICP matches. No-op for manual/CRM accounts.
    from nexus.discovery.auto import rescreen_discovered_account

    if await rescreen_discovered_account(ts, account):
        return {
            "account_id": account.id,
            "new_signals": len(new_signals),
            "composite_score": None,
            "scoring_status": "skipped",
            "inbox_tasks_created": [],
            "plays_executed": [],
            "icp_screened": True,
            "signals_source": "shared_company" if shared else "tenant",
        }

    score = await runtime.run("scoring", ts, account_id=account.id, persist=True)
    composite = score.output.get("composite")

    inbox = get_inbox_service()
    plays = get_plays_engine()
    # Load enabled plays ONCE for the whole batch instead of re-querying per signal
    # (the per-signal loop below would otherwise re-read the plays table N times).
    enabled_plays = await ts.list(Play, Play.enabled == True)  # noqa: E712
    # Only meaningful signals become Inbox tasks; weak ones (a generic press mention) still feed
    # the timeline and Plays but don't clutter the rep's daily task list.
    min_strength = get_settings().inbox_min_signal_strength
    task_ids: list[str] = []
    play_run_ids: list[str] = []
    for sig in new_signals:
        if sig.strength >= min_strength:
            task = await inbox.create_from_signal(ts, sig, account, composite_score=composite)
            task_ids.append(task.id)
        for run in await plays.evaluate(
            ts, account=account, signal=sig, composite=composite, plays=enabled_plays
        ):
            play_run_ids.append(run.id)

    # Generic domain event: the account has been (re)scored and its state may have changed.
    # Subscribers (e.g. the CRM auto-sync fast-path) react off-request; publish is a no-op when
    # no one is listening, so this is free when the feature is off.
    await get_event_bus().publish(
        Event(
            name="account.scored",
            tenant_id=ts.tenant_id,
            payload={
                "account_id": account.id,
                "composite_score": composite,
                "new_signals": len(new_signals),
            },
        )
    )

    return {
        "account_id": account.id,
        "new_signals": len(new_signals),
        "composite_score": composite,
        "scoring_status": score.status,
        "inbox_tasks_created": task_ids,
        "plays_executed": play_run_ids,
        # Which crawl fed this run. Without it "0 new signals" means both "nothing happened at this
        # company" and "the shared crawler owns this account", which are opposite problems.
        "signals_source": "shared_company" if shared else "tenant",
    }
