"""What each reply category does (spec §6 table, D3, D6, D7, D8, D22).

Nothing here ever sends to the person who wrote (D22): the only sends a reply can cause are the
re-engagement a "later" schedules for its date and the steps an out-of-office resumes. Responses to
human replies are drafted in phase 10 and sent by the SDR.

| category | this person's sequences | colleagues' | suppression | alert |
|---|---|---|---|---|
| interested / question / referral | stopped `replied` | paused `colleague_replied` | — | immediate |
| later (confident, dated) | snoozed until the date | — | — | digest |
| out_of_office | paused until the business day after they are back | — | — | digest |
| declined | stopped `declined` | paused | `declined` | digest |
| unsubscribe | stopped `unsubscribed` | — | `unsubscribed`, permanent | digest |
| other_auto | nothing | — | — | — |
| unclear | paused `needs_decision` | — | — | immediate |
"""
from __future__ import annotations

from datetime import time, timezone

UTC = timezone.utc  # datetime.UTC is 3.11+; this project supports 3.10

RESUME_AT = time(9, 0)
HUMAN = ("interested", "question", "referral")


async def apply(ts, *, verdict, match, inbound, mailbox, classification) -> str:
    from nexus.engagement.replies.alerts import notify
    from nexus.engagement.sequences.service import set_status
    from nexus.engagement.suppression.service import suppress
    from nexus.engagement.timekeeping import at_local, zone_or_none

    category = verdict.category
    owner = mailbox.owner_user_id
    who = getattr(match.contact, "full_name", "") or inbound.from_addr
    company = getattr(match.account, "name", "")
    about = f"{who}{f' at {company}' if company else ''}"
    account_id = getattr(match.account, "id", None)
    meta = {"message_id": inbound.id, "classification_id": classification.id,
            "contact_id": getattr(match.contact, "id", None)}

    async def pause_colleagues():
        for enrollment in match.colleague_enrollments:
            if enrollment.status in ("active", "snoozed", "awaiting_review"):
                await set_status(ts, enrollment, "paused", "colleague_replied")

    if category in HUMAN:
        for enrollment in match.enrollments:
            await set_status(ts, enrollment, "stopped", "replied")
        await pause_colleagues()
        await notify(ts, category=f"reply_{category}", owner_user_id=owner, account_id=account_id,
                     title=f"{about} replied: {category}", body=_snippet(inbound.body_text),
                     meta=meta)
        return f"stopped:replied; notified:reply_{category}"

    if category == "later":
        for enrollment in match.enrollments:
            zone = zone_or_none(enrollment.contact_timezone) or UTC
            enrollment.snoozed_until = at_local(verdict.resolved_date, RESUME_AT, zone)
            await set_status(ts, enrollment, "snoozed", "later")
        await notify(ts, category="reply_scheduled", owner_user_id=owner, account_id=account_id,
                     title=f"{about} asked to reconnect on {verdict.resolved_date:%d %b %Y}",
                     body=_snippet(inbound.body_text), meta=meta)
        return f"snoozed:{verdict.resolved_date.isoformat()}"

    if category == "out_of_office":
        for enrollment in match.enrollments:
            if enrollment.status not in ("active", "awaiting_review"):
                continue
            zone = zone_or_none(enrollment.contact_timezone) or UTC
            enrollment.snoozed_until = at_local(verdict.resume_on, RESUME_AT, zone)
            await set_status(ts, enrollment, "paused", "out_of_office")
        await notify(ts, category="reply_out_of_office", owner_user_id=owner,
                     account_id=account_id,
                     title=f"{about} is away until {verdict.resolved_date:%d %b}", meta=meta)
        return f"paused:out_of_office until {verdict.resume_on.isoformat()}"

    if category in ("declined", "unsubscribe"):
        reason = "declined" if category == "declined" else "unsubscribed"
        for enrollment in match.enrollments:
            await set_status(ts, enrollment, "stopped", reason)
        address = getattr(match.contact, "email", "") or inbound.from_addr
        await suppress(ts, email=address, reason=reason,
                       contact_id=getattr(match.contact, "id", None), source_message_id=inbound.id)
        if category == "declined":
            await pause_colleagues()
        await notify(ts, category=f"reply_{reason}", owner_user_id=owner, account_id=account_id,
                     title=f"{about} {reason}", meta=meta)
        return f"stopped:{reason}; suppressed"

    if category == "other_auto":
        return "ignored"

    for enrollment in match.enrollments:
        if enrollment.status not in ("stopped", "completed"):
            await set_status(ts, enrollment, "paused", "needs_decision")
    await notify(ts, category="reply_needs_decision", owner_user_id=owner, account_id=account_id,
                 title=f"{about} replied — needs your decision",
                 body=_snippet(inbound.body_text), meta=meta)
    return "paused:needs_decision; notified"


async def apply_colleague(ts, *, match, inbound, mailbox, classification) -> str:
    """Someone at the company wrote who is not in the sequence: a referral by default (§6 rule 4)."""
    from nexus.engagement.replies.alerts import notify
    from nexus.engagement.sequences.service import set_status

    for enrollment in match.colleague_enrollments:
        if enrollment.status in ("active", "snoozed", "awaiting_review"):
            await set_status(ts, enrollment, "paused", "colleague_replied")
    await notify(ts, category="reply_referral", owner_user_id=mailbox.owner_user_id,
                 account_id=getattr(match.account, "id", None),
                 title=f"Someone at {getattr(match.account, 'name', 'an account')} replied",
                 body=_snippet(inbound.body_text),
                 meta={"message_id": inbound.id, "classification_id": classification.id})
    return "paused:colleague_replied; notified:reply_referral"


def _snippet(text: str, limit: int = 280) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"
