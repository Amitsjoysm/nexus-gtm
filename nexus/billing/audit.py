# nexus/billing/audit.py
"""Audit trail for platform-admin mutations.

docs/billing/17-Production-Checklist.md §Admin requires 100% of admin mutations captured. The
point is answerability: when a customer disputes a charge or a price changes unexpectedly,
"who did this, when, and what was it before" has to come from a record, not from memory.

Recording never fails the mutation it describes — a billing change that succeeded must not be
rolled back because its audit row could not be written. But the failure is logged at ERROR
rather than shrugged off: an unaudited admin write is a real problem.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from nexus.models.billing import BillingAuditLog

logger = logging.getLogger("nexus.billing.audit")


def snapshot(obj: Any, fields: tuple[str, ...]) -> dict:
    """Pull a comparable dict off an ORM row. Returns {} for None so a create reads as
    before={} rather than crashing."""
    if obj is None:
        return {}
    out: dict[str, Any] = {}
    for f in fields:
        value = getattr(obj, f, None)
        # Numeric/Decimal and datetimes are not JSON-serialisable; store them as text.
        if isinstance(value, (str, int, float, bool)) or value is None:
            out[f] = value
        else:
            out[f] = str(value)
    return out


async def record_admin_action(
    session: AsyncSession,
    *,
    actor: str,
    action: str,
    target: str = "",
    subject_tenant_id: str | None = None,
    before: dict | None = None,
    after: dict | None = None,
    note: str = "",
) -> bool:
    """Append one audit row. Returns True if written.

    Caller passes an already-open session so the audit row commits with the mutation it
    describes — an audit entry for a change that got rolled back would be worse than none.
    """
    try:
        session.add(
            BillingAuditLog(
                actor=actor or "unknown",
                action=action,
                target=target,
                subject_tenant_id=subject_tenant_id,
                before=before or {},
                after=after or {},
                note=note,
            )
        )
        await session.flush()
        return True
    except Exception:
        logger.error("audit write failed for %s on %s", action, target, exc_info=True)
        return False
