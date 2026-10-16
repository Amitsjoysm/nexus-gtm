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


async def fetch_page(view: str, payload: dict, *, page: int, size: int, count: bool = False,
                     client: InfoJoyClient | None = None) -> Fetched:
    key = cache.page_key(view, payload, page, size)
    hit = await cache.get_page(key)
    if hit is not None and (not count or hit.total is not None):
        return Fetched(parsed=hit, cached=True)
    outcome = await (client or InfoJoyClient()).search(view, payload, page=page, size=size,
                                                      count=count)
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
