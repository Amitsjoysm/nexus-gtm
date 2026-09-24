"""Today: where an SDR starts (spec §19, "Where do I start?").

One ordered list per SDR, built from what the engine already knows. The order is the order of cost
if left: a buyer who said yes and is waiting goes cold fastest, then replies only a person can
decide, then colleagues a reply paused, then calls due, then opening emails waiting for approval,
then the people coming back today, who need nothing but are worth knowing about.

Within a kind, ranked by reply likelihood (§18.5), and by its BAND rather than its score: people in
one band stay longest-waiting first, which is what the reply-speed reminder measures, and a
hundredth of a point of fit does not jump someone the queue. Items with no one person behind them
(colleagues at an account, a campaign's review) keep their age order.

Scoped to the caller's own mailboxes and campaigns: this is a personal list, and a manager's team
view is the reply desk's toggle.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

#: The order kinds appear in, and what each one asks of the SDR.
KINDS = ("reply", "decide", "colleagues", "call", "review", "returning")
#: Likelihood bands, likeliest first. `unknown` (nothing to go on) ranks with the unrated items.
_BAND_RANK = {"high": 0, "medium": 1, "low": 2, "unknown": 3}


def _aware(moment: datetime | None) -> datetime | None:
    """`call_tasks.due_at` is a plain timezone column, which SQLite hands back naive."""
    if moment is None or moment.tzinfo is not None:
        return moment
    return moment.replace(tzinfo=UTC)


@dataclass(slots=True)
class TodayItem:
    kind: str
    title: str
    detail: str
    link: str
    at: datetime | None
    count: int = 1
    #: Whose item this is, for ranking; not part of what the screen receives.
    contact_id: str | None = None

    def as_dict(self) -> dict:
        out = asdict(self)
        out.pop("contact_id")
        return out


async def today(ts, *, user_id: str, now: datetime) -> list[TodayItem]:
    from nexus.engagement.timekeeping import local_day_start, zone_or_none
    from nexus.models.account import Account, Contact
    from nexus.models.calling import CallTask
    from nexus.models.engagement import (
        EngagementCampaign,
        EngagementEnrollment,
        EngagementMessage,
        MailboxConnection,
        ReplyClassification,
    )

    mailboxes = await ts.list(MailboxConnection, MailboxConnection.owner_user_id == user_id)
    mailbox_ids = [m.id for m in mailboxes]
    zone = zone_or_none(mailboxes[0].timezone if mailboxes else None) or UTC
    day_start = local_day_start(now, zone)
    day_end = day_start + timedelta(days=1)
    items: list[TodayItem] = []

    open_replies = await ts.list(ReplyClassification,
                                 ReplyClassification.mailbox_connection_id.in_(mailbox_ids),
                                 ReplyClassification.status == "open") if mailbox_ids else []
    contact_ids = {r.contact_id for r in open_replies if r.contact_id}
    messages = {m.id: m for m in await ts.list(
        EngagementMessage, EngagementMessage.id.in_([r.message_id for r in open_replies]))} \
        if open_replies else {}

    paused = await ts.list(EngagementEnrollment,
                           EngagementEnrollment.mailbox_connection_id.in_(mailbox_ids),
                           EngagementEnrollment.status == "paused",
                           EngagementEnrollment.status_reason == "colleague_replied") \
        if mailbox_ids else []
    dated = await ts.list(EngagementEnrollment,
                          EngagementEnrollment.mailbox_connection_id.in_(mailbox_ids),
                          EngagementEnrollment.snoozed_until.is_not(None)) if mailbox_ids else []
    returning = [e for e in dated if e.status in ("snoozed", "paused")
                 and day_start <= _aware(e.snoozed_until) < day_end]
    open_calls = await ts.list(CallTask, CallTask.owner_user_id == user_id,
                               CallTask.engagement_enrollment_id.is_not(None),
                               CallTask.status == "open")
    calls = [c for c in open_calls if c.due_at is None or _aware(c.due_at) < day_end]
    contact_ids |= {e.contact_id for e in returning} | {c.contact_id for c in calls if c.contact_id}
    contacts = {c.id: c for c in await ts.list(Contact, Contact.id.in_(contact_ids))} \
        if contact_ids else {}
    account_ids = {e.account_id for e in paused} | {c.account_id for c in contacts.values()}
    accounts = {a.id: a for a in await ts.list(Account, Account.id.in_(account_ids))} \
        if account_ids else {}

    def who(contact_id: str | None) -> str:
        contact = contacts.get(contact_id)
        if contact is None:
            return "Someone"
        account = accounts.get(contact.account_id)
        return f"{contact.full_name} at {account.name}" if account else contact.full_name

    def arrived(r) -> datetime | None:
        m = messages.get(r.message_id)
        return (m.received_at if m else None) or r.created_at

    for r in sorted(open_replies, key=lambda r: arrived(r) or now):
        category = r.corrected_category or r.category
        if category == "unclear":
            items.append(TodayItem("decide", f"Decide on {who(r.contact_id)}",
                                   "Their reply needs a person to say what happens next.",
                                   f"/engagement/replies?reply={r.id}", arrived(r),
                                   contact_id=r.contact_id))
        else:
            said = {"interested": "They're interested.", "question": "They asked a question.",
                    "referral": "They pointed you to someone else."}.get(category, "They replied.")
            items.append(TodayItem("reply", f"Answer {who(r.contact_id)}", said,
                                   f"/engagement/replies?reply={r.id}", arrived(r),
                                   contact_id=r.contact_id))

    by_account: dict[str, list] = {}
    for e in paused:
        by_account.setdefault(e.account_id, []).append(e)
    for account_id, rows in by_account.items():
        name = getattr(accounts.get(account_id), "name", "an account")
        items.append(TodayItem(
            "colleagues", f"Resume or stop {len(rows)} at {name}",
            "A colleague replied, so they are paused until you decide.",
            "/engagement/replies", min(e.updated_at or e.created_at for e in rows),
            count=len(rows)))

    for c in sorted(calls, key=lambda c: _aware(c.due_at) or now):
        items.append(TodayItem("call", f"Call {who(c.contact_id)}", c.reason or "A call step is due.",
                               "/calls", c.due_at, contact_id=c.contact_id))

    campaigns = await ts.list(EngagementCampaign, EngagementCampaign.owner_user_id == user_id,
                              EngagementCampaign.status.in_(("draft", "reviewing", "active")))
    if campaigns:
        waiting = await ts.list(EngagementEnrollment,
                                EngagementEnrollment.campaign_id.in_([c.id for c in campaigns]),
                                EngagementEnrollment.status == "awaiting_review")
        per_campaign: dict[str, int] = {}
        for e in waiting:
            per_campaign[e.campaign_id] = per_campaign.get(e.campaign_id, 0) + 1
        for campaign in campaigns:
            n = per_campaign.get(campaign.id, 0)
            if n:
                items.append(TodayItem(
                    "review", f"Review {n} opening {'email' if n == 1 else 'emails'} in {campaign.name}",
                    "Nothing sends until you approve it.",
                    f"/engagement/campaigns/{campaign.id}", campaign.created_at, count=n))

    for e in sorted(returning, key=lambda e: _aware(e.snoozed_until)):
        items.append(TodayItem("returning", f"{who(e.contact_id)} comes back today",
                               "Their sequence resumes on its own.", "/engagement/replies?tab=scheduled",
                               e.snoozed_until, contact_id=e.contact_id))

    bands = await _likelihood_bands(ts, [i.contact_id for i in items if i.contact_id], now)
    order = {kind: i for i, kind in enumerate(KINDS)}
    # A stable sort: within one kind and one band, the age order built above survives.
    return sorted(items, key=lambda i: (order[i.kind],
                                        _BAND_RANK[bands.get(i.contact_id or "", "unknown")]))


async def _likelihood_bands(ts, contact_ids: list[str], now: datetime) -> dict[str, str]:
    """Each person's reply-likelihood band. The Today plan must load even when insights cannot:
    a failure ranks everyone `unknown`, which is the age order."""
    from nexus.engagement.insights.service import for_contacts

    if not contact_ids:
        return {}
    try:
        insights = await for_contacts(ts, contact_ids, now=now)
    except Exception:  # noqa: BLE001 - ranking is a nicety; the list is the product
        return {}
    return {i.contact_id: i.likelihood.band for i in insights}
