"""Mailbox health: the bounce rate over the last seven days (spec §19).

Deliverability decays quietly. A mailbox that bounces more than 3% of what it sends is on its way to
the spam folder for everyone, including the buyers whose addresses are good. The figure sits next
to the over-50-a-day warning on My mailboxes, where the SDR already looks.

A warning, never a block, like the volume warning (D10): the fix is usually the list, and stopping
the mailbox would stop the good sends with the bad.

Below `MIN_SENT` sends there is no rate to speak of: one bounce in five sends is 20% and means
nothing, so the figure is reported but no warning is raised.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

WINDOW_DAYS = 7
WARN_ABOVE = 0.03
MIN_SENT = 20


@dataclass(slots=True)
class MailboxHealth:
    sent_7d: int
    bounced_7d: int
    bounce_rate_7d: float
    warning: str

    def as_dict(self) -> dict:
        return asdict(self)


def assess(sent: int, bounced: int) -> MailboxHealth:
    """Pure: the rate and whether it warrants a warning."""
    rate = round(bounced / sent, 4) if sent else 0.0
    warning = ""
    if sent >= MIN_SENT and rate > WARN_ABOVE:
        warning = (f"{rate:.1%} of this week's emails bounced. Above {WARN_ABOVE:.0%}, providers "
                   "start sending the rest to spam. Check the addresses before sending more.")
    return MailboxHealth(sent_7d=sent, bounced_7d=bounced, bounce_rate_7d=rate, warning=warning)


async def mailbox_health(ts, mailbox, *, now: datetime) -> MailboxHealth:
    from sqlalchemy import func, select

    from nexus.models.engagement import EngagementMessage

    since = now - timedelta(days=WINDOW_DAYS)
    rows = (await ts.session.execute(
        select(EngagementMessage.status, func.count())
        .where(EngagementMessage.tenant_id == ts.tenant_id)
        .where(EngagementMessage.mailbox_connection_id == mailbox.id)
        .where(EngagementMessage.direction == "out")
        .where(EngagementMessage.status.in_(("sent", "bounced")))
        .where(EngagementMessage.sent_at >= since)
        .group_by(EngagementMessage.status))).all()
    counts = {status: int(n) for status, n in rows}
    bounced = counts.get("bounced", 0)
    # A bounced message was sent first: it counts in both.
    return assess(counts.get("sent", 0) + bounced, bounced)
