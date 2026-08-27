"""Reading and writing per-workspace signal preferences.

The gate lives at the single persist point (`IngestionService.ingest`) rather than at source
selection, because **one source yields several kinds**: `WebNewsSource` alone returns funding,
hiring and news. Disabling a source to disable a kind would switch off the other two as well, and
the shared-company fan-out writes through `ingest` too — so gating there covers both paths without
a second rule that could drift.
"""
from __future__ import annotations

from nexus.models.signal_preference import SignalPreference


async def disabled_kinds(ts) -> set[str]:
    """Kinds this workspace has switched OFF.

    Returned as a set and read once per ingest call rather than once per signal: a crawl can hand
    over dozens of signals for one account, and a query each would turn a config lookup into an N+1
    on the hot ingestion path.
    """
    rows = await ts.list(SignalPreference)
    return {r.kind for r in rows if not r.enabled}


async def kind_enabled(ts, kind: str) -> bool:
    """Is this signal kind collected for this workspace?

    No row -> True. An unknown kind -> True. Both follow this codebase's standing bias that unknown
    resolves permissive: a signal kind added in a later release must not be silently dropped for
    every existing workspace until someone remembers to write a row. That failure is invisible —
    signals simply stop, which is indistinguishable from a quiet market.
    """
    row = await ts.first(SignalPreference, SignalPreference.kind == kind)
    return True if row is None else bool(row.enabled)


async def set_kind(ts, kind: str, *, enabled: bool) -> SignalPreference:
    row = await ts.first(SignalPreference, SignalPreference.kind == kind)
    if row is None:
        row = SignalPreference(tenant_id=ts.tenant_id, kind=kind, enabled=enabled)
        ts.add(row)
    else:
        row.enabled = enabled
    await ts.flush()
    return row


async def current_preferences(ts) -> dict[str, bool]:
    """Every known kind with its effective state, so the UI can render the full list.

    Built from the CATALOGUE and overlaid with stored rows, never from the rows alone — a screen
    that only lists what somebody has already toggled cannot be used to toggle anything for the
    first time.
    """
    from nexus.ingestion.service import SIGNAL_KINDS

    stored = {r.kind: bool(r.enabled) for r in await ts.list(SignalPreference)}
    return {kind: stored.get(kind, True) for kind in sorted(SIGNAL_KINDS)}
