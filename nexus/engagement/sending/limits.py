"""How much a mailbox has sent today, and the warning above 50 (spec §5, D10).

**A warning, never a block.** The product does not cap sending (D10): a hard limit would stop an SDR
who knows their domain is warmed, and the real limits are the providers' own, which pause the mailbox
when they bite. What the product owes the SDR is the number and the risk, stated before they approve
a batch and while they are over it.

"Today" is the mailbox owner's day, from local midnight in the mailbox's timezone. A UTC day would
reset mid-afternoon for an SDR in California and count yesterday's evening sends for one in Sydney.
"""
from __future__ import annotations

from datetime import datetime, timezone

UTC = timezone.utc  # datetime.UTC is 3.11+; this project supports 3.10

#: Above this many a day, the chance of being marked as spam rises for most domains.
VOLUME_WARNING_THRESHOLD = 50


async def sent_today(ts, mailbox, *, now: datetime | None = None) -> int:
    """Outbound messages this mailbox has sent since its local midnight."""
    from sqlalchemy import func, select

    from nexus.core.db import utcnow
    from nexus.engagement.timekeeping import local_day_start, zone_or_none
    from nexus.models.engagement import EngagementMessage

    moment = now or utcnow()
    start = local_day_start(moment, zone_or_none(mailbox.timezone) or UTC)
    # `ts.select` takes a model; an aggregate names the tenant explicitly instead.
    stmt = (select(func.count(EngagementMessage.id))
            .where(EngagementMessage.tenant_id == ts.tenant_id)
            .where(EngagementMessage.mailbox_connection_id == mailbox.id)
            .where(EngagementMessage.direction == "out")
            .where(EngagementMessage.status == "sent")
            .where(EngagementMessage.sent_at >= start))
    return int((await ts.session.execute(stmt)).scalar_one() or 0)


def volume_warning(mailbox_email: str, today: int, planned: int = 0) -> str:
    """The sentence the launch, approval and mailbox screens show, or ``""`` below the threshold."""
    total = max(0, int(today)) + max(0, int(planned))
    if total <= VOLUME_WARNING_THRESHOLD:
        return ""
    return (f"This will send about {total} emails from {mailbox_email} today. Above "
            f"{VOLUME_WARNING_THRESHOLD} a day raises the chance of being marked as spam.")
