"""Expired rows must actually leave, or the table grows forever and the saving turns into storage."""
from __future__ import annotations

from nexus.fetching import cache
from nexus.workers.tasks import HANDLERS, handle_prune_web_cache

HITS = [{"title": "t", "url": "https://x.test/a", "snippet": "", "source": "t"}]


async def test_the_job_deletes_expired_rows_only():
    await cache.put("search", "fresh", engine="t", limit=4, payload=HITS, ttl_s=3600)
    await cache.put("search", "stale", engine="t", limit=4, payload=HITS, ttl_s=-1)

    result = await handle_prune_web_cache({})

    assert result == {"pruned": 1}
    assert await cache.get("search", "fresh", engine="t", limit=4) == HITS


def test_the_job_is_routable():
    # An unregistered name is dropped by `dispatch` with "no handler for job", silently.
    assert HANDLERS["prune_web_cache"] is handle_prune_web_cache


async def test_the_heartbeat_enqueues_it_whatever_the_automation_switch_says(monkeypatch):
    # Housekeeping for a platform-global table is not a tenant opt-in: a deployment with automation
    # off still fills the cache from manual refreshes, and would never empty it.
    from nexus.core.config import get_settings
    from nexus.workers import scheduler

    monkeypatch.setattr(get_settings(), "automation_enabled", False)
    names: list[str] = []

    class Recorder:
        async def enqueue(self, job):
            names.append(job.name)

    await scheduler._enqueue_due(Recorder())

    assert "prune_web_cache" in names
