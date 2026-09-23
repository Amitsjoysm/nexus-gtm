"""Reply alerts through the existing alert routing (spec §11, D12).

Immediate to the mailbox owner — the person whose name is on the email — for what needs a human now:
interested, question, referral, needs-decision, a campaign out of credits, a mailbox that needs
reconnecting. Everything else (scheduled, out of office, bounced, unsubscribed, declined) is an
in-app `info` alert that the reply desk and the daily digest pick up. Categories are the ones in
`alerts.rules._ENGAGEMENT_RULES`, so a user can subscribe to them like any other.

Never raises: an alert that cannot be delivered must not undo the reply that caused it.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("nexus.engagement.replies")

IMMEDIATE = {"reply_interested", "reply_question", "reply_referral", "reply_needs_decision",
             "campaign_out_of_credits", "mailbox_needs_reauth"}


async def notify(ts, *, category: str, title: str, body: str = "", owner_user_id: str | None,
                 account_id: str | None = None, meta: dict | None = None):
    from nexus.alerts.fanout import channels_for, fan_out
    from nexus.alerts.rules import engagement_rule
    from nexus.alerts.service import get_alert_service
    from nexus.core.db import utcnow

    severity, action = engagement_rule(category)
    try:
        alert = await get_alert_service().create(
            ts, title=title, body="\n\n".join(x for x in (body, action) if x),
            severity=severity, account_id=account_id, source="engagement",
            meta={"category": category, "owner_user_id": owner_user_id, **(meta or {})})
        if category in IMMEDIATE:
            channels = await channels_for(ts, category=category, severity=severity,
                                          owner_user_id=owner_user_id, now=utcnow())
            if channels:
                from nexus.alerts.connections import resolve_alert_channels

                await fan_out(alert, channels, await resolve_alert_channels(ts))
                await ts.flush()
        return alert
    except Exception:
        logger.warning("engagement alert %s could not be raised", category, exc_info=True)
        return None
