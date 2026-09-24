"""Read person and company profiles from the insights store (spec §18.3, §18.5). Read-only.

**Never raises and never blocks a screen.** An unconfigured store, an unreachable one, or a query
that fails all answer "no profile": insights decorate the screens, and a review queue must not fail
because a store in another cloud is slow.

**The workspace keys are not read.** The profile rows carry them so the builder can count distinct
workspaces; the display rules need only the count, so the key list never enters this process.

**A short cache, misses included.** Screens ask about the same people repeatedly (a review queue is
re-read after every approval); five minutes is well inside how often a profile can change, since
the builder runs on the worker's schedule. An unknown person is cached as unknown, or every render
of a list of new prospects would query the store for each of them.
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger("nexus.engagement.insights.client")

TTL_S = 300
MAX_BATCH = 200
MAX_CACHED = 5000

_PERSON_COLUMNS = ("person_email, company_domain, best_weekday, best_hour, median_response_s, "
                   "reply_propensity, last_reply_band, workspace_count")
_COMPANY_COLUMNS = ("company_domain, best_weekday, best_hour, median_response_s, reply_propensity, "
                    "workspace_count")

_cache: dict[tuple[str, str], tuple[float, dict | None]] = {}


def clear_cache() -> None:
    _cache.clear()


def _normal(value: str) -> str:
    return (value or "").strip().lower()


async def _read(kind: str, keys: list[str]) -> dict[str, dict]:
    from nexus.engagement.ledger import stores

    table, key, columns = (("person_profiles", "person_email", _PERSON_COLUMNS) if kind == "person"
                           else ("company_profiles", "company_domain", _COMPANY_COLUMNS))
    try:
        async with stores.connect("insights") as conn:
            rows = await conn.fetch(
                f"SELECT {columns} FROM nexus_ledger.{table} WHERE {key} = ANY($1::text[])", keys)
    except stores.StoreNotConfigured:
        return {}
    except Exception:
        logger.warning("could not read %s profiles from the insights store", kind, exc_info=True)
        return {}
    return {row[key]: dict(row) for row in rows}


async def _profiles(kind: str, values: list[str]) -> dict[str, dict]:
    now = time.monotonic()
    wanted = sorted({_normal(v) for v in values if _normal(v)})[:MAX_BATCH]
    found: dict[str, dict] = {}
    missing: list[str] = []
    for value in wanted:
        hit = _cache.get((kind, value))
        if hit and hit[0] > now:
            if hit[1] is not None:
                found[value] = hit[1]
        else:
            missing.append(value)
    if missing:
        fresh = await _read(kind, missing)
        if len(_cache) > MAX_CACHED:
            _cache.clear()
        for value in missing:
            _cache[(kind, value)] = (now + TTL_S, fresh.get(value))
            if value in fresh:
                found[value] = fresh[value]
    return found


async def person_profiles(emails: list[str]) -> dict[str, dict]:
    """``{normalised email: profile}`` for the people the store knows."""
    return await _profiles("person", emails)


async def company_profiles(domains: list[str]) -> dict[str, dict]:
    return await _profiles("company", domains)
