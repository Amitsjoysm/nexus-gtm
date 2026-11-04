"""The unified inbox: every engagement email sent and received, one conversation per row, with a
reading pane (SDR round 2, 6.4, 2026-10-29).

Asked for as "a simplified Outlook". The reply desk shows replies that need a decision and nothing
else, so the emails a rep sent, the threads nobody answered, and a reply already dealt with had no
screen. This is that screen, and it is read-only: **answering goes through the reply desk**, which
holds the one send path for an answer (`desk.send_response` → `engagement.sending.send`, the
do-not-contact gate, the exactly-once row and the `outreach.email_send` charge). A second send
button here would be a second path, and the first thing to drift would be the gate.

* **Whose mail.** The viewer's own mailboxes; a manager may ask for the team's, as on the desk.
  A thread on a mailbox the viewer may not see is not found, never forbidden, so an id says nothing.
* **One row per thread** (`engagement_threads`), newest activity first, built from what left or
  arrived (`sent`, `received`, `bounced`): drafts and queued rows are not mail yet.
* **Filters.** Mailbox, campaign (the thread's enrollment's), replied (they wrote at least once),
  and unread. There is no per-person read mark, so **unread means waiting on you**: a reply on the
  thread is still open on the reply desk, nobody has answered or decided it. Opening a thread here
  does not change that; answering or deciding it on the desk does.
* An inbound message is shown without the history it quotes (`parse.strip_quoted`): it quotes our
  own emails back underneath, which are already in the thread above it.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime

SHOWN = ("sent", "received", "bounced")
PAGE = 50
MAX_PAGE = 200


@dataclass(slots=True)
class ThreadRow:
    thread_id: str
    subject: str
    snippet: str
    last_at: datetime | None
    last_direction: str
    message_count: int
    replied: bool
    unread: bool
    contact_id: str | None = None
    contact_name: str = ""
    contact_email: str = ""
    account_id: str | None = None
    account_name: str = ""
    mailbox_id: str = ""
    mailbox_email: str = ""
    campaign_id: str | None = None
    campaign_name: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class ThreadView:
    row: ThreadRow
    messages: list[dict] = field(default_factory=list)
    #: The reply desk item to answer from: the newest reply on the thread, open or handled.
    reply_id: str | None = None
    reply_open: bool = False


async def visible_mailboxes(ts, *, user_id: str, team: bool) -> list:
    from nexus.models.engagement import MailboxConnection

    where = [] if team else [MailboxConnection.owner_user_id == user_id]
    return sorted(await ts.list(MailboxConnection, *where), key=lambda m: m.email or "")


async def threads(ts, *, mailboxes: list, mailbox_id: str | None = None,
                  campaign_id: str | None = None, replied: bool = False, unread: bool = False,
                  unanswered: bool = False, before: datetime | None = None,
                  limit: int = PAGE) -> list[ThreadRow]:
    """One page of conversations on ``mailboxes``, newest activity first. ``unanswered`` is Mail's
    Sent folder (2026-11-04): conversations where nothing has come back yet."""
    from sqlalchemy import select

    from nexus.models.engagement import EngagementEnrollment, EngagementMessage, EngagementThread

    ids = [m.id for m in mailboxes]
    if mailbox_id:
        ids = [i for i in ids if i == mailbox_id]
    if not ids:
        return []
    stmt = ts.select(EngagementThread, EngagementThread.mailbox_connection_id.in_(ids),
                     EngagementThread.id.in_(
                         select(EngagementMessage.thread_id)
                         .where(EngagementMessage.tenant_id == ts.tenant_id,
                                EngagementMessage.thread_id.is_not(None),
                                EngagementMessage.status.in_(SHOWN))))
    if campaign_id:
        stmt = stmt.where(EngagementThread.enrollment_id.in_(
            select(EngagementEnrollment.id)
            .where(EngagementEnrollment.tenant_id == ts.tenant_id,
                   EngagementEnrollment.campaign_id == campaign_id)))
    if replied:
        stmt = stmt.where(EngagementThread.id.in_(
            select(EngagementMessage.thread_id)
            .where(EngagementMessage.tenant_id == ts.tenant_id,
                   EngagementMessage.direction == "in", EngagementMessage.status == "received")))
    if unread:
        stmt = stmt.where(EngagementThread.id.in_(_waiting_threads(ts)))
    if unanswered:
        stmt = stmt.where(EngagementThread.id.not_in(
            select(EngagementMessage.thread_id)
            .where(EngagementMessage.tenant_id == ts.tenant_id,
                   EngagementMessage.thread_id.is_not(None),
                   EngagementMessage.direction == "in", EngagementMessage.status == "received")))
    if before is not None:
        stmt = stmt.where(EngagementThread.last_message_at < before)
    # NULLS LAST said out loud: Postgres sorts nulls first in a descending order, SQLite last.
    stmt = stmt.order_by(EngagementThread.last_message_at.desc().nulls_last(),
                         EngagementThread.id.desc()) \
        .limit(max(1, min(int(limit or PAGE), MAX_PAGE)))
    page = list((await ts.session.scalars(stmt)).all())
    if not page:
        return []
    return await _rows(ts, page, mailboxes)


def _waiting_threads(ts):
    """Threads with a reply still open on the reply desk: the inbox's "unread"."""
    from sqlalchemy import select

    from nexus.models.engagement import EngagementMessage, ReplyClassification

    return (select(EngagementMessage.thread_id)
            .join(ReplyClassification, ReplyClassification.message_id == EngagementMessage.id)
            .where(EngagementMessage.tenant_id == ts.tenant_id,
                   ReplyClassification.status == "open"))


async def _rows(ts, page: list, mailboxes: list) -> list[ThreadRow]:
    from nexus.activity.history import first_lines
    from nexus.models.account import Account, Contact
    from nexus.models.engagement import (
        EngagementCampaign,
        EngagementEnrollment,
        EngagementMessage,
        ReplyClassification,
    )

    by_thread: dict[str, list] = {}
    for m in await ts.list(EngagementMessage, EngagementMessage.thread_id.in_([t.id for t in page]),
                           EngagementMessage.status.in_(SHOWN)):
        by_thread.setdefault(m.thread_id, []).append(m)
    inbound_ids = [m.id for rows in by_thread.values() for m in rows if m.direction == "in"]
    waiting = {r.message_id for r in await ts.list(
        ReplyClassification, ReplyClassification.message_id.in_(inbound_ids),
        ReplyClassification.status == "open")} if inbound_ids else set()

    contact_ids = {t.contact_id for t in page if t.contact_id}
    contacts = {c.id: c for c in await ts.list(Contact, Contact.id.in_(contact_ids))} \
        if contact_ids else {}
    account_ids = {c.account_id for c in contacts.values() if c.account_id}
    accounts = {a.id: a for a in await ts.list(Account, Account.id.in_(account_ids))} \
        if account_ids else {}
    enrollment_ids = {t.enrollment_id for t in page if t.enrollment_id}
    enrollments = {e.id: e for e in await ts.list(
        EngagementEnrollment, EngagementEnrollment.id.in_(enrollment_ids))} if enrollment_ids else {}
    campaigns = {c.id: c for c in await ts.list(
        EngagementCampaign,
        EngagementCampaign.id.in_({e.campaign_id for e in enrollments.values()}))} \
        if enrollments else {}
    boxes = {m.id: m for m in mailboxes}

    out = []
    for t in page:
        rows = sorted(by_thread.get(t.id, []), key=_moment)
        last = rows[-1] if rows else None
        contact = contacts.get(t.contact_id)
        account = accounts.get(getattr(contact, "account_id", None))
        campaign = campaigns.get(getattr(enrollments.get(t.enrollment_id), "campaign_id", None))
        out.append(ThreadRow(
            thread_id=t.id,
            subject=(getattr(last, "subject", "") or t.base_subject or "(no subject)"),
            snippet=first_lines(getattr(last, "body_text", "") or "",
                                inbound=getattr(last, "direction", "") == "in"),
            last_at=_moment(last) if last else t.last_message_at,
            last_direction=getattr(last, "direction", "") or "",
            message_count=len(rows),
            replied=any(m.direction == "in" for m in rows),
            unread=any(m.id in waiting for m in rows),
            contact_id=t.contact_id, contact_name=getattr(contact, "full_name", "") or "",
            contact_email=getattr(contact, "email", "") or "",
            account_id=getattr(account, "id", None), account_name=getattr(account, "name", "") or "",
            mailbox_id=t.mailbox_connection_id,
            mailbox_email=getattr(boxes.get(t.mailbox_connection_id), "email", "") or "",
            campaign_id=getattr(campaign, "id", None), campaign_name=getattr(campaign, "name", "")))
    return out


def _moment(message):
    return message.sent_at or message.received_at or message.created_at


async def thread(ts, thread_id: str, *, mailboxes: list) -> ThreadView | None:
    """One conversation, oldest first, or `None` when it is not on a mailbox the viewer may see."""
    from nexus.engagement.replies.parse import split_quoted
    from nexus.models.engagement import EngagementMessage, EngagementThread, ReplyClassification

    found = await ts.get(EngagementThread, thread_id)
    if found is None or found.mailbox_connection_id not in {m.id for m in mailboxes}:
        return None
    row = (await _rows(ts, [found], mailboxes))[0]
    messages = sorted(await ts.list(EngagementMessage, EngagementMessage.thread_id == found.id,
                                    EngagementMessage.status.in_(SHOWN)), key=_moment)
    readings = {r.message_id: r for r in await ts.list(
        ReplyClassification, ReplyClassification.message_id.in_([m.id for m in messages]))} \
        if messages else {}
    view = ThreadView(row=row)
    for m in messages:
        reading = readings.get(m.id)
        inbound = m.direction == "in"
        body, quoted = split_quoted(m.body_text or "") if inbound else (m.body_text or "", "")
        view.messages.append({
            "id": m.id, "direction": m.direction, "status": m.status,
            "from_addr": m.from_addr or "", "to_addrs": list(m.to_addrs or []),
            "subject": m.subject or "",
            "body": body, "quoted": quoted,
            "at": _moment(m),
            "category": (reading.corrected_category or reading.category) if reading else None,
        })
        if reading is not None:
            view.reply_id, view.reply_open = reading.id, reading.status == "open"
    return view
