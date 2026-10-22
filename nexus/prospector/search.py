"""The Total and Net new tabs.

**Total**: one provider page (25/50/100 rows), every row masked and marked Saved when the workspace
already holds it. ``include_count`` asks the provider for a total on that same request, explicitly
only, because a count makes the call slower.

**Net new** (decision D12): the provider cannot exclude our records, so the page is filled by
scanning provider pages of ``max(100, page_size)`` and skipping held rows. It stops when the page
is full, the provider has nothing more, or ``prospector_net_new_scan_rows`` rows (or the day's
remaining budget, whichever is smaller) have been scanned. Every scanned row was bought, including
the hidden ones, so the cap is a cost bound and the response says when it was reached
(``scan_capped``). The next click resumes from a signed cursor.

Ordering inside a request (the ``fetching/cache.py`` rule): every cache write happens in
``fetch_page`` before any tenant write; the daily count and the COGS record are written once, last.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from nexus.prospector import cursor as cursors
from nexus.prospector import limits
from nexus.prospector.client import InfoJoyClient
from nexus.prospector.fetch import fetch_page, record_browse_cost
from nexus.prospector.holdings import held_companies, held_contacts
from nexus.prospector.identity import company_identity, contact_identity, row_key
from nexus.prospector.presenter import company_row, contact_row
from nexus.prospector.records import parse_company, parse_contact
from nexus.prospector.schemas import ProspectRowOut, SearchIn, SearchOut

logger = logging.getLogger("nexus.prospector.search")

_client_override: InfoJoyClient | None = None


def set_client(client: InfoJoyClient | None) -> None:
    """Test seam: the client every Prospector request uses. ``None`` restores the real one."""
    global _client_override
    _client_override = client


def client() -> InfoJoyClient:
    return _client_override or InfoJoyClient()


@dataclass(slots=True)
class _Present:
    rows: list[ProspectRowOut]
    held_flags: list[bool]


async def present(ts, view: str, items: list[dict], *, api_page: int,
                  api_page_size: int) -> _Present:
    """Rows for one provider page, in the provider's order, with holdings resolved in bulk."""
    if view == "contacts":
        records = [parse_contact(i) for i in items]
        held = await held_contacts(ts, records)
        idents = [contact_identity(r) for r in records]
        build = contact_row
    else:
        records = [parse_company(i) for i in items]
        held = await held_companies(ts, records)
        idents = [company_identity(r) for r in records]
        build = company_row
    rows, flags = [], []
    for index, (record, ident) in enumerate(zip(records, idents)):
        h = held.get(ident) if ident else None
        # A row with no identity (no email and no usable domain) cannot be bought; its key is
        # stable for display and resolves to "unavailable" if selected.
        key = row_key(ts.tenant_id, ident or f"x:{api_page}:{api_page_size}:{index}")
        rows.append(build(record, key=key, held=h, api_page=api_page,
                          api_page_size=api_page_size))
        flags.append(h is not None)
    return _Present(rows=rows, held_flags=flags)


async def search(ts, *, user_id: str, body: SearchIn) -> SearchOut:
    canonical = body.filters.canonical(body.view)          # 422 for a filter the view refuses
    await limits.check_rate(ts.tenant_id, user_id)
    if body.scope == "total":
        out, served, bought = await _total(ts, body, canonical)
    else:
        out, served, bought = await _net_new(ts, body, canonical)
    await limits.count_rows(ts, served)
    await record_browse_cost(ts, rows=bought, user_id=user_id, view=body.view)
    logger.info("prospector search view=%s scope=%s filters=%s rows=%d served=%d bought=%d",
                body.view, body.scope, body.filters.active_keys(), len(out.rows), served, bought)
    return out


async def _total(ts, body: SearchIn, canonical: dict) -> tuple[SearchOut, int, int]:
    await limits.ensure_budget(ts, body.page_size)
    fetched = await fetch_page(body.view, canonical, page=body.page, size=body.page_size,
                               count=body.include_count, client=client(), ts=ts)
    items = fetched.parsed.items
    shown = await present(ts, body.view, items, api_page=body.page, api_page_size=body.page_size)
    out = SearchOut(rows=shown.rows, page=body.page, page_size=body.page_size,
                    has_more=len(items) >= body.page_size, total=fetched.parsed.total)
    return out, len(items), fetched.vendor_rows


def _scan_cap() -> int:
    from nexus.core.config import get_settings

    return max(1, int(get_settings().prospector_net_new_scan_rows))


#: A Net new scan stops asking for further pages once this many seconds have passed and returns
#: what it found with a cursor ("Keep scanning" resumes). Without it a scan of five slow provider
#: pages could keep the rep waiting for minutes on one click.
NET_NEW_DEADLINE_S = 45.0


async def _net_new(ts, body: SearchIn, canonical: dict) -> tuple[SearchOut, int, int]:
    query = cursors.query_hash(body.view, canonical)
    if body.cursor:
        position = cursors.decode(ts.tenant_id, body.cursor, expect_query=query)
    else:
        position = {"p": 1, "o": 0, "s": max(100, body.page_size), "q": query}
    budget = await limits.ensure_budget(ts, 1)
    cap = min(_scan_cap(), budget)

    page, offset, size = position["p"], position["o"], position["s"]
    rows: list[ProspectRowOut] = []
    scanned = bought = 0
    exhausted = timed_out = False
    started = time.monotonic()
    while len(rows) < body.page_size and scanned < cap:
        if scanned and time.monotonic() - started >= NET_NEW_DEADLINE_S:
            timed_out = True                         # keep what was found; Next resumes here
            break
        fetched = await fetch_page(body.view, canonical, page=page, size=size, client=client(),
                                   ts=ts)
        bought += fetched.vendor_rows
        items = fetched.parsed.items
        window = items[offset:]
        if not window:
            exhausted = True
            break
        shown = await present(ts, body.view, window, api_page=page, api_page_size=size)
        consumed = 0
        for row, held in zip(shown.rows, shown.held_flags):
            consumed += 1
            scanned += 1
            if not held:
                rows.append(row)
            if len(rows) >= body.page_size or scanned >= cap:
                break
        if offset + consumed < len(items):
            offset += consumed                       # stopped inside this page
        elif len(items) < size:
            exhausted = True                         # a short page is the last one
            break
        else:
            page, offset = page + 1, 0

    next_cursor = None if exhausted else cursors.encode(
        ts.tenant_id, {"p": page, "o": offset, "s": size, "q": query})
    out = SearchOut(rows=rows, page=1, page_size=body.page_size, has_more=not exhausted,
                    next_cursor=next_cursor, scanned=scanned, exhausted=exhausted,
                    scan_capped=(not exhausted and len(rows) < body.page_size
                                 and (scanned >= cap or timed_out)))
    return out, scanned, bought
