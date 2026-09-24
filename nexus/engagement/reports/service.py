"""What a campaign achieved, and how fast the team answers (spec §11).

**Everything is counted in people, not messages.** "Replied" is the number of people who wrote back
at least once, not the number of emails they wrote: a buyer who replies three times is one reply to
the campaign, and counting messages would let a talkative prospect make a weak campaign look good.
The one per-message figure is "sent" per step, because a step is sent once per person anyway.

The funnel, in order: contacts → sent → bounced → replied → positive → meetings. Positive is the
ledger's own `POSITIVE_CATEGORIES` (interested, question, referral), read from the SDR's correction
when there is one, so the report agrees with what a person decided the reply meant.

**A reply belongs to the last step sent before it.** Reply rate per step is replies attributed that
way over people the step reached. Attributing to the first step would credit the opening email with
every reply a follow-up earned, which is the question the per-step report exists to answer.

**Time to first response is in business hours** (`desk.service.business_hours_between`, in the
mailbox's own zone), the same clock the reply-speed reminder runs on. A reply that arrived on
Friday evening and was answered on Monday morning was answered promptly.

Bounded: a campaign's rows are read once and grouped here, in a fixed number of queries whatever its
size, and response times read a window (30 days by default).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from statistics import median

RESPONSE_WINDOW_DAYS = 30
NEEDS_AN_ANSWER = ("interested", "question", "referral")


@dataclass(slots=True)
class StepResult:
    step_index: int
    channel: str
    sent: int = 0
    replies: int = 0

    @property
    def reply_rate(self) -> float:
        return round(self.replies / self.sent, 4) if self.sent else 0.0


@dataclass(slots=True)
class CampaignReport:
    contacts: int = 0
    sent: int = 0
    bounced: int = 0
    replied: int = 0
    positive: int = 0
    meetings: int = 0
    steps: list[StepResult] = field(default_factory=list)
    categories: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        out = asdict(self)
        out["steps"] = [{**asdict(s), "reply_rate": s.reply_rate} for s in self.steps]
        out["reply_rate"] = round(self.replied / self.sent, 4) if self.sent else 0.0
        out["positive_rate"] = round(self.positive / self.sent, 4) if self.sent else 0.0
        return out


def _when(message) -> datetime | None:
    return message.sent_at or message.received_at or message.created_at


async def campaign_report(ts, campaign) -> CampaignReport:
    from nexus.engagement.ledger.payloads import POSITIVE_CATEGORIES
    from nexus.engagement.sequences.service import steps_of
    from sqlalchemy import or_

    from nexus.models.engagement import (
        EngagementEnrollment,
        EngagementMessage,
        EngagementThread,
        ReplyClassification,
    )

    report = CampaignReport()
    steps = await steps_of(ts, campaign)
    report.steps = [StepResult(step_index=s.step_index, channel=s.channel) for s in steps]
    by_index = {s.step_index: s for s in report.steps}

    enrollments = await ts.list(EngagementEnrollment,
                                EngagementEnrollment.campaign_id == campaign.id)
    report.contacts = len(enrollments)
    if not enrollments:
        return report
    ids = [e.id for e in enrollments]
    # A reply only attaches to a LIVE enrollment, so a second reply after the first one stopped the
    # sequence carries none. The thread still remembers the enrollment that started it.
    thread_owner = {t.id: t.enrollment_id for t in await ts.list(
        EngagementThread, EngagementThread.enrollment_id.in_(ids))}
    messages = await ts.list(EngagementMessage, or_(
        EngagementMessage.enrollment_id.in_(ids),
        EngagementMessage.thread_id.in_(list(thread_owner)) if thread_owner else False))
    person_of = {m.id: (m.enrollment_id or thread_owner.get(m.thread_id)) for m in messages}
    inbound_ids = [m.id for m in messages if m.direction == "in"]
    readings = {r.message_id: r for r in await ts.list(
        ReplyClassification, ReplyClassification.message_id.in_(inbound_ids))}         if inbound_ids else {}

    sent_people: set[str] = set()
    bounced_people: set[str] = set()
    replied_people: set[str] = set()
    positive_people: set[str] = set()
    meeting_people: set[str] = set()
    outbound_by_person: dict[str, list] = {}
    first_reply: dict[str, object] = {}

    for m in messages:
        person = person_of[m.id]
        if person is None:
            continue
        if m.direction == "out" and m.status in ("sent", "bounced"):
            sent_people.add(person)
            outbound_by_person.setdefault(person, []).append(m)
            if m.step_index is not None and m.step_index in by_index and m.kind == "step":
                by_index[m.step_index].sent += 1
            if m.status == "bounced":
                bounced_people.add(person)
        elif m.direction == "in" and m.inbound_kind == "human":
            replied_people.add(person)
            earlier = first_reply.get(person)
            if earlier is None or _when(m) < _when(earlier):
                first_reply[person] = m
    for e in enrollments:
        if e.status_reason == "bounced":
            bounced_people.add(e.id)

    for reading in readings.values():
        person = person_of.get(reading.message_id)
        category = reading.corrected_category or reading.category
        report.categories[category] = report.categories.get(category, 0) + 1
        if category in POSITIVE_CATEGORIES:
            positive_people.add(person)
        if reading.decision == "meeting":
            meeting_people.add(person)

    # Each person's first reply goes to the last step they were sent before it.
    for person, reply in first_reply.items():
        before = [m for m in outbound_by_person.get(person, [])
                  if m.step_index is not None and _when(m) and _when(m) <= _when(reply)]
        if before:
            step = max(before, key=_when).step_index
            if step in by_index:
                by_index[step].replies += 1

    report.sent = len(sent_people)
    report.bounced = len(bounced_people)
    report.replied = len(replied_people)
    report.positive = len(positive_people)
    report.meetings = len(meeting_people)
    report.categories = dict(sorted(report.categories.items(), key=lambda kv: -kv[1]))
    return report


@dataclass(slots=True)
class ResponseTime:
    user_id: str
    name: str
    answered: int
    waiting: int
    median_hours: float | None
    p90_hours: float | None


def _p90(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(0.9 * (len(ordered) - 1))))]


async def response_times(ts, *, user_id: str, team: bool, now: datetime,
                         days: int = RESPONSE_WINDOW_DAYS) -> list[ResponseTime]:
    """Per mailbox owner: how many replies that needed an answer were answered, how many still wait,
    and the median and 90th percentile business hours to the first answer, over the window."""
    from sqlalchemy import select

    from nexus.engagement.desk.service import business_hours_between
    from nexus.engagement.timekeeping import zone_or_none
    from nexus.models.engagement import EngagementMessage, MailboxConnection, ReplyClassification
    from nexus.models.identity import User

    where = [] if team else [MailboxConnection.owner_user_id == user_id]
    mailboxes = {m.id: m for m in await ts.list(MailboxConnection, *where)}
    if not mailboxes:
        return []
    since = now - timedelta(days=days)
    readings = [r for r in await ts.list(
        ReplyClassification, ReplyClassification.mailbox_connection_id.in_(list(mailboxes)))
        if (r.corrected_category or r.category) in NEEDS_AN_ANSWER]
    if not readings:
        return []
    inbound = {m.id: m for m in await ts.list(
        EngagementMessage, EngagementMessage.id.in_([r.message_id for r in readings]))}
    readings = [r for r in readings
                if inbound.get(r.message_id) and (_when(inbound[r.message_id]) or now) >= since]
    threads = {m.thread_id for m in inbound.values() if m.thread_id}
    answers = await ts.list(EngagementMessage, EngagementMessage.thread_id.in_(threads),
                            EngagementMessage.direction == "out",
                            EngagementMessage.kind == "response",
                            EngagementMessage.status == "sent") if threads else []

    per_owner: dict[str, dict] = {}
    for r in readings:
        mailbox = mailboxes[r.mailbox_connection_id]
        owner = per_owner.setdefault(mailbox.owner_user_id, {"hours": [], "waiting": 0})
        message = inbound[r.message_id]
        arrived = _when(message)
        answer = min((a for a in answers if a.thread_id == message.thread_id
                      and a.sent_at and arrived and a.sent_at >= arrived),
                     key=lambda a: a.sent_at, default=None)
        if answer is None:
            owner["waiting"] += 1
            continue
        zone = zone_or_none(mailbox.timezone) or UTC
        owner["hours"].append(round(business_hours_between(arrived, answer.sent_at, zone), 2))

    # Users are not tenant-scoped rows, so they are read by id: the ids came from this tenant's
    # own mailboxes, which is what keeps this inside the workspace.
    rows = (await ts.session.execute(
        select(User.id, User.full_name, User.email).where(User.id.in_(list(per_owner))))).all()         if per_owner else []
    names = {uid: (full or email or "") for uid, full, email in rows}
    out = []
    for owner_id, data in per_owner.items():
        hours = data["hours"]
        out.append(ResponseTime(
            user_id=owner_id, name=names.get(owner_id, ""), answered=len(hours),
            waiting=data["waiting"],
            median_hours=round(median(hours), 2) if hours else None,
            p90_hours=round(_p90(hours), 2) if hours else None))
    return sorted(out, key=lambda r: (r.median_hours is None, r.median_hours or 0.0))
