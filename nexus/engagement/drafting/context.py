"""The context pack: everything the model sees before it writes (spec §7, D15).

One builder for every draft — first emails, follow-ups, re-engagements and, in phase 10, responses —
so "the AI always knows the date, and every email in the conversation" (D15) is true by
construction rather than by each caller remembering to include it.

Five blocks, in the order the model needs them:

1. **Now** — UTC, the contact's local date, time and weekday, whether that is a business day for
   them, and the SDR's local time. Without it a follow-up says "hope your week is going well" on a
   Saturday.
2. **This conversation** — every message in the thread, oldest first, with direction and time.
3. **Other conversations with this person** — across campaigns and mailboxes: the most recent in
   full, older ones as one line each, so a long history fits the budget.
4. **History** — how earlier replies were read and what the SDR decided.
5. **The person and the company** — role, seniority, what we know about the account, its live
   signals.

`facts` are the specific, sourced things a draft can open on; `personalisation.uses_a_fact` checks
the email used one of them (D17).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

UTC = timezone.utc  # datetime.UTC is 3.11+; this project supports 3.10

#: Per-message character budget. A long reply is kept; a forwarded thread pasted inside it is not.
MESSAGE_BUDGET = 1500
OLDER_CONVERSATIONS = 5


@dataclass(slots=True)
class ContextPack:
    text: str
    facts: list[str] = field(default_factory=list)
    zone_name: str = "UTC"


async def _rows(ts, stmt) -> list:
    return list((await ts.session.scalars(stmt)).all())


def _clip(text: str, limit: int = MESSAGE_BUDGET) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + " …"


def _when(moment: datetime | None) -> str:
    if moment is None:
        return "unknown time"
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def now_block(now: datetime, contact_zone, contact_zone_name: str, sdr_zone,
              sdr_zone_name: str) -> str:
    from nexus.engagement.timekeeping import is_business_day, to_local

    local = to_local(now, contact_zone)
    sdr = to_local(now, sdr_zone)
    business = "a business day" if is_business_day(local.date()) else "NOT a business day"
    return (
        "NOW\n"
        f"- UTC: {now.astimezone(UTC).strftime('%Y-%m-%d %H:%M')} ({now.astimezone(UTC):%A})\n"
        f"- The contact's local time ({contact_zone_name}): {local:%A %Y-%m-%d %H:%M}, "
        f"{business} for them\n"
        f"- The SDR's local time ({sdr_zone_name}): {sdr:%A %H:%M}\n"
    )


def conversation_block(messages) -> str:
    if not messages:
        return "THIS CONVERSATION\n- Nothing has been sent or received yet: this is the first email.\n"
    lines = ["THIS CONVERSATION (oldest first)"]
    for message in messages:
        who = "WE WROTE" if message.direction == "out" else "THEY WROTE"
        when = _when(message.sent_at or message.received_at or message.created_at)
        lines.append(f"- {who} at {when} — Subject: {message.subject or '(none)'}\n"
                     f"  {_clip(message.body_text)}")
    return "\n".join(lines) + "\n"


async def sender_name(ts, mailbox) -> str:
    """The name an email from this mailbox is signed with: its display name, else its owner's."""
    name = (getattr(mailbox, "display_name", "") or "").strip()
    if not name and getattr(mailbox, "owner_user_id", None):
        from nexus.models.identity import User

        owner = await ts.session.get(User, mailbox.owner_user_id)
        name = ((owner.full_name if owner else "") or "").strip()
    return name


async def _sender_block(ts, mailbox) -> str:
    """Who the email is from (`copy.sender_block`), so the model signs with the real first name."""
    from nexus.agents.copy import sender_block

    return sender_block(await sender_name(ts, mailbox))


#: What each follow-up adds, by its place in the sequence (the B2B cold email playbook the product
#: owner supplied, 2026-10-01). The rule underneath all of them: every touch must give the reader
#: something the last one did not, because "just bumping this" is the email that gets marked as spam.
#: Touch 1 is the first email; a template's own angle for a step still wins over these.
FOLLOWUP_BY_TOUCH = {
    2: ("Bring a NEW angle: what a similar company got from us, only if the context names a real "
        "customer result; otherwise a different problem the same trigger creates."),
    3: ("Ask one pointed question or share one short insight about what the current way costs "
        "someone in their role. No pitch in this one."),
    4: "Keep it to two or three short lines: one new fact or question, then the ask.",
}

#: The last email of a sequence. Saying so is what lets a busy buyer answer without a meeting.
LAST_TOUCH = ("This is the LAST email in the sequence. In two or three short lines, say politely "
              "that you will stop writing and leave the door open. No guilt, no pressure.")


def followup_instruction(touch: int, total: int) -> str:
    """What follow-up number ``touch`` (2 or more) should add, given ``total`` steps.

    The close-the-loop note is for the last step of a sequence of three or more, or any touch past
    the fourth: a two-step sequence ending on "I'll stop writing" after one unanswered email reads as
    a sulk, not a courtesy.
    """
    if touch >= 5 or (touch >= 3 and touch == total):
        return LAST_TOUCH
    return FOLLOWUP_BY_TOUCH.get(touch, FOLLOWUP_BY_TOUCH[4])


async def build_context(ts, *, enrollment, contact, account, mailbox, step=None,
                        kind: str = "first", now: datetime | None = None) -> ContextPack:
    """The pack for one draft. Reads only this tenant's rows, through the tenant session."""
    from nexus.agents.copy import account_facts, signal_facts
    from nexus.core.db import utcnow
    from nexus.engagement.timekeeping import zone_or_none
    from nexus.models.engagement import EngagementMessage, EngagementThread, ReplyClassification
    from nexus.models.signal import SignalEvent

    moment = now or utcnow()
    zone_name = (getattr(enrollment, "contact_timezone", "") or mailbox.timezone or "UTC")
    contact_zone = zone_or_none(zone_name) or UTC
    sdr_zone = zone_or_none(mailbox.timezone) or UTC
    parts = [now_block(moment, contact_zone, zone_name, sdr_zone, mailbox.timezone or "UTC")]
    sender = await _sender_block(ts, mailbox)
    if sender:
        parts.append(sender)

    current_thread_id = getattr(enrollment, "current_thread_id", None)
    conversation = []
    if current_thread_id:
        conversation = await _rows(ts, ts.select(
            EngagementMessage, EngagementMessage.thread_id == current_thread_id,
            EngagementMessage.status.in_(("sent", "received", "bounced")),
        ).order_by(EngagementMessage.created_at.asc()))
    parts.append(conversation_block(conversation))

    others = await _rows(ts, ts.select(
        EngagementThread, EngagementThread.contact_id == contact.id,
    ).order_by(EngagementThread.last_message_at.desc()))
    others = [t for t in others if t.id != current_thread_id]
    if others:
        lines = ["OTHER CONVERSATIONS WITH THIS PERSON (most recent first)"]
        latest = others[0]
        latest_messages = await _rows(ts, ts.select(
            EngagementMessage, EngagementMessage.thread_id == latest.id,
            EngagementMessage.status.in_(("sent", "received")),
        ).order_by(EngagementMessage.created_at.asc()))
        lines.append(f"- Most recent, \"{latest.base_subject}\":")
        for message in latest_messages[-6:]:
            who = "we" if message.direction == "out" else "they"
            lines.append(f"  {who} ({_when(message.sent_at or message.received_at)}): "
                         f"{_clip(message.body_text, 400)}")
        for thread in others[1:1 + OLDER_CONVERSATIONS]:
            lines.append(f"- Earlier: \"{thread.base_subject}\" (last {_when(thread.last_message_at)})")
        parts.append("\n".join(lines) + "\n")

    classifications = await _rows(ts, ts.select(
        ReplyClassification, ReplyClassification.contact_id == contact.id,
    ).order_by(ReplyClassification.created_at.desc()).limit(5))
    if classifications:
        lines = ["HISTORY"]
        for item in classifications:
            detail = f"- A reply read as {item.category}"
            if item.resolved_date:
                detail += f", asking to reconnect on {item.resolved_date}"
            if item.decision:
                detail += f"; the SDR decided: {item.decision}"
            lines.append(detail)
        parts.append("\n".join(lines) + "\n")

    signals = await _rows(ts, ts.select(
        SignalEvent, SignalEvent.account_id == account.id,
    ).order_by(SignalEvent.occurred_at.desc()).limit(8))
    facts: list[str] = [s.title for s in signals if (s.title or "").strip()][:5]
    person = [f"- Name: {contact.full_name}"]
    if contact.title:
        person.append(f"- Title: {contact.title}")
        facts.append(contact.title)
    if contact.seniority:
        person.append(f"- Seniority: {contact.seniority}")
    parts.append("THE PERSON\n" + "\n".join(person) + "\n")
    referral = (contact.custom_fields or {}).get("referred_by") \
        if isinstance(contact.custom_fields, dict) else None
    referrer = (referral.get("name") or "").strip() if isinstance(referral, dict) else ""
    if referrer:
        # Who made the introduction, never what they wrote to us (spec §19).
        title = (referral.get("title") or "").strip()
        parts.append("REFERRAL\n- " + referrer + (f" ({title})" if title else "")
                     + f" at {account.name} suggested we speak with this person.\n")
        facts.append(f"{referrer.split()[0]} suggested we talk")
    parts.append("THE COMPANY\n" + account_facts(account) + "\n")
    rendered_signals = signal_facts(signals)
    if rendered_signals:
        parts.append("LIVE SIGNALS (strongest first)\n" + rendered_signals + "\n")

    touch_line = ""
    if kind == "followup" and step is not None:
        from nexus.models.engagement import EngagementStep

        touch = int(getattr(step, "step_index", 0) or 0) + 1
        total = len(await _rows(ts, ts.select(
            EngagementStep, EngagementStep.campaign_id == step.campaign_id)))
        touch_line = f" This is email {touch} of {total}. " + followup_instruction(touch, total)
    instructions = {
        "first": "This is the FIRST email to this person." + (
            f" Say in one short line that {referrer.split()[0]} suggested you get in touch; do "
            "not quote or paraphrase anything else they said." if referrer else ""),
        "followup": ("This is a FOLLOW-UP in the same thread. Do not repeat the earlier email; add "
                     "one new, specific reason to reply. Never pretend they answered, and never "
                     "write that you are just following up or bumping this." + touch_line),
        "reengage": ("They asked us to get back in touch around now. Reference that they asked, in "
                     "one short line, and make it easy to pick the conversation back up."),
        "response": ("They replied. Answer what they actually said first, briefly. If they showed "
                     "interest or asked to talk, offer two specific times in their timezone from "
                     "the date above and ask which suits; otherwise propose one next step."),
        "signal": ("Our earlier emails went unanswered, or they asked to talk later. Something new "
                   "has happened at their company (the angle below): open with it in one line as "
                   "the reason for writing now. Never pretend they replied, and if they asked for "
                   "later, say you are early because of this news."),
    }.get(kind, "")
    angle = (getattr(step, "angle", "") or "").strip()
    block = ["INSTRUCTIONS", f"- {instructions}"]
    if angle:
        block.append(f"- The angle for this step (follow this over the suggestion above): {angle}")
    parts.append("\n".join(block) + "\n")
    return ContextPack(text="\n".join(parts).strip(), facts=facts, zone_name=zone_name)
