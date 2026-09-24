"""Engagement activity as `Outcome` rows, so the dashboards and account tiering keep working (§11).

The attribution dashboards, the ROI rollup and `tiering.classify` all read `outcomes`. The old
engine wrote a `sent` outcome per touch; without the same here, a workspace that moved to the new
engine would watch its funnel go flat while it sent more than ever.

* **sent** — every campaign step or re-engagement that left, like the old engine's per-touch row.
  One-off sends and desk answers are not campaign touches and are not counted.
* **replied** — once per person per campaign (their first human reply), or once per thread for a
  reply to a one-off email. An out-of-office is not a reply from the person.
* **meeting** — written by the reply desk's "Meeting booked", with the same attribution.

`Outcome.campaign_id` is the OLD campaigns table's foreign key and stays so (spec §13); the new
campaign travels in `meta.engagement_campaign_id` beside `enrollment_id` and `message_id`.

**Never raises, and never takes the caller down with it.** Each write runs in a savepoint: a
failed outcome row must not roll back the send it describes, which would turn "the email left" into
"the email left and we have no record of it".
"""
from __future__ import annotations

import logging

logger = logging.getLogger("nexus.engagement.reports.outcomes")

COUNTED_SEND_KINDS = ("step", "reengage")


def attribution(enrollment, message=None) -> dict:
    meta = {"source": "engagement"}
    if enrollment is not None:
        meta["engagement_campaign_id"] = enrollment.campaign_id
        meta["enrollment_id"] = enrollment.id
    if message is not None:
        meta["message_id"] = message.id
        if message.step_index is not None:
            meta["step_index"] = message.step_index
    return meta


async def _record(ts, stage: str, *, account_id, contact_id, meta: dict) -> None:
    from nexus.outcomes.service import get_outcome_service

    try:
        async with ts.session.begin_nested():
            await get_outcome_service().record(ts, stage=stage, account_id=account_id,
                                               contact_id=contact_id, meta=meta)
    except Exception:
        logger.warning("could not record the %s outcome", stage, exc_info=True)


async def record_sent(ts, message, enrollment) -> None:
    if enrollment is None or message.kind not in COUNTED_SEND_KINDS:
        return
    await _record(ts, "sent", account_id=enrollment.account_id, contact_id=message.contact_id,
                  meta=attribution(enrollment, message))


async def record_replied(ts, message, classification) -> None:
    """The person's first human reply to this campaign (or to this thread, for a one-off)."""
    from sqlalchemy import func, select

    from nexus.models.engagement import (
        EngagementEnrollment,
        EngagementMessage,
        ReplyClassification,
    )

    if classification.category == "out_of_office" or message.inbound_kind != "human":
        return
    scope = (EngagementMessage.enrollment_id == message.enrollment_id
             if message.enrollment_id else EngagementMessage.thread_id == message.thread_id)
    # An earlier out-of-office was not them replying, so it does not make this the second reply.
    earlier = (await ts.session.execute(
        select(func.count()).select_from(EngagementMessage)
        .join(ReplyClassification, ReplyClassification.message_id == EngagementMessage.id)
        .where(EngagementMessage.tenant_id == ts.tenant_id)
        .where(scope)
        .where(EngagementMessage.direction == "in")
        .where(EngagementMessage.inbound_kind == "human")
        .where(EngagementMessage.id != message.id)
        .where(ReplyClassification.category != "out_of_office"))).scalar_one()
    if earlier:
        return
    enrollment = await ts.get(EngagementEnrollment, message.enrollment_id) \
        if message.enrollment_id else None
    await _record(ts, "replied", account_id=classification.account_id,
                  contact_id=message.contact_id, meta=attribution(enrollment, message))
