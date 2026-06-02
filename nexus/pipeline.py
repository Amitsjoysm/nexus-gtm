"""Account intelligence loop: ingest signals → score → inbox → plays.

This is the orchestration seam wired by the API (synchronously) and the worker (async job).
"""
from __future__ import annotations

from nexus.agents.runtime import AgentRuntime, get_agent_runtime
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

    score = await runtime.run("scoring", ts, account_id=account.id, persist=True)
    composite = score.output.get("composite")

    inbox = get_inbox_service()
    plays = get_plays_engine()
    # Load enabled plays ONCE for the whole batch instead of re-querying per signal
    # (the per-signal loop below would otherwise re-read the plays table N times).
    enabled_plays = await ts.list(Play, Play.enabled == True)  # noqa: E712
    task_ids: list[str] = []
    play_run_ids: list[str] = []
    for sig in new_signals:
        task = await inbox.create_from_signal(ts, sig, account, composite_score=composite)
        task_ids.append(task.id)
        for run in await plays.evaluate(
            ts, account=account, signal=sig, composite=composite, plays=enabled_plays
        ):
            play_run_ids.append(run.id)

    return {
        "account_id": account.id,
        "new_signals": len(new_signals),
        "composite_score": composite,
        "scoring_status": score.status,
        "inbox_tasks_created": task_ids,
        "plays_executed": play_run_ids,
    }
