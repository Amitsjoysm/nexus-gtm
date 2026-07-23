"""Account intelligence loop: ingest signals → score → inbox → plays.

This is the orchestration seam wired by the API (synchronously) and the worker (async job).
"""
from __future__ import annotations

from nexus.agents.runtime import AgentRuntime, get_agent_runtime
from nexus.core.config import get_settings
from nexus.core.events import Event, get_event_bus
from nexus.core.tenancy import TenantSession
from nexus.inbox.service import get_inbox_service
from nexus.ingestion.service import get_ingestion_service
from nexus.models.account import Account
from nexus.models.workflow import Play
from nexus.plays.engine import get_plays_engine


async def process_account(
    ts: TenantSession, account: Account, *, runtime: AgentRuntime | None = None
) -> dict:
    runtime = runtime or get_agent_runtime()

    new_signals = await get_ingestion_service().run_sources(ts, account)

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
    }
