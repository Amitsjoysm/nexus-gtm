"""CRM activity logging (spec §19): the emails an SDR sends, the replies they get and the meetings
they book, written to the workspace's HubSpot or Salesforce so nobody logs them by hand.

**The same connector, and the same records, as the account sync.** The worker resolves the
connector exactly as `handle_sync_crm_due_accounts` does, and an activity is written only against an
account that sync has already put in that CRM (`accounts.crm_id`, with `crm_source` naming the same
CRM). An account the CRM does not know yet waits, and its activities follow once it is there, while
they are still inside the look-back. Creating a CRM company here would be a second sync path with
its own idea of what a company is; writing against a `crm_id` from a CRM the workspace has since
left would attach the note to a record in the wrong system.

**Once each.** A row is marked `crm_logged_at` only when the CRM accepted it, and a failure is tried
again on the next tick. The one duplicate this cannot rule out is a push the CRM accepted whose mark
was then lost with the transaction.

**Nothing old is backfilled.** Seven days back, so switching the log on does not pour a year of
history into a customer's CRM in one sweep.

**A reply is a person writing back.** Out-of-office and other automatic answers are not activities
anyone wants in a CRM, and a bounce is a delivery failure, not a reply.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

LOOKBACK = timedelta(days=7)
BATCH = 200
NOT_A_REPLY = ("out_of_office", "other_auto")


@dataclass(slots=True)
class Activity:
    kind: str                  # email_sent | email_reply | meeting_booked
    row: object                # what gets marked: the message, or the classification for a meeting
    account_id: str | None
    contact_id: str | None
    subject: str
    at: datetime


async def pending(ts, *, now: datetime) -> list[Activity]:
    """What has happened in the look-back and is not in the CRM yet, oldest first."""
    from nexus.models.account import Contact
    from nexus.models.engagement import EngagementMessage, ReplyClassification

    since = now - LOOKBACK
    sent = await ts.list(EngagementMessage, EngagementMessage.direction == "out",
                         EngagementMessage.status == "sent",
                         EngagementMessage.crm_logged_at.is_(None),
                         EngagementMessage.sent_at >= since)
    received = await ts.list(EngagementMessage, EngagementMessage.direction == "in",
                             EngagementMessage.status == "received",
                             EngagementMessage.crm_logged_at.is_(None),
                             EngagementMessage.received_at >= since)
    readings = {c.message_id: c for c in await ts.list(
        ReplyClassification, ReplyClassification.message_id.in_([m.id for m in received]))} \
        if received else {}
    replies = [m for m in received if m.id in readings
               and (readings[m.id].corrected_category or readings[m.id].category) not in NOT_A_REPLY]
    meetings = await ts.list(ReplyClassification, ReplyClassification.decision == "meeting",
                             ReplyClassification.crm_logged_at.is_(None),
                             ReplyClassification.decided_at >= since)

    contact_ids = {m.contact_id for m in sent + replies if m.contact_id}
    accounts_of = {c.id: c.account_id for c in await ts.list(
        Contact, Contact.id.in_(contact_ids))} if contact_ids else {}
    out = [Activity("email_sent", m, accounts_of.get(m.contact_id), m.contact_id, m.subject,
                    m.sent_at) for m in sent]
    out += [Activity("email_reply", m, readings[m.id].account_id or accounts_of.get(m.contact_id),
                     m.contact_id, m.subject, m.received_at) for m in replies]
    out += [Activity("meeting_booked", c, c.account_id, c.contact_id, "", c.decided_at)
            for c in meetings]
    return sorted(out, key=lambda a: a.at)


def describe(activity: Activity, contact_name: str) -> str:
    """The line the CRM shows. The subject is the email's own; nothing else of its text leaves."""
    who = contact_name or "a contact"
    if activity.kind == "email_sent":
        return f"Email to {who}: {activity.subject}".strip()
    if activity.kind == "email_reply":
        return f"Reply from {who}: {activity.subject}".strip()
    return f"Meeting booked with {who}"


async def log_pending(ts, connector, *, now: datetime) -> dict:
    """Write what is pending through ``connector``. Never raises: a connector answers with a
    result, and a CRM that is down is tried again on the next tick."""
    from nexus.models.account import Account, Contact

    activities = (await pending(ts, now=now))[:BATCH]
    account_ids = {a.account_id for a in activities if a.account_id}
    contact_ids = {a.contact_id for a in activities if a.contact_id}
    accounts = {a.id: a for a in await ts.list(Account, Account.id.in_(account_ids))} \
        if account_ids else {}
    contacts = {c.id: c for c in await ts.list(Contact, Contact.id.in_(contact_ids))} \
        if contact_ids else {}
    logged = waiting = failed = 0
    for activity in activities:
        account = accounts.get(activity.account_id)
        if account is None or not account.crm_id or account.crm_source != connector.source:
            waiting += 1
            continue
        contact = contacts.get(activity.contact_id)
        result = await connector.push_activity(
            account_id=account.crm_id, kind=activity.kind,
            detail={"subject": describe(activity, getattr(contact, "full_name", "") or ""),
                    "email": getattr(contact, "email", "") or "",
                    "at": activity.at.isoformat() if activity.at else ""})
        if result.ok:
            activity.row.crm_logged_at = now
            logged += 1
        else:
            failed += 1
    await ts.flush()
    return {"logged": logged, "waiting": waiting, "failed": failed}
