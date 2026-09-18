"""How long a cached web answer stays good, by signal kind.

The TTL IS the cadence. There is no scheduler change anywhere in this feature: a dork whose cached
entry is still fresh short-circuits before any request, so `next_refresh_at` and `tiering.classify`
keep working exactly as they do today and the account's timeline is unchanged.

Settings are read on every call. This module is used from the process-wide ingestion service, which
is built once and keeps its sources, so a value captured at import would ignore the Control plane
until a restart — the same trap `DorkedSearchSource` documents for its query cap.
"""
from __future__ import annotations

#: Kinds where being first is the value, so they keep the six-hour loop.
FAST_KINDS = frozenset({"funding", "news"})


def ttl_for_signal_kind(kind: str) -> int:
    """Seconds a searched answer for this signal kind stays good."""
    from nexus.core.config import get_settings

    settings = get_settings()
    if (kind or "").strip().lower() in FAST_KINDS:
        return int(settings.signal_cache_ttl_fast_s)
    return int(settings.signal_cache_ttl_slow_s)


def ttl_for_page() -> int:
    """Seconds a fetched page stays good."""
    from nexus.core.config import get_settings

    return int(get_settings().page_cache_ttl_s)
