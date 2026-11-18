"""Fetch one page through the shared cache, and record what browsing cost us.

``fetch_page`` is the only path from a Prospector screen to the provider: cache first, then the
client, then the cache again with the answer. A failed call raises ``ProspectorError`` with the
status and sentence the router returns, and is never stored, because a cached failure would freeze
an outage for the whole TTL (the ``last_failure`` rule of ``integrations/search/engines.py``).

``record_browse_cost`` is separate on purpose. It writes in the TENANT transaction, and that write
must come after every cache write in the request (see ``cache.py``), so a Net new scan fetches all
its pages first and records once at the end.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from nexus.prospector import cache
from nexus.prospector.client import InfoJoyClient, SearchOutcome
from nexus.prospector.errors import ProspectorError
from nexus.prospector.records import ParsedPage

logger = logging.getLogger("nexus.prospector.fetch")

#: Browsing COGS: recorded, never charged (catalogued shadow; browsing is free, D3).
BROWSE_CAPABILITY = "prospector.search_rows"

_STATUS = {
    "not_configured": 400,
    "invalid": 422,
    "rejected": 502,
    "bad_response": 502,
    "busy": 503,
    "unavailable": 503,
}


@dataclass(slots=True)
class Fetched:
    parsed: ParsedPage
    cached: bool
    #: Rows bought from the provider by THIS call (0 on a cache hit).
    vendor_rows: int = 0


def _raise(outcome: SearchOutcome) -> None:
    status = _STATUS.get(outcome.status, 503)
    headers = {"Retry-After": "5"} if outcome.status == "busy" else None
    raise ProspectorError(status, outcome.detail or "The data provider could not answer.",
                          headers=headers)


def split_payload(view: str, payload: dict) -> list[dict] | None:
    """One payload per value of a list the provider does not OR, or ``None`` when none applies.

    Measured 2026-11-18 on the live API: ``Titles: [CEO, CTO]`` returned the same 60 rows as
    ``[CTO]``, in either order, and ``[CFO, CEO, CTO]`` no CEO at all, so "CEO or CTO" showed
    CTOs only. Every other filter is sent as it is, unchanged in each part (AND across sections).
    """
    from nexus.prospector.fields import split_filters

    for name in split_filters(view):
        values = payload.get(name)
        if isinstance(values, list) and len(values) > 1:
            return [{**payload, name: [value]} for value in values]
    return None


def _identity(item: dict) -> str:
    """A row's identity for de-duplicating merged parts: its email, else name at its company."""
    lowered = {str(k).lower(): v for k, v in item.items()}
    email = str(lowered.get("email") or "").strip().lower()
    if email:
        return f"e:{email}"
    return "n:" + "|".join(str(lowered.get(k) or "").strip().lower()
                           for k in ("firstname", "lastname", "companyurl", "companyname"))


def _interleave(parts: list[list[dict]]) -> list[dict]:
    """One row from each part in turn, so CEOs and CTOs share the page, each person once (a
    "CEO & CTO" comes back from both parts)."""
    out: list[dict] = []
    seen: set[str] = set()
    for index in range(max((len(p) for p in parts), default=0)):
        for part in parts:
            if index < len(part):
                key = _identity(part[index])
                if key not in seen:
                    seen.add(key)
                    out.append(part[index])
    return out


async def _fetch_split(view: str, parts: list[dict], *, page: int, size: int,
                       client: InfoJoyClient | None, ts) -> Fetched:
    """Page ``page`` of each part at ``ceil(size / parts)`` rows, merged.

    Rows bought stay what one search of ``size`` would buy. Each part is its own cache entry, so a
    selection made from the merged page resolves to the same rows (``selection.py`` calls this
    same function). Parts are asked concurrently in one connection release: the TenantSession
    itself is never used by two coroutines.
    """
    import asyncio

    from nexus.core.tenancy import connection_released

    part_size = -(-size // len(parts))
    keys = [cache.page_key(view, part, page, part_size) for part in parts]
    pages: list[ParsedPage | None] = [await cache.get_page(k) for k in keys]
    missing = [i for i, hit in enumerate(pages) if hit is None]
    bought = 0
    if missing:
        provider = client or InfoJoyClient()

        async def ask(i: int) -> SearchOutcome:
            return await provider.search(view, parts[i], page=page, size=part_size, count=False)

        if ts is None:
            outcomes = await asyncio.gather(*(ask(i) for i in missing))
        else:
            async with connection_released(ts):
                outcomes = await asyncio.gather(*(ask(i) for i in missing))
        failed: SearchOutcome | None = None
        for i, outcome in zip(missing, outcomes):
            if not outcome.ok or outcome.page is None:
                failed = failed or outcome
                continue
            # A part that answered is kept even when another failed: the retry then buys only
            # the part that failed.
            await cache.put_page(keys[i], view=view, page=page, size=part_size,
                                 parsed=outcome.page)
            pages[i] = outcome.page
            bought += outcome.vendor_rows
        if failed is not None:
            logger.info("prospector split fetch refused: %s", failed.status)
            _raise(failed)
    answered = [p for p in pages if p is not None]
    merged = ParsedPage(items=_interleave([p.items for p in answered]),
                        more=any(len(p.items) >= part_size for p in answered))
    return Fetched(parsed=merged, cached=not missing, vendor_rows=bought)


async def fetch_page(view: str, payload: dict, *, page: int, size: int, count: bool = False,
                     client: InfoJoyClient | None = None, ts=None) -> Fetched:
    """One page, from the cache or the provider.

    ``ts``: the request's TenantSession, whose database connection is handed back to the pool for
    the provider wait (``core.tenancy.connection_released``) and bound to the tenant again after.
    Pass it only before the request has written anything; the helper refuses to release otherwise.

    A list the provider does not OR (job titles) is asked one value at a time and merged
    (``split_payload``); the merged page may be a row or two longer than ``size`` and says whether
    more follow in ``ParsedPage.more``.
    """
    from nexus.core.tenancy import connection_released
    from nexus.prospector.client import counts_supported

    parts = split_payload(view, payload)
    if parts:
        return await _fetch_split(view, parts, page=page, size=size, client=client, ts=ts)

    # A count the provider cannot give is not asked for, and does not turn a cached page into a
    # miss: that re-bought the page on every "Show total" click and still showed no total.
    count = bool(count) and counts_supported(view)
    key = cache.page_key(view, payload, page, size)
    hit = await cache.get_page(key)
    if hit is not None and (not count or hit.total is not None):
        return Fetched(parsed=hit, cached=True)
    provider = client or InfoJoyClient()
    if ts is None:
        outcome = await provider.search(view, payload, page=page, size=size, count=count)
    else:
        async with connection_released(ts):
            outcome = await provider.search(view, payload, page=page, size=size, count=count)
    if not outcome.ok or outcome.page is None:
        logger.info("prospector fetch refused: %s", outcome.status)
        _raise(outcome)
    await cache.put_page(key, view=view, page=page, size=size, parsed=outcome.page)
    return Fetched(parsed=outcome.page, cached=False, vendor_rows=outcome.vendor_rows)


async def record_browse_cost(ts, *, rows: int, user_id: str | None, view: str) -> None:
    """One usage row for the rows bought while browsing. Shadow: never charges. Never raises."""
    if rows <= 0:
        return
    from nexus.billing.meter import metered

    try:
        async with metered(ts, BROWSE_CAPABILITY, quantity=rows, user_id=user_id,
                           attrs={"view": view}):
            pass
    except Exception:  # observing a cost must never fail the search that incurred it
        logger.warning("could not record prospector browse cost", exc_info=True)
