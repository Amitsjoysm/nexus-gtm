"""A person's or an account's history: every call logged and every email sent or received, newest
first (SDR round 2, 5.2, 2026-10-29).

Asked for as "call and email history per person or account": before this, calls were readable only
one contact at a time inside the call console, and emails only on the account's Emails tab, oldest
first and without the calls between them. One list answers "what has already been said to them?",
which is the question a rep asks before every touch.

* **Calls** come from `call_activities`: when, the category and disposition in the team's own words
  (`calling/dispositions.py`), the notes, and the duration. **Who called** is not on the activity
  row; it is the person who placed the call live on the platform account (`placed_calls.user_id`),
  else the owner of the call task it was logged on, which is who it was assigned to and, in a call
  queue, who works it. Names are resolved through THIS workspace's membership, as the credit report
  does, so someone who has left reads "Former member".
* **Emails** come from `engagement_messages` that left or arrived (`sent`, `received`, `bounced`;
  drafts and queued rows are not history), with the first lines of what was said: an inbound reply
  quotes our own emails back underneath, so its first lines go through `parse.strip_quoted` first.
  The thread link is given only when the viewer may open that thread in the unified inbox: their
  own mailbox, or any mailbox for a manager. Everyone at the account still sees that it happened.

Tenant-scoped through `TenantSession`, so another workspace's contact or account id finds nothing
and the endpoint answers 404.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime

#: The most entries one read returns. A history longer than this is old news; the panel says so.
MAX_ITEMS = 300
#: How much of an email the history shows: the opening lines, not the whole message.
PREVIEW_LINES = 3
PREVIEW_CHARS = 280


@dataclass(slots=True)
class ActivityItem:
    id: str
    kind: str                       # call | email
    at: datetime | None
    contact_id: str | None = None
    contact_name: str = ""
    # A call.
    by: str = ""
    category: str = ""
    disposition: str = ""
    disposition_code: str = ""
    notes: str = ""
    duration_s: int | None = None
    #: Placed through the app's telephony, so the duration was measured unless the rep corrected it.
    placed_live: bool = False
    # An email.
    direction: str = ""             # out | in
    status: str = ""                # sent | received | bounced
    subject: str = ""
    preview: str = ""
    mailbox: str = ""
    campaign_id: str | None = None
    campaign_name: str = ""
    thread_id: str | None = None
    thread_link: str = ""
    reply_category: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def first_lines(text: str, *, inbound: bool) -> str:
    """The opening of a message as the history shows it. An inbound reply loses the history it
    quotes first, or the first lines would be our own email again."""
    from nexus.engagement.replies.parse import strip_quoted

    body = strip_quoted(text or "") if inbound else (text or "")
    lines = [" ".join(line.split()) for line in body.splitlines()]
    shown = " ".join([line for line in lines if line][:PREVIEW_LINES])
    return shown if len(shown) <= PREVIEW_CHARS else shown[:PREVIEW_CHARS - 1].rstrip() + "…"


async def for_contact(ts, contact, *, viewer_id: str, manager: bool,
                      limit: int = MAX_ITEMS) -> list[ActivityItem]:
    from nexus.models.calling import CallActivity

    return await _history(ts, contact_ids={contact.id},
                          call_filter=CallActivity.contact_id == contact.id,
                          viewer_id=viewer_id, manager=manager, limit=limit)


async def for_account(ts, account, *, viewer_id: str, manager: bool,
                      limit: int = MAX_ITEMS) -> list[ActivityItem]:
    """Everyone at the account, the people since removed included: what was said to them is still
    the account's history."""
    from sqlalchemy import select

    from nexus.models.account import Contact
    from nexus.models.calling import CallActivity

    people = set((await ts.session.scalars(
        select(Contact.id).where(Contact.tenant_id == ts.tenant_id,
                                 Contact.account_id == account.id))).all())
    return await _history(ts, contact_ids=people,
                          call_filter=CallActivity.account_id == account.id,
                          viewer_id=viewer_id, manager=manager, limit=limit)


async def _history(ts, *, contact_ids: set[str], call_filter, viewer_id: str, manager: bool,
                   limit: int) -> list[ActivityItem]:
    limit = max(1, min(int(limit or MAX_ITEMS), MAX_ITEMS))
    items = await _calls(ts, call_filter, limit) + await _emails(ts, contact_ids, limit,
                                                                 viewer_id=viewer_id,
                                                                 manager=manager)
    names = await _contact_names(ts, {i.contact_id for i in items if i.contact_id})
    for item in items:
        item.contact_name = names.get(item.contact_id or "", "")
    items.sort(key=lambda i: _sort_key(i.at), reverse=True)
    return items[:limit]


def _sort_key(at: datetime | None) -> float:
    from nexus.core.db import ensure_aware

    moment = ensure_aware(at)
    return moment.timestamp() if moment is not None else 0.0


async def _calls(ts, call_filter, limit: int) -> list[ActivityItem]:
    from nexus.billing.usage_report import FORMER_MEMBER, _member_names
    from nexus.calling.dispositions import category_label, lookup
    from nexus.models.calling import CallActivity, CallTask, PlacedCall

    rows = list((await ts.session.scalars(
        ts.select(CallActivity, call_filter)
        .order_by(CallActivity.occurred_at.desc()).limit(limit))).all())
    if not rows:
        return []
    task_ids = {r.call_task_id for r in rows if r.call_task_id}
    owners = {t.id: t.owner_user_id for t in await ts.list(CallTask, CallTask.id.in_(task_ids))} \
        if task_ids else {}
    call_ids = {r.provider_call_id for r in rows if r.provider_call_id}
    placers = {p.provider_call_id: p.user_id for p in await ts.list(
        PlacedCall, PlacedCall.provider_call_id.in_(call_ids))} if call_ids else {}

    def caller(row) -> str | None:
        return placers.get(row.provider_call_id or "") or owners.get(row.call_task_id or "")

    people = await _member_names(ts, {u for u in (caller(r) for r in rows) if u})
    out = []
    for r in rows:
        who = caller(r)
        known = lookup(r.disposition)
        out.append(ActivityItem(
            id=r.id, kind="call", at=r.occurred_at, contact_id=r.contact_id,
            by=(people.get(who, (FORMER_MEMBER, ""))[0] if who else ""),
            category=category_label(r.disposition),
            disposition=known.label if known else (r.disposition or "").replace("_", " "),
            disposition_code=r.disposition or "", notes=r.notes or "",
            duration_s=r.duration_s, placed_live=bool(r.provider_call_id)))
    return out


async def _emails(ts, contact_ids: set[str], limit: int, *, viewer_id: str,
                  manager: bool) -> list[ActivityItem]:
    from nexus.models.engagement import (
        EngagementCampaign,
        EngagementEnrollment,
        EngagementMessage,
        MailboxConnection,
        ReplyClassification,
    )

    if not contact_ids:
        return []
    when = EngagementMessage.created_at
    rows = list((await ts.session.scalars(
        ts.select(EngagementMessage, EngagementMessage.contact_id.in_(contact_ids),
                  EngagementMessage.status.in_(("sent", "received", "bounced")))
        .order_by(when.desc()).limit(limit))).all())
    if not rows:
        return []
    enrollment_ids = {m.enrollment_id for m in rows if m.enrollment_id}
    enrollments = {e.id: e for e in await ts.list(
        EngagementEnrollment, EngagementEnrollment.id.in_(enrollment_ids))} if enrollment_ids else {}
    campaign_ids = {e.campaign_id for e in enrollments.values()}
    campaigns = {c.id: c for c in await ts.list(
        EngagementCampaign, EngagementCampaign.id.in_(campaign_ids))} if campaign_ids else {}
    mailboxes = {b.id: b for b in await ts.list(
        MailboxConnection, MailboxConnection.id.in_({m.mailbox_connection_id for m in rows}))}
    readings = {r.message_id: r for r in await ts.list(
        ReplyClassification, ReplyClassification.message_id.in_([m.id for m in rows]))}

    out = []
    for m in rows:
        campaign = campaigns.get(getattr(enrollments.get(m.enrollment_id), "campaign_id", None))
        mailbox = mailboxes.get(m.mailbox_connection_id)
        reading = readings.get(m.id)
        inbound = m.direction == "in"
        may_open = manager or getattr(mailbox, "owner_user_id", None) == viewer_id
        out.append(ActivityItem(
            id=m.id, kind="email", at=m.sent_at or m.received_at or m.created_at,
            contact_id=m.contact_id, direction=m.direction, status=m.status,
            subject=m.subject or "", preview=first_lines(m.body_text or "", inbound=inbound),
            mailbox=getattr(mailbox, "email", "") or "",
            campaign_id=getattr(campaign, "id", None), campaign_name=getattr(campaign, "name", ""),
            thread_id=m.thread_id,
            thread_link=f"/engagement/inbox?thread={m.thread_id}" if m.thread_id and may_open else "",
            reply_category=(reading.corrected_category or reading.category) if reading else None))
    return out


async def _contact_names(ts, ids: set[str]) -> dict[str, str]:
    from nexus.models.account import Contact

    if not ids:
        return {}
    return {c.id: c.full_name or c.email or "" for c in await ts.list(Contact, Contact.id.in_(ids))}
