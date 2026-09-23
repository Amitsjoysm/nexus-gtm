"""Which conversation an inbound email belongs to — or none, in which case it is not stored (§6).

The first rule that matches wins:

1. **Known provider thread** → that conversation.
2. **Reply headers** (`In-Reply-To` / `References`) name one of our `Message-ID`s → that
   conversation. Catches clients that break provider threading, and replies to forwards.
3. **Same person, different thread** — the sender is a contact with an enrollment → their most
   recent conversation; effects apply to every live enrollment for that person.
4. **Colleague** — the sender's domain is an account with a live enrollment → `referral` by default,
   and the colleagues' sequences pause (D3).
5. **No match** → discarded without storing a subject or a body. The SDR's mailbox is theirs; only
   mail that belongs to our conversations or known contacts is kept (privacy filter).
"""
from __future__ import annotations

from dataclasses import dataclass, field

LIVE = ("active", "awaiting_review", "paused", "snoozed")
#: Webmail domains are never a company: a Gmail address is not a colleague of every Gmail address.
FREE_MAIL = frozenset({
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "yahoo.com",
    "icloud.com", "me.com", "aol.com", "proton.me", "protonmail.com", "gmx.com", "gmx.de",
    "yandex.com", "zoho.com", "mail.com",
})


@dataclass(slots=True)
class Match:
    rule: str                       # thread | headers | person | colleague | none
    thread: object | None = None
    contact: object | None = None
    account: object | None = None
    answered: object | None = None  # our outbound message this replies to, when known
    enrollments: list = field(default_factory=list)       # this person's live enrollments
    colleague_enrollments: list = field(default_factory=list)  # others at the same company

    @property
    def stored(self) -> bool:
        return self.rule != "none"


def domain_of(address: str) -> str:
    return (address or "").rsplit("@", 1)[-1].strip().lower() if "@" in (address or "") else ""


async def match(ts, *, mailbox, parsed, provider_thread_id: str) -> Match:
    from nexus.models.account import Account, Contact
    from nexus.models.engagement import EngagementMessage, EngagementThread

    thread = await ts.first(EngagementThread,
                            EngagementThread.mailbox_connection_id == mailbox.id,
                            EngagementThread.provider_thread_id == provider_thread_id) \
        if provider_thread_id else None
    rule = "thread" if thread is not None else ""
    answered = None
    for rfc_id in parsed.replied_to_ids:
        answered = await ts.first(EngagementMessage, EngagementMessage.rfc_message_id == rfc_id,
                                  EngagementMessage.direction == "out")
        if answered is not None:
            break
    if thread is None and answered is not None and answered.thread_id:
        thread = await ts.get(EngagementThread, answered.thread_id)
        rule = "headers"

    contact = None
    if thread is not None and thread.contact_id:
        contact = await ts.get(Contact, thread.contact_id)
    if contact is None and parsed.from_addr:
        contact = await _contact_by_email(ts, parsed.from_addr)
    if thread is None and contact is not None and await _has_enrollment(ts, contact.id):
        thread = await _latest_thread(ts, contact.id)
        rule = "person"

    if rule:
        account = await ts.get(Account, contact.account_id) if contact is not None else None
        if answered is None and thread is not None:
            answered = await _latest_outbound(ts, thread.id)
        return Match(rule=rule, thread=thread, contact=contact, account=account,
                     answered=answered,
                     enrollments=await _live_enrollments(ts, contact_id=getattr(contact, "id", None)),
                     colleague_enrollments=await _colleagues(ts, contact, account))

    domain = domain_of(parsed.from_addr)
    if domain and domain not in FREE_MAIL:
        account = await ts.first(Account, Account.domain == domain)
        if account is not None:
            colleagues = await _live_enrollments(ts, account_id=account.id)
            if colleagues:
                return Match(rule="colleague", contact=contact, account=account,
                             thread=await _latest_thread_for_account(ts, account.id),
                             colleague_enrollments=colleagues)
    return Match(rule="none")


async def _contact_by_email(ts, address: str):
    from sqlalchemy import func

    from nexus.models.account import Contact

    return await ts.first(Contact, func.lower(Contact.email) == address.lower())


async def _has_enrollment(ts, contact_id: str) -> bool:
    from nexus.models.engagement import EngagementEnrollment

    return await ts.first(EngagementEnrollment,
                          EngagementEnrollment.contact_id == contact_id) is not None


async def _latest_thread(ts, contact_id: str):
    from nexus.models.engagement import EngagementThread

    rows = await ts.session.scalars(
        ts.select(EngagementThread, EngagementThread.contact_id == contact_id)
        .order_by(EngagementThread.last_message_at.desc()).limit(1))
    return rows.first()


async def _latest_thread_for_account(ts, account_id: str):
    from nexus.models.engagement import EngagementThread

    rows = await ts.session.scalars(
        ts.select(EngagementThread, EngagementThread.account_id == account_id)
        .order_by(EngagementThread.last_message_at.desc()).limit(1))
    return rows.first()


async def _latest_outbound(ts, thread_id: str):
    from nexus.models.engagement import EngagementMessage

    rows = await ts.session.scalars(
        ts.select(EngagementMessage, EngagementMessage.thread_id == thread_id,
                  EngagementMessage.direction == "out", EngagementMessage.status == "sent")
        .order_by(EngagementMessage.sent_at.desc()).limit(1))
    return rows.first()


async def _live_enrollments(ts, *, contact_id: str | None = None, account_id: str | None = None):
    from nexus.models.engagement import EngagementEnrollment

    where = [EngagementEnrollment.status.in_(LIVE)]
    if contact_id:
        where.append(EngagementEnrollment.contact_id == contact_id)
    elif account_id:
        where.append(EngagementEnrollment.account_id == account_id)
    else:
        return []
    return await ts.list(EngagementEnrollment, *where)


async def _colleagues(ts, contact, account) -> list:
    """Live enrollments of OTHER people at the same company — the ones a reply pauses (D3)."""
    if account is None:
        return []
    return [e for e in await _live_enrollments(ts, account_id=account.id)
            if contact is None or e.contact_id != contact.id]
