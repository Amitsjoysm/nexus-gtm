"""Replies the AI sends by itself, where the workspace allows it and the mailbox's SDR switched it
on (2026-10-13; `settings.reply_automation`). Everywhere else D22 stands: the AI drafts, a person
sends.

Ingestion only MARKS a reply (`ReplyClassification.auto_reply = "pending"`): a confident
`interested` or `question` from a human, not a colleague, in a thread that has not had an automatic
answer yet. This sweep, enqueued every tick, does the work after that transaction has committed, so
a slow draft never holds the mailbox read open, and it re-checks everything at send time:

* the switch is read AGAIN: an SDR who turned it off a minute ago is obeyed;
* a reply a person already answered, or closed, is left alone;
* the draft must pass every check (`check_draft`) and must not name a clock time, because the AI
  cannot see the rep's calendar and a time it offered would be a promise the rep never made;
* it goes out through `desk.send_response`, the path the SDR's own Send button takes, so the
  do-not-contact gate, exactly-once row and `outreach.email_send` charge all apply unchanged.

Anything short of that is `held` with a sentence in `auto_reply_note`, and the reply waits on the
desk with the AI's draft ready to edit, exactly as it would have without the switch. Never raises.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger("nexus.engagement.replies")

#: Per workspace per sweep. A draft is an LLM call; the rest wait for the next tick.
BATCH = 20

_CLOCK = re.compile(r"\b(?:[01]?\d|2[0-3]):[0-5]\d\b|\b(?:1[0-2]|0?[1-9])\s?(?:am|pm|a\.m\.|p\.m\.)",
                    re.IGNORECASE)


def names_a_time(text: str) -> bool:
    """Does the draft offer a clock time (10:30, 3pm, 9 am)? Durations and counts do not count."""
    return bool(_CLOCK.search(text or ""))


async def send_pending(ts) -> dict[str, int]:
    """Answer this workspace's pending replies. Returns how many were sent and held."""
    from nexus.models.engagement import ReplyClassification

    rows = await ts.list(ReplyClassification, ReplyClassification.auto_reply == "pending",
                         limit=BATCH)
    outcomes: dict[str, int] = {}
    for classification in rows:
        try:
            async with ts.session.begin_nested():
                outcome = await _answer(ts, classification)
        except Exception as exc:  # one reply must not stop the rest
            logger.warning("automatic reply for %s failed", classification.id, exc_info=True)
            _hold(classification, f"The automatic reply could not be sent ({type(exc).__name__}), "
                                  "so it waits for you.")
            outcome = "held"
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
    await ts.flush()
    return outcomes


def _hold(classification, note: str) -> str:
    classification.auto_reply = "held"
    classification.auto_reply_note = note[:500]
    return "held"


async def _answer(ts, classification) -> str:
    from nexus.engagement.desk.service import DeskError, draft_response, send_response
    from nexus.engagement.replies.alerts import notify
    from nexus.engagement.settings import read_settings, reply_automation
    from nexus.models.account import Contact
    from nexus.models.engagement import MailboxConnection
    from nexus.models.identity import Tenant

    mailbox = await ts.get(MailboxConnection, classification.mailbox_connection_id)
    tenant = await ts.session.get(Tenant, ts.tenant_id)
    sends_alone = mailbox is not None and reply_automation(
        read_settings(getattr(tenant, "email_settings", None)), mailbox)[1]
    if not sends_alone:
        return _hold(classification, "Sending AI replies by itself was switched off before this "
                                     "one went out, so it waits for you.")
    if classification.status != "open" or classification.responded_at is not None:
        return _hold(classification, "Someone answered or closed this reply first.")

    try:
        written = await draft_response(ts, classification, user_id=mailbox.owner_user_id,
                                       automatic=True)
    except DeskError as exc:
        return _hold(classification, f"{exc} It waits for you.")
    if written["quality_problems"]:
        return _hold(classification, "The AI's draft did not pass its checks ("
                     + "; ".join(written["quality_problems"])[:300]
                     + "), so it waits for you to edit and send.")
    if names_a_time(written["body"]):
        return _hold(classification, "The AI's draft offered a specific time, which only you can "
                                     "promise, so it waits for you.")

    result = await send_response(ts, classification, user_id=mailbox.owner_user_id,
                                 subject=written["subject"], body=written["body"])
    if not result.sent:
        return _hold(classification, f"It could not be sent ({result.reason or result.outcome}), "
                                     "so it waits for you.")
    classification.auto_reply = "sent"
    classification.auto_reply_note = None
    await ts.flush()
    contact = await ts.get(Contact, classification.contact_id) \
        if classification.contact_id else None
    who = getattr(contact, "full_name", "") or getattr(contact, "email", "") or "a buyer"
    await notify(ts, category="reply_auto_sent", owner_user_id=mailbox.owner_user_id,
                 account_id=classification.account_id, title=f"The AI replied to {who}",
                 body=written["body"][:280],
                 meta={"classification_id": classification.id,
                       "contact_id": classification.contact_id})
    return "sent"
