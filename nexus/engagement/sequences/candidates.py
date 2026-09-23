"""Who can be added to a campaign, and what has already been said to them (spec §9).

Two reads the campaign screens need and nothing else provides:

* **Candidates.** The builder's first step picks contacts "from a list, filters or search", and a
  saved list holds ACCOUNTS, so it has to expand to the people at them, narrowed by title and
  seniority (§9: "an account list expands to contacts with title and seniority pickers"). A contact
  with no address is left out, because it could never be sent to. One on the do-not-contact list
  is returned and marked, not hidden: an SDR who cannot find a person they expected needs to see
  why, and `enroll` refuses them anyway.
* **The conversation timeline.** Every engagement message to or from a contact (or everyone at an
  account), across campaigns and one-off sends, newest last — the "cross-campaign conversation
  timeline" on the contact and account pages.

Both are bounded, and both read in a fixed number of queries whatever the page size.
"""
from __future__ import annotations

from dataclasses import dataclass

#: The most a candidate search returns. A campaign bigger than this is added in several passes,
#: which is also the size at which a person stops reading the rows they are ticking.
CANDIDATE_LIMIT = 500
TIMELINE_LIMIT = 200


@dataclass(slots=True)
class Candidate:
    contact_id: str
    full_name: str
    title: str
    seniority: str
    email: str
    email_status: str
    account_id: str
    account_name: str
    blocked: bool


def _terms(raw: str | None) -> list[str]:
    return [t.strip().lower() for t in (raw or "").split(",") if t.strip()]


async def candidates(ts, *, list_id: str | None = None, q: str | None = None,
                     title: str | None = None, seniority: str | None = None,
                     limit: int = CANDIDATE_LIMIT) -> list[Candidate]:
    """Contacts with an address, optionally from one saved list, filtered by title keywords (any of
    a comma-separated set), seniority (any of a set) and free text."""
    from sqlalchemy import func, or_, select

    from nexus.models.account import Account, Contact
    from nexus.models.engagement import DoNotContact
    from nexus.models.workflow import ListItem

    stmt = (select(Contact, Account)
            .join(Account, Account.id == Contact.account_id)
            .where(Contact.tenant_id == ts.tenant_id, Account.tenant_id == ts.tenant_id)
            .where(Contact.deleted_at.is_(None))
            .where(func.coalesce(Contact.email, "") != ""))
    if list_id:
        items = await ts.list(ListItem, ListItem.list_id == list_id)
        named = {i.contact_id for i in items if i.contact_id}
        whole = {i.account_id for i in items if not i.contact_id}
        if not named and not whole:
            return []
        # A list item that names a person means that person; one that names only an account means
        # everyone there.
        clauses = []
        if named:
            clauses.append(Contact.id.in_(named))
        if whole:
            clauses.append(Contact.account_id.in_(whole))
        stmt = stmt.where(or_(*clauses))
    if q and q.strip():
        like = f"%{q.strip().lower()}%"
        stmt = stmt.where(or_(func.lower(Contact.full_name).like(like),
                              func.lower(func.coalesce(Contact.title, "")).like(like),
                              func.lower(Contact.email).like(like),
                              func.lower(Account.name).like(like)))
    titles = _terms(title)
    if titles:
        stmt = stmt.where(or_(*[func.lower(func.coalesce(Contact.title, "")).like(f"%{t}%")
                                for t in titles]))
    levels = _terms(seniority)
    if levels:
        stmt = stmt.where(func.lower(func.coalesce(Contact.seniority, "")).in_(levels))
    stmt = stmt.order_by(Account.name.asc(), Contact.full_name.asc()) \
        .limit(max(1, min(limit, CANDIDATE_LIMIT)))
    rows = (await ts.session.execute(stmt)).all()
    if not rows:
        return []

    addresses = {(c.email or "").strip().lower() for c, _a in rows}
    blocked = {d.email for d in await ts.list(DoNotContact, DoNotContact.email.in_(addresses),
                                              DoNotContact.lifted_at.is_(None))}
    return [Candidate(
        contact_id=c.id, full_name=c.full_name or "", title=c.title or "",
        seniority=c.seniority or "", email=c.email or "", email_status=c.email_status or "",
        account_id=a.id, account_name=a.name or "",
        blocked=(c.email or "").strip().lower() in blocked,
    ) for c, a in rows]


@dataclass(slots=True)
class TimelineEntry:
    message_id: str
    direction: str
    kind: str
    status: str
    subject: str
    preview: str
    at: object
    contact_id: str | None
    contact_name: str
    campaign_id: str | None
    campaign_name: str
    category: str | None


async def timeline(ts, *, contact_id: str | None = None, account_id: str | None = None,
                   limit: int = TIMELINE_LIMIT) -> list[TimelineEntry]:
    """What was sent and received, oldest first, across every campaign and one-off send.

    Drafts and queued rows are not conversation: only what left or arrived is shown.
    """
    from nexus.models.account import Contact
    from nexus.models.engagement import (
        EngagementCampaign,
        EngagementEnrollment,
        EngagementMessage,
        ReplyClassification,
    )

    if not contact_id and not account_id:
        return []
    if contact_id:
        people = {contact_id}
    else:
        people = {c.id for c in await ts.list(Contact, Contact.account_id == account_id)}
    if not people:
        return []
    messages = await ts.list(EngagementMessage, EngagementMessage.contact_id.in_(people),
                             EngagementMessage.status.in_(("sent", "received", "bounced")))
    messages.sort(key=lambda m: m.sent_at or m.received_at or m.created_at)
    messages = messages[-max(1, min(limit, TIMELINE_LIMIT)):]
    if not messages:
        return []

    enrollment_ids = {m.enrollment_id for m in messages if m.enrollment_id}
    enrollments = {e.id: e for e in await ts.list(
        EngagementEnrollment, EngagementEnrollment.id.in_(enrollment_ids))} if enrollment_ids else {}
    campaign_ids = {e.campaign_id for e in enrollments.values()}
    campaigns = {c.id: c for c in await ts.list(
        EngagementCampaign, EngagementCampaign.id.in_(campaign_ids))} if campaign_ids else {}
    names = {c.id: c.full_name or "" for c in await ts.list(Contact, Contact.id.in_(people))}
    readings = {r.message_id: r for r in await ts.list(
        ReplyClassification, ReplyClassification.message_id.in_([m.id for m in messages]))}

    out = []
    for m in messages:
        enrollment = enrollments.get(m.enrollment_id)
        campaign = campaigns.get(getattr(enrollment, "campaign_id", None))
        reading = readings.get(m.id)
        out.append(TimelineEntry(
            message_id=m.id, direction=m.direction, kind=m.kind, status=m.status,
            subject=m.subject or "", preview=" ".join((m.body_text or "").split())[:400],
            at=m.sent_at or m.received_at or m.created_at, contact_id=m.contact_id,
            contact_name=names.get(m.contact_id, ""),
            campaign_id=getattr(campaign, "id", None), campaign_name=getattr(campaign, "name", ""),
            category=(reading.corrected_category or reading.category) if reading else None,
        ))
    return out
