# nexus/accounts/suppression.py
""""Don't suggest these again here": domains a workspace deleted and does not want offered back.

Written by bulk delete when the person ticks the option (`discovery_suppressions`, migration 0076).
Read wherever discovery asks "does this workspace already hold it?" (`prospecting.companies._held`
and the shared-store anti-join), so Populate, the daily sweep and the Orchestrator's discover skip
these exactly as they skip a company the workspace has. Per workspace: another workspace may still
be offered the same company, and the shared record stays for it.
"""
from __future__ import annotations

from sqlalchemy import exists, select


def domain_of(domain: str | None) -> str:
    from nexus.companies.resolution import normalise_domain

    return normalise_domain(domain) or ""


async def suppressed(ts, domains) -> set[str]:
    """Which of ``domains`` this workspace asked not to be offered again."""
    from nexus.models.records import DiscoverySuppression

    wanted = sorted({d for d in domains if d})
    if not wanted:
        return set()
    rows = await ts.session.scalars(select(DiscoverySuppression.domain).where(
        DiscoverySuppression.tenant_id == ts.tenant_id, DiscoverySuppression.domain.in_(wanted)))
    return set(rows.all())


def not_suppressed(ts, domain_column):
    """A WHERE clause: ``domain_column`` is not one this workspace suppressed."""
    from nexus.models.records import DiscoverySuppression

    return ~exists(select(DiscoverySuppression.id).where(
        DiscoverySuppression.tenant_id == ts.tenant_id,
        DiscoverySuppression.domain == domain_column))


async def suppress(ts, domains, *, user_id: str | None, reason: str = "deleted") -> int:
    """Record ``domains`` (normalised; blanks ignored). Idempotent. Returns how many were new."""
    from nexus.models.records import DiscoverySuppression

    clean = sorted({d for d in (domain_of(x) for x in domains) if d})
    if not clean:
        return 0
    have = await suppressed(ts, clean)
    added = [d for d in clean if d not in have]
    for domain in added:
        ts.add(DiscoverySuppression(tenant_id=ts.tenant_id, domain=domain, reason=reason,
                                    created_by_user_id=user_id))
    if added:
        await ts.flush()
    return len(added)
