"""Signal re-engagement (spec §19): when something happens at a company, suggest getting back in
touch with the people there who went quiet, or who asked for later.

**A suggestion, never a send** (D22). The SDR sees who and why, reads a drafted email in the same
thread, edits it, and presses Send.

Who is suggested, all of it decided here and nowhere else:

* A sequence that **finished with no reply** (`completed`), or a **"later" still more than two weeks
  from its date**. Nearer than that the re-engagement they asked for is about to go anyway, and two
  emails in a fortnight is one too many.
* **Never someone who declined or unsubscribed** (their enrollments are `stopped`, which is not
  eligible), never anyone on do-not-contact, never anyone emailed in the last 14 days, and never
  anyone another campaign is already going to email (a live enrollment elsewhere).
* One suggestion per person: their account's strongest recent signal. A signal counts when it is of
  a kind an SDR would open with, at or above the alert floor, and happened **after** the sequence
  ended (or after they asked for later) and within the last 14 days. News from before they went
  quiet is not a reason to write now.
* **Once per signal.** The send's idempotency key is the enrollment and the signal, so a second
  press, a retry or a colleague on the same screen cannot send it twice.

Ranked by reply likelihood band (phase 13), then by how recent the signal is.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

RELEVANT = ("funding", "hiring", "job_posting", "tech_install", "job_switch", "news",
            "website_change", "web_visit")
WINDOW = timedelta(days=14)
LATER_MARGIN = timedelta(days=14)
MAX_SUGGESTIONS = 20
#: Enrollments that will email the person on their own; a suggestion would be a second voice.
LIVE = ("active", "awaiting_review", "paused", "snoozed")
_BAND_RANK = {"high": 0, "medium": 1, "low": 2, "unknown": 3}


def idempotency_key(enrollment_id: str, signal_id: str) -> str:
    return f"signal:{enrollment_id}:{signal_id}"


@dataclass(slots=True)
class Suggestion:
    enrollment_id: str
    contact_id: str
    contact_name: str
    account_id: str
    account_name: str
    campaign_id: str
    campaign_name: str
    reason: str               # quiet | later
    signal_id: str
    signal_kind: str
    signal_title: str
    signal_at: datetime
    likelihood: str

    def as_dict(self) -> dict:
        return asdict(self)


def _aware(moment: datetime | None) -> datetime | None:
    from datetime import UTC

    if moment is None or moment.tzinfo is not None:
        return moment
    return moment.replace(tzinfo=UTC)


async def suggestions(ts, *, user_id: str, now: datetime) -> list[Suggestion]:
    from nexus.core.config import get_settings
    from nexus.engagement.insights.service import for_contacts
    from nexus.engagement.suppression.service import active_block
    from nexus.models.account import Account, Contact
    from nexus.models.engagement import (
        EngagementCampaign,
        EngagementEnrollment,
        EngagementMessage,
        MailboxConnection,
    )
    from nexus.models.signal import SignalEvent

    mailbox_ids = [m.id for m in await ts.list(MailboxConnection,
                                               MailboxConnection.owner_user_id == user_id)]
    if not mailbox_ids:
        return []
    rows = await ts.list(EngagementEnrollment,
                         EngagementEnrollment.mailbox_connection_id.in_(mailbox_ids),
                         EngagementEnrollment.status.in_(("completed", "snoozed")))
    later = [e for e in rows if e.status == "snoozed" and e.status_reason == "later"
             and e.snoozed_until is not None and _aware(e.snoozed_until) > now + LATER_MARGIN]
    # "After they asked for later" is measured from what they wrote, not from `updated_at`, which
    # any edit to the enrollment moves.
    heard: dict[str, datetime] = {}
    if later:
        for m in await ts.list(EngagementMessage, EngagementMessage.direction == "in",
                               EngagementMessage.contact_id.in_({e.contact_id for e in later})):
            at = _aware(m.received_at or m.created_at)
            if m.contact_id not in heard or at > heard[m.contact_id]:
                heard[m.contact_id] = at
    eligible = [(e, "quiet", _aware(e.finished_at)) for e in rows if e.status == "completed"]
    eligible += [(e, "later", heard.get(e.contact_id)) for e in later]
    if not eligible:
        return []

    since = now - WINDOW
    account_ids = {e.account_id for e, _, _ in eligible}
    signals = await ts.list(SignalEvent, SignalEvent.account_id.in_(account_ids),
                            SignalEvent.kind.in_(RELEVANT),
                            SignalEvent.strength >= get_settings().signal_alert_floor,
                            SignalEvent.occurred_at >= since)
    if not signals:
        return []
    by_account: dict[str, list] = {}
    for s in signals:
        by_account.setdefault(s.account_id, []).append(s)
    contact_ids = {e.contact_id for e, _, _ in eligible}
    recently = {m.contact_id for m in await ts.list(
        EngagementMessage, EngagementMessage.contact_id.in_(contact_ids),
        EngagementMessage.direction == "out", EngagementMessage.status == "sent",
        EngagementMessage.sent_at >= since)}
    # Someone another campaign is already about to email is not someone to write to again.
    eligible_ids = {e.id for e, _, _ in eligible}
    recently |= {e.contact_id for e in await ts.list(
        EngagementEnrollment, EngagementEnrollment.contact_id.in_(contact_ids),
        EngagementEnrollment.status.in_(LIVE)) if e.id not in eligible_ids}
    contacts = {c.id: c for c in await ts.list(Contact, Contact.id.in_(contact_ids))}
    accounts = {a.id: a for a in await ts.list(Account, Account.id.in_(account_ids))}
    campaigns = {c.id: c for c in await ts.list(
        EngagementCampaign,
        EngagementCampaign.id.in_({e.campaign_id for e, _, _ in eligible}))}

    picked: list[tuple] = []
    for enrollment, reason, anchor in eligible:
        contact = contacts.get(enrollment.contact_id)
        if contact is None or getattr(contact, "deleted_at", None) is not None \
                or not (contact.email or "").strip() or contact.id in recently:
            continue
        fresh = [s for s in by_account.get(enrollment.account_id, [])
                 if anchor is None or _aware(s.occurred_at) > anchor]
        if not fresh:
            continue
        if await active_block(ts, contact.email) is not None:
            continue
        best = max(fresh, key=lambda s: (s.strength, _aware(s.occurred_at)))
        picked.append((enrollment, reason, best, contact))
    if not picked:
        return []

    bands = {i.contact_id: i.likelihood.band for i in await for_contacts(
        ts, [contact.id for _, _, _, contact in picked], now=now)}
    out = [Suggestion(
        enrollment_id=e.id, contact_id=contact.id, contact_name=contact.full_name,
        account_id=e.account_id, account_name=getattr(accounts.get(e.account_id), "name", ""),
        campaign_id=e.campaign_id, campaign_name=getattr(campaigns.get(e.campaign_id), "name", ""),
        reason=reason, signal_id=s.id, signal_kind=s.kind, signal_title=s.title,
        signal_at=_aware(s.occurred_at), likelihood=bands.get(contact.id, "unknown"))
        for e, reason, s, contact in picked]
    out.sort(key=lambda x: (_BAND_RANK.get(x.likelihood, 3), -x.signal_at.timestamp()))
    return out[:MAX_SUGGESTIONS]


class RestartError(ValueError):
    """Why this suggestion cannot be acted on now."""


async def _load(ts, *, user_id: str, enrollment_id: str, signal_id: str, now: datetime):
    """The suggestion, re-derived: a stale screen must not act on someone no longer eligible."""
    from nexus.models.account import Account, Contact
    from nexus.models.engagement import EngagementEnrollment, EngagementThread, MailboxConnection
    from nexus.models.signal import SignalEvent

    current = {(s.enrollment_id, s.signal_id) for s in await suggestions(ts, user_id=user_id,
                                                                         now=now)}
    if (enrollment_id, signal_id) not in current:
        raise RestartError("This suggestion no longer applies: they may have been emailed, "
                           "replied, or asked not to be contacted.")
    enrollment = await ts.get(EngagementEnrollment, enrollment_id)
    signal = await ts.get(SignalEvent, signal_id)
    contact = await ts.get(Contact, enrollment.contact_id)
    account = await ts.get(Account, enrollment.account_id)
    mailbox = await ts.get(MailboxConnection, enrollment.mailbox_connection_id)
    thread = await ts.get(EngagementThread, enrollment.current_thread_id) \
        if enrollment.current_thread_id else None
    return enrollment, signal, contact, account, mailbox, thread


@dataclass(slots=True)
class _Angle:
    angle: str


async def draft(ts, *, user_id: str, enrollment_id: str, signal_id: str, now: datetime) -> dict:
    """An email in the same thread that opens with the news. Charged as `ai.email_draft`."""
    from nexus.engagement.drafting.drafter import draft as write

    enrollment, signal, contact, account, mailbox, thread = await _load(
        ts, user_id=user_id, enrollment_id=enrollment_id, signal_id=signal_id, now=now)
    written = await write(ts, enrollment=enrollment, contact=contact, account=account,
                          mailbox=mailbox, step=_Angle(f"The news: {signal.title}"),
                          kind="signal", thread=thread, user_id=user_id, now=now)
    if not written.ok:
        raise RestartError(f"The email could not be drafted: {written.error}")
    return {"subject": written.subject, "body": written.body,
            "quality_problems": written.problems}


async def send(ts, *, user_id: str, enrollment_id: str, signal_id: str, subject: str, body: str,
               now: datetime):
    """Send the SDR's text in the same thread. The enrollment itself does not restart: this is one
    email, and a reply to it reaches the desk like any other."""
    from nexus.engagement.sending.service import send as deliver

    if not subject.strip() or not body.strip():
        raise RestartError("Write a subject and a message first.")
    enrollment, signal, contact, _account, mailbox, thread = await _load(
        ts, user_id=user_id, enrollment_id=enrollment_id, signal_id=signal_id, now=now)
    return await deliver(ts, mailbox=mailbox, contact=contact, subject=subject.strip(),
                         body=body.strip(), thread=thread, kind="reengage", user_id=user_id,
                         idempotency_key=idempotency_key(enrollment.id, signal.id),
                         context={"signal_id": signal.id, "enrollment_id": enrollment.id})
