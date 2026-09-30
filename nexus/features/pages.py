"""The menu pages a superadmin may hide, and which ones are hidden right now.

`HIDEABLE_PAGES` is every entry of the frontend menu (`app/nav.tsx`) except the floor, and
`tests/test_page_visibility.py` pins the two lists together, so a page added to the menu later is
covered the day it ships and the floor can never be added by mistake.

The floor is never hideable: Dashboard, Accounts and Contacts are the product (and
`test_the_floor_of_the_product_is_never_gated` keeps them ungated by plan too), Members and
Settings are how a workspace is run, Billing is where a customer fixes the plan that hid something,
and the Superadmin console is the only place a hide can be undone.

EVERYTHING HERE FAILS OPEN, like `switches.py`: a hide is a restriction, so an unreadable table
hides nothing. 30s TTL for the second API worker; the writing process drops its own cache.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from sqlalchemy import select

from nexus.core.db import get_sessionmaker

logger = logging.getLogger("nexus.features.pages")

TTL_S = 30.0


@dataclass(frozen=True, slots=True)
class PageSpec:
    key: str
    path: str
    label: str
    # The module switch the page sits under, shown beside it so an operator can see that hiding
    # Approvals leaves Orchestrator and AI Runs alone. Empty when the page has none.
    module: str = ""


HIDEABLE_PAGES: tuple[PageSpec, ...] = (
    PageSpec("inbox", "/inbox", "Inbox", "module.signals"),
    PageSpec("calls", "/calls", "Calls", "module.calling"),
    PageSpec("mailboxes", "/mailboxes", "My mailboxes", "module.outreach"),
    PageSpec("campaigns", "/engagement/campaigns", "Campaigns", "module.campaigns"),
    PageSpec("replies", "/engagement/replies", "Replies", "module.campaigns"),
    PageSpec("templates", "/engagement/templates", "Sequence templates", "module.cadences"),
    PageSpec("network", "/network", "Network", "module.network"),
    PageSpec("lists", "/lists", "Lists", "module.lists"),
    PageSpec("signals", "/signals", "Signals", "module.signals"),
    PageSpec("alerts", "/alerts", "Alerts", "module.signals"),
    PageSpec("orchestrator", "/orchestrator", "Orchestrator", "module.agents"),
    PageSpec("runs", "/runs", "AI Runs", "module.agents"),
    PageSpec("approvals", "/approvals", "Approvals", "module.agents"),
    PageSpec("plays", "/plays", "Plays", "module.plays"),
    PageSpec("relevance", "/relevance", "Relevance", "module.relevance"),
    PageSpec("integrations", "/integrations", "Integrations", "module.integrations"),
)

BY_KEY: dict[str, PageSpec] = {page.key: page for page in HIDEABLE_PAGES}

_cache: list[str] | None = None
_loaded_at = 0.0


def invalidate() -> None:
    """Drop the cache. Immediate for THIS process; others wait out the TTL."""
    global _cache, _loaded_at
    _cache, _loaded_at = None, 0.0


async def _load() -> list[str]:
    from nexus.models.page_visibility import PageVisibility

    async with get_sessionmaker()() as session:
        rows = (await session.scalars(
            select(PageVisibility).where(PageVisibility.hidden.is_(True)))).all()
    # A row whose key has left the catalog is ignored rather than applied: a page removed from the
    # menu must not keep a hide nobody can see or undo.
    return [BY_KEY[r.page_key].path for r in rows if r.page_key in BY_KEY]


async def hidden_paths() -> list[str]:
    """The menu paths hidden from every workspace, in menu order. ``[]`` on any failure."""
    global _cache, _loaded_at
    now = time.monotonic()
    if _cache is not None and (now - _loaded_at) < TTL_S:
        return list(_cache)
    try:
        loaded = await _load()
    except Exception:
        logger.warning("page visibility load failed; showing every page", exc_info=True)
        return []
    order = {page.path: i for i, page in enumerate(HIDEABLE_PAGES)}
    _cache, _loaded_at = sorted(loaded, key=order.__getitem__), now
    return list(_cache)
