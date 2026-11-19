# nexus/accounts/suppression.py
""""Don't suggest these again here": domains a workspace deleted and does not want offered back.

Written by bulk delete when the person ticks the option (`discovery_suppressions`, migration 0076).
Read wherever discovery asks "does this workspace already hold it?" (`prospecting.companies._held`
and the shared-store anti-join), so Populate, the daily sweep and the Orchestrator's discover skip
these exactly as they skip a company the workspace has. Per workspace: another workspace may still
be offered the same company, and the shared record stays for it.

An **exclusion list** (2026-11-19) is the same table with reason ``excluded``: domains a workspace
uploads or pastes (its customers, competitors, partners, a CRM's open pipeline) so discovery never
offers them, whatever it holds. Asked as "an enterprise with 2,000+ companies and an exclusion list
of domains". Nothing it holds is touched, and removing an entry lets discovery offer it again.
"""
from __future__ import annotations

import re

from sqlalchemy import delete, exists, func, select

#: Reasons a row carries: deleted by bulk delete with the option ticked, or put on the list.
REASON_DELETED = "deleted"
REASON_EXCLUDED = "excluded"
#: Lookups and writes go in slices: one statement per 1,000 domains, whatever the list's size.
_CHUNK = 1000
_EMAIL = re.compile(r"^[^@\s]+@([^@\s]+)$")


def domain_of(domain: str | None) -> str:
    from nexus.companies.resolution import normalise_domain

    return normalise_domain(domain) or ""


def read_entry(raw: str) -> str:
    """The company domain one pasted entry names: a domain, a website, ``@acme.com`` or an address
    at the company (its domain). Free mail, reserved names and anything unreadable read as ""."""
    text = (raw or "").strip().strip("\"'<>;,").strip()
    if not text:
        return ""
    match = _EMAIL.match(text)
    if match:
        text = match.group(1)
    return domain_of(text.lstrip("@"))


async def suppressed(ts, domains) -> set[str]:
    """Which of ``domains`` this workspace asked not to be offered again."""
    from nexus.models.records import DiscoverySuppression

    wanted = sorted({d for d in domains if d})
    if not wanted:
        return set()
    out: set[str] = set()
    for start in range(0, len(wanted), _CHUNK):
        rows = await ts.session.scalars(select(DiscoverySuppression.domain).where(
            DiscoverySuppression.tenant_id == ts.tenant_id,
            DiscoverySuppression.domain.in_(wanted[start:start + _CHUNK])))
        out |= set(rows.all())
    return out


def not_suppressed(ts, domain_column):
    """A WHERE clause: ``domain_column`` is not one this workspace suppressed."""
    from nexus.models.records import DiscoverySuppression

    return ~exists(select(DiscoverySuppression.id).where(
        DiscoverySuppression.tenant_id == ts.tenant_id,
        DiscoverySuppression.domain == domain_column))


async def exclusions(ts, *, q: str = "", limit: int = 50, offset: int = 0
                     ) -> tuple[int, list]:
    """``(total, rows)`` of the workspace's list, newest first, ``q`` a part of the domain."""
    from nexus.models.records import DiscoverySuppression

    stmt = select(DiscoverySuppression).where(DiscoverySuppression.tenant_id == ts.tenant_id)
    needle = (q or "").strip().lower()
    if needle:
        esc = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        stmt = stmt.where(DiscoverySuppression.domain.like(f"%{esc}%", escape="\\"))
    total = await ts.session.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = (await ts.session.scalars(
        stmt.order_by(DiscoverySuppression.created_at.desc(), DiscoverySuppression.domain)
        .limit(limit).offset(offset))).all()
    return int(total or 0), list(rows)


async def unsuppress(ts, domains) -> int:
    """Take ``domains`` off the list, whatever put them there. Returns how many were removed."""
    from nexus.models.records import DiscoverySuppression

    clean = sorted({d for d in (domain_of(x) for x in domains) if d})
    removed = 0
    for start in range(0, len(clean), _CHUNK):
        result = await ts.session.execute(delete(DiscoverySuppression).where(
            DiscoverySuppression.tenant_id == ts.tenant_id,
            DiscoverySuppression.domain.in_(clean[start:start + _CHUNK])))
        removed += int(result.rowcount or 0)
    return removed


async def suppress(ts, domains, *, user_id: str | None, reason: str = REASON_DELETED) -> int:
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
