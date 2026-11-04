"""Today: where an SDR starts (spec §19, "Where do I start?").

One ordered list per SDR, built from what the engine already knows. The order is the order of cost
if left: a buyer who said yes and is waiting goes cold fastest, then replies only a person can
decide, then colleagues a reply paused, then calls due, then opening emails waiting for approval,
then people worth writing to again because something happened at their company, then the people
coming back today, who need nothing but are worth knowing about.

Within a kind, ranked by reply likelihood (§18.5), and by its BAND rather than its score: people in
one band stay longest-waiting first, which is what the reply-speed reminder measures, and a
hundredth of a point of fit does not jump someone the queue. Items with no one person behind them
(colleagues at an account, a campaign's review) keep their age order.

Scoped to the caller's own mailboxes and campaigns: this is a personal list, and a manager's team
view is the reply desk's toggle.

**Every due step is a task** (SDR round 2, W3.2, 2026-10-29). The Inbox is the one daily list, so
each item also says which of its sections it belongs to (`section`: calls, emails, replies), whether
it is due today or overdue (`due`, against the start of the SDR's own day), who it is about, and the
one action that clears it. The order above is unchanged, because the dashboard and the MCP overview
read it as "most urgent first"; the Inbox groups by section and puts today before overdue itself.

* Calls: every open call the SDR owns that is due by the end of today, overdue included. A call
  from a campaign step always; any other call (a dated callback, one queued by hand) once it has a
  due time, so an undated power list does not flood the day.
* Emails: opening emails waiting for approval, per campaign; a follow-up held for review (D17, or
  review every touch), per person, since nothing sends until the SDR approves it; and the
  re-engagements, signal-led (write again) and dated (comes back today).
* Replies: answers, decisions, and colleagues a reply paused.

An opening email and a held follow-up are told apart by `started_at`: approval starts a sequence,
so someone awaiting review who has never started is waiting on their opening email. Before this,
a held follow-up was counted as one more "opening email" in its campaign's review line.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

UTC = timezone.utc  # datetime.UTC is 3.11+; this project supports 3.10

#: The order kinds appear in, and what each one asks of the SDR.
KINDS = ("reply", "decide", "colleagues", "call", "followup", "review", "restart", "returning")
#: Which Inbox section each kind belongs to.
SECTION = {"reply": "replies", "decide": "replies", "colleagues": "replies", "call": "calls",
           "followup": "emails", "review": "emails", "restart": "emails", "returning": "emails"}
#: The one action that clears an item, as the Inbox's button reads it.
ACTION = {"reply": "Reply", "decide": "Decide", "colleagues": "Resume or stop", "call": "Call",
          "followup": "Review and send", "review": "Review and send", "restart": "Write again",
          "returning": "View"}
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
    #: Whose item this is: ranks it, and lets the Inbox open the person and account in place.
    contact_id: str | None = None
    account_id: str | None = None
    contact_name: str = ""
    account_name: str = ""
    #: The row the action is about: a call task, a reply, a message, an enrollment or a campaign.
    ref_id: str | None = None
    #: `today` or `overdue`, against the start of the SDR's own day. Filled in by `today`.
    due: str = "today"

    @property
    def section(self) -> str:
        return SECTION[self.kind]

    @property
    def action(self) -> str:
        return ACTION[self.kind]

    def as_dict(self) -> dict:
        out = asdict(self)
        out["section"], out["action"] = self.section, self.action
        return out


async def today(ts, *, user_id: str, now: datetime) -> list[TodayItem]:
    from sqlalchemy import or_

    from nexus.engagement.sequences.service import current_messages
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
    # A campaign's call steps always; any other call once it is dated, so an undated power list is
    # the Calls page's and not today's.
    open_calls = await ts.list(CallTask, CallTask.owner_user_id == user_id,
                               CallTask.status == "open",
                               or_(CallTask.engagement_enrollment_id.is_not(None),
                                   CallTask.due_at.is_not(None)))
    calls = [c for c in open_calls if c.due_at is None or _aware(c.due_at) < day_end]

    campaigns = await ts.list(EngagementCampaign, EngagementCampaign.owner_user_id == user_id,
                              EngagementCampaign.status.in_(("draft", "reviewing", "active")))
    waiting = await ts.list(EngagementEnrollment,
                            EngagementEnrollment.campaign_id.in_([c.id for c in campaigns]),
                            EngagementEnrollment.status == "awaiting_review") if campaigns else []
    # Approval starts a sequence: never started is an opening email, started is a held follow-up.
    openers = [e for e in waiting if e.started_at is None]
    held = [e for e in waiting if e.started_at is not None]

    contact_ids |= ({e.contact_id for e in returning} | {e.contact_id for e in held}
                    | {c.contact_id for c in calls if c.contact_id})
    contacts = {c.id: c for c in await ts.list(Contact, Contact.id.in_(contact_ids))} \
        if contact_ids else {}
    account_ids = ({e.account_id for e in paused} | {c.account_id for c in contacts.values()}
                   | {c.account_id for c in calls if c.account_id})
    accounts = {a.id: a for a in await ts.list(Account, Account.id.in_(account_ids))} \
        if account_ids else {}

    def who(contact_id: str | None) -> str:
        contact = contacts.get(contact_id)
        if contact is None:
            return "Someone"
        account = accounts.get(contact.account_id)
        return f"{contact.full_name} at {account.name}" if account else contact.full_name

    def person(contact_id: str | None, account_id: str | None = None) -> dict:
        """The fields that let the Inbox open the person and the account in place."""
        contact = contacts.get(contact_id)
        account_id = getattr(contact, "account_id", None) or account_id
        return {"contact_id": contact_id if contact is not None else None,
                "account_id": account_id,
                "contact_name": getattr(contact, "full_name", "") or "",
                "account_name": getattr(accounts.get(account_id), "name", "") or ""}

    def arrived(r) -> datetime | None:
        m = messages.get(r.message_id)
        return (m.received_at if m else None) or r.created_at

    for r in sorted(open_replies, key=lambda r: arrived(r) or now):
        category = r.corrected_category or r.category
        if category == "unclear":
            items.append(TodayItem("decide", f"Decide on {who(r.contact_id)}",
                                   "Their reply needs a person to say what happens next.",
                                   f"/engagement/mail?folder=needs_reply&reply={r.id}", arrived(r),
                                   ref_id=r.id, **person(r.contact_id, r.account_id)))
        else:
            said = {"interested": "They're interested.", "question": "They asked a question.",
                    "referral": "They pointed you to someone else."}.get(category, "They replied.")
            items.append(TodayItem("reply", f"Answer {who(r.contact_id)}", said,
                                   f"/engagement/mail?folder=needs_reply&reply={r.id}", arrived(r),
                                   ref_id=r.id, **person(r.contact_id, r.account_id)))

    by_account: dict[str, list] = {}
    for e in paused:
        by_account.setdefault(e.account_id, []).append(e)
    for account_id, rows in by_account.items():
        name = getattr(accounts.get(account_id), "name", "an account")
        items.append(TodayItem(
            "colleagues", f"Resume or stop {len(rows)} at {name}",
            "A colleague replied, so they are paused until you decide.",
            "/engagement/mail?folder=needs_reply", min(e.updated_at or e.created_at for e in rows),
            count=len(rows), account_id=account_id, account_name=name))

    for c in sorted(calls, key=lambda c: _aware(c.due_at) or now):
        items.append(TodayItem("call", f"Call {who(c.contact_id)}", c.reason or "A call step is due.",
                               f"/calls?task={c.id}", c.due_at, ref_id=c.id,
                               **person(c.contact_id, c.account_id)))

    names = {c.id: c.name for c in campaigns}
    drafts = await current_messages(ts, held) if held else {}
    for e in sorted(held, key=lambda e: _aware(e.next_action_at) or now):
        row = drafts.get(e.id)
        problems = [p for p in (getattr(row, "quality_problems", None) or []) if p]
        why = (f"Held because it failed a check: {', '.join(problems[:2])}." if problems
               else "Nothing sends until you approve it.")
        items.append(TodayItem(
            "followup", f"Review the follow-up to {who(e.contact_id)}",
            f"Step {e.current_step_index + 1} of {names.get(e.campaign_id, 'a campaign')}. {why}",
            f"/engagement/campaigns/{e.campaign_id}?tab=review", e.next_action_at,
            ref_id=getattr(row, "id", None) or e.id, **person(e.contact_id, e.account_id)))

    per_campaign: dict[str, list] = {}
    for e in openers:
        per_campaign.setdefault(e.campaign_id, []).append(e)
    for campaign in campaigns:
        rows = per_campaign.get(campaign.id, [])
        if rows:
            n = len(rows)
            items.append(TodayItem(
                "review", f"Review {n} opening {'email' if n == 1 else 'emails'} in {campaign.name}",
                "Nothing sends until you approve it.",
                f"/engagement/campaigns/{campaign.id}?tab=review",
                min(_aware(e.created_at) for e in rows), count=n, ref_id=campaign.id))

    restart = await _worth_writing_again(ts, user_id=user_id, now=now)
    if restart:
        n = len(restart)
        items.append(TodayItem(
            "restart", f"Write again to {n} {'person' if n == 1 else 'people'}",
            "Something happened at their company since they went quiet or asked for later.",
            "/engagement/mail?folder=write_again", restart[0].signal_at, count=n, due="today"))

    for e in sorted(returning, key=lambda e: _aware(e.snoozed_until)):
        items.append(TodayItem("returning", f"{who(e.contact_id)} comes back today",
                               "Their sequence resumes on its own.", "/engagement/mail?folder=scheduled",
                               e.snoozed_until, ref_id=e.id, **person(e.contact_id, e.account_id)))

    for item in items:
        # Overdue is anything that has waited since before the SDR's day began. A suggestion and a
        # person coming back today are today's by what they are.
        if item.kind not in ("restart", "returning") and item.at is not None \
                and _aware(item.at) < day_start:
            item.due = "overdue"

    bands = await _likelihood_bands(ts, [i.contact_id for i in items if i.contact_id], now)
    order = {kind: i for i, kind in enumerate(KINDS)}
    # A stable sort: within one kind and one band, the age order built above survives.
    return sorted(items, key=lambda i: (order[i.kind],
                                        _BAND_RANK[bands.get(i.contact_id or "", "unknown")]))


async def _worth_writing_again(ts, *, user_id: str, now: datetime) -> list:
    """Signal re-engagement suggestions (§19). A suggestion is optional, so failing to build them
    leaves them off the list rather than taking the list down."""
    from nexus.engagement.enhancements.signal_reengage import suggestions

    try:
        return await suggestions(ts, user_id=user_id, now=now)
    except Exception:  # noqa: BLE001 - optional line on a list that must load
        return []


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
