"""Is the self-hosted fetcher answering? Mirrors `nexus/verification/health.py`.

A dead fetcher fails soft — every search falls back to the paid provider — so the only symptom is a
bigger bill. One check, used two ways: the `web fetcher` row on Platform health, and a WARNING in
the API and worker boot logs, scheduled in the background so a slow fetcher never delays startup.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

OK, UNCONFIGURED, ERROR = "ok", "unconfigured", "error"

# Background tasks are held here so they are not garbage-collected mid-flight.
_background: set[asyncio.Task] = set()


@dataclass(frozen=True)
class FetchCheck:
    status: str      # ok | unconfigured | error
    detail: str
    url: str = ""


async def check_fetch_service() -> FetchCheck:
    """Never raises: a health check that crashes reports nothing, which reads as all clear."""
    from nexus.fetching.client import get_fetch_client

    try:
        client = get_fetch_client()
        if not client.configured:
            return FetchCheck(
                UNCONFIGURED,
                "no fetch service URL or token; every signal search goes to the paid provider",
            )
        payload = await client.health()
    except Exception as exc:
        return FetchCheck(ERROR, f"could not run the check ({type(exc).__name__})")
    if payload is None:
        return FetchCheck(ERROR, client.last_failure or "unreachable", client.base_url)
    if not payload.get("ok"):
        return FetchCheck(ERROR, "service reported not ok", client.base_url)
    tier = "browser tier available" if payload.get("browser") else "HTTP tier only"
    return FetchCheck(OK, tier, client.base_url)


async def warn_if_fetcher_unreachable(logger: logging.Logger | None = None) -> FetchCheck | None:
    """Log the check where an operator reading a boot log will see it. Never raises.

    Quiet when unconfigured: not running the fetcher is a supported deployment, not a fault.
    """
    log = logger or logging.getLogger("nexus.fetching.health")
    try:
        check = await check_fetch_service()
    except Exception:
        log.warning("could not check the web fetcher", exc_info=True)
        return None
    if check.status == ERROR:
        log.warning("WEB FETCHER UNREACHABLE: %s (url=%s) — signal searches fall back to the paid "
                    "provider until it answers", check.detail, check.url)
    elif check.status == OK:
        log.info("web fetcher ok: %s", check.detail)
    return check


def schedule_fetcher_warning(logger: logging.Logger | None = None) -> asyncio.Task:
    """Run the startup check in the background: a slow or dead fetcher must never delay boot."""
    task = asyncio.get_running_loop().create_task(
        warn_if_fetcher_unreachable(logger), name="nexus-web-fetcher-check",
    )
    _background.add(task)
    task.add_done_callback(_background.discard)
    return task
