# Phase 10: The Reply Desk Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every reply phase 09 classified reaches a person who can act on it in one screen — the queue of what needs an answer, the conversation behind it, an AI-suggested response the SDR edits and sends, four decisions that close the item, and a reminder when an interested buyer has been waiting.

**Architecture:** `nexus/engagement/desk/service.py` is the whole surface: three tab readers (one query each), `item()` for the detail, `draft_response`/`send_response`/`save_to_drafts` for the answer, `decide`/`correct`/`reassign` for the outcome, and `remind_unanswered` for the reply-speed promise. It composes what earlier phases already own — the drafter (08), the sender (07), suppression (05), the alert routing (09) and `set_status` (08) — and adds no second path to any of them. `nexus/api/routers/engagement_desk.py` is a thin translation layer: it resolves the classification, checks it belongs to the caller's mailbox, and turns a `DeskError` into a 409.

**Tech Stack:** FastAPI, async SQLAlchemy, the phase 03 provider seam (`create_draft`), the existing alert system.

**Roadmap:** [00-roadmap.md](00-roadmap.md). **Spec:** §9, §19, D3, D22. **Depends on:** phases 05, 07, 08, 09.

**Verified:** implemented on `feat/sdr-engagement` on top of phases 01–09 and run in the CI image: `tests/test_engagement_desk.py` (19 passed — office-hours arithmetic, the three tabs, the conversation and paused colleagues, the metered suggestion, threading a sent answer, the unmetered draft, all four decisions, corrections, colleague resume/stop, the reminder, and the API including a rep who may not open a colleague's reply), plus the engagement, billing and worker suites it touches, and `ruff`.

---

## Decisions this phase makes

- **`ai.reply_draft` is its own capability** (2 credits, COGS $0.0012), not `ai.email_draft`. The prompt carries the whole conversation as well as writing one, and a workspace that answers every reply by hand should not pay the line of one that drafts every send. `drafter.draft` therefore takes a `capability` argument rather than naming one.
- **Saving to Drafts is never metered.** Nothing left the building, and charging for pressing Save bills a customer for hesitating — the same rule `draft_for_contact` already applies in `outreach/`.
- **An answer does not close an `unclear` item.** `unclear` means a person still has to say what happens to the sequence; the reply has been answered and the decision has not, so `responded_at` is set and `status` stays `open`.
- **The reply-speed promise is counted in office hours** (Mon–Fri 09:00–18:00 in the mailbox's own zone), not wall-clock hours and not whole weekdays. Wall-clock makes every reply that lands at six in the evening late by breakfast; whole weekdays puts a four-hour promise at ten at night.
- **One reminder per reply.** `reminded_at` is a timestamp, not a counter: a second nudge says nothing new about the same fact.
- **A manager's team view is server-side.** `?team=true` is honoured only for `manage_engagement`; a rep asking for it gets their own mailboxes, silently, because the parameter is a request and not an assertion.

---

## Files

| Action | Path | Responsibility |
|---|---|---|
| Create | `nexus/engagement/desk/__init__.py`, `service.py` | the queue, the answer, the decisions, the reminder |
| Create | `nexus/api/routers/engagement_desk.py` | `/engagement/desk/...` |
| Create | `nexus/engagement/sending/drafts.py` | one way to put a message in a provider's Drafts |
| Modify | `nexus/engagement/drafting/drafter.py` | the `capability` argument |
| Modify | `nexus/billing/catalog.py`, `nexus/billing/rates.py` | `ai.reply_draft` |
| Modify | `nexus/workers/tasks.py`, `nexus/workers/scheduler.py` | the `remind_replies` job |
| Modify | `nexus/api/routers/__init__.py` | register the router |
| Modify | `tests/test_engagement_sending.py` | `create_draft` on the provider double |
| Create | `tests/test_engagement_desk.py` | everything above |

---

### Task 1: The capability the suggestion is charged as

**Files:**
- Modify: `nexus/engagement/drafting/drafter.py`
- Modify: `nexus/billing/catalog.py`, `nexus/billing/rates.py`

- [ ] **Step 1: Write the failing test.** Create `tests/test_engagement_desk.py` with the final text from Task 6. The test that proves this task is `test_the_suggested_answer_is_written_charged_and_kept_for_the_sdr`, which asserts exactly one `ai.reply_draft` usage row.

- [ ] **Step 2: Run** `pytest tests/test_engagement_desk.py -n0 -q -k suggested` — expected FAIL: `No module named 'nexus.engagement.desk'`.

- [ ] **Step 3: Give the drafter a capability argument.** In `nexus/engagement/drafting/drafter.py` change the signature and docstring:

```python
async def draft(ts, *, enrollment, contact, account, mailbox, step=None, kind: str = "first",
                thread=None, user_id: str | None = None, now=None,
                capability: str = EMAIL_DRAFT) -> Draft:
    """One draft. Never raises: a draft that cannot be written reports why.

    ``capability`` is what the draft is charged as. An outbound step is ``ai.email_draft``; a reply
    the desk suggests is ``ai.reply_draft``, priced separately because it reads the conversation as
    well as writing one — and because a workspace that answers every reply by hand should not be
    paying the same line as one that drafts every send.
    """
```

the two capability names beside the meter, at module level:

```python
#: What a draft is charged as. Named here, beside the meter, so the capability a caller passes and
#: the call that charges it are one module (``tests/test_billing_metering_coverage.py``).
EMAIL_DRAFT = "ai.email_draft"
REPLY_DRAFT = "ai.reply_draft"
```

and the meter inside it:

```python
        async with metered(ts, capability, user_id=user_id, source="engagement"):
```

The constants are not decoration. `test_billing_metering_coverage` counts a priced capability as billed only when its id is written in a module that meters; passing the string in from the desk left `ai.reply_draft` looking like a price nobody is charged, and the guard failed.

The default keeps every existing caller — `draft_first_emails`, `regenerate`, the just-in-time follow-up in `advance.py` — charging exactly what it charged before.

- [ ] **Step 4: Add the capability.** In `nexus/billing/catalog.py`, immediately before `ai.contact_rank`:

```python
    _cap("ai.reply_draft", "ai", "AI suggested response", sub_category="outreach",
         default_mode="metered", depends_on=["module.campaigns"],
         description="Drafting one answer to a reply, from the whole conversation (spec §9)."),
```

- [ ] **Step 5: Price it.** In `nexus/billing/rates.py`, immediately after `ai.reply_classify`:

```python
    # Reads the thread as well as writing a reply, so a longer prompt than a first email.
    _r("ai.reply_draft", 2, 0.0012, "groq over the conversation"),
```

`tests/test_billing_metering_coverage.py` refuses a priced capability with no call site, so this lands in the same change as Task 2's `draft_response`. The phase 08 launch-gate estimate already names `ai.reply_draft` as one worst-case line per contact; until this task it priced at zero.

- [ ] **Step 6: Commit** together with Task 2.

---

### Task 2: The desk service

**Files:**
- Create: `nexus/engagement/desk/__init__.py`, `nexus/engagement/desk/service.py`
- Test: `tests/test_engagement_desk.py`

- [ ] **Step 1: Run the tests from Task 6** — expected FAIL: `No module named 'nexus.engagement.desk'`.

- [ ] **Step 2: Implement** `nexus/engagement/sending/drafts.py`, the shared Drafts helper:

```python
"""Put a message in the SDR's own Drafts folder through Gmail or Microsoft Graph.

One function, because two screens do this — the contact composer's Save to Drafts and the reply
desk's — and a draft must be built exactly as a send would be: same From, same signature, same
opt-out footer and List-Unsubscribe headers. What the SDR opens in Gmail is what would have gone out.

**Never metered.** `outreach.email_send` prices a message that left the building; charging for a
draft bills a customer for pressing Save.

A draft that answers a message carries `In-Reply-To`/`References` and the provider's thread, so it
opens inside the conversation rather than as a new one.
"""
from __future__ import annotations


async def save_draft(ts, *, mailbox, contact, subject: str, body: str, answering=None,
                     thread=None) -> str:
    """Create the draft and return the provider's id for it.

    Raises the provider's errors. An expired grant also marks the mailbox ``needs_reauth``, so the
    next screen that shows it says "reconnect" rather than repeating a failure nobody can read.
    """
    from nexus.engagement.ids import new_ulid
    from nexus.engagement.mailboxes.provider import AuthExpired, ThreadRef
    from nexus.engagement.mailboxes.registry import open_provider
    from nexus.engagement.sending import mime
    from nexus.engagement.sending.service import _domain, _signature
    from nexus.engagement.suppression.tokens import unsubscribe_url

    parent_id = getattr(answering, "rfc_message_id", "") or ""
    message = mime.build_message(
        from_addr=mailbox.email, from_name=mailbox.display_name or "", to_addr=contact.email,
        subject=(subject or "").strip(), body=body,
        message_id=mime.new_rfc_message_id(_domain(mailbox.email)), ref=new_ulid(),
        unsubscribe_url=unsubscribe_url(ts.tenant_id, contact.id),
        unsubscribe_mailto=mailbox.email, signature=await _signature(ts, mailbox),
        in_reply_to=parent_id,
        references=mime.references_for(getattr(answering, "references_header", "") or "",
                                       parent_id))
    thread_ref = None
    if thread is not None:
        thread_ref = ThreadRef(
            provider_thread_id=thread.provider_thread_id or "",
            reply_to_provider_message_id=getattr(answering, "provider_message_id", "") or "",
            in_reply_to=parent_id)
    try:
        provider = await open_provider(ts, mailbox)
        return await provider.create_draft(mime.to_bytes(message), thread=thread_ref)
    except AuthExpired:
        mailbox.status = "needs_reauth"
        mailbox.last_error = mailbox.last_error or "Reconnect this mailbox: the grant expired"
        await ts.flush()
        raise
```

- [ ] **Step 2b: Implement** `nexus/engagement/desk/__init__.py`:

```python
"""The reply desk: the queue an SDR works, and the decisions that close an item (spec §9)."""
```

- [ ] **Step 3: Implement** `nexus/engagement/desk/service.py`:

```python
"""The reply desk: what needs an answer, the answer the AI suggests, and the SDR's decision (§9, D22).

**Nothing here sends by itself.** The AI drafts; a person presses Send. That is D22, and it is why
`send_response` takes the text the SDR is looking at rather than re-drafting at the last moment.

Three tabs, one query each:

* **Needs action** — open classifications (`interested`, `question`, `referral`, `unclear`) plus the
  colleagues those replies paused, who never resume on their own (D3).
* **Scheduled** — enrollments waiting for a date: a re-engagement they asked for, or a return from
  leave. Both are editable and cancellable.
* **Handled** — declined, unsubscribed and bounced: the record of who may not be emailed and why.

A rep sees their own mailboxes; a manager sees the team's and can reassign.

Four decisions close an item, and each one is a different statement about the person:
`reengage` (come back on a date), `block` (do not contact), `close` (nothing more to do here),
`meeting` (which also writes an `Outcome`, so the dashboards and account tiering see it).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time

OPEN_CATEGORIES = ("interested", "question", "referral", "unclear")
HANDLED_CATEGORIES = ("declined", "unsubscribe")
DECISIONS = ("reengage", "block", "close", "meeting")
REENGAGE_AT = time(9, 0)
#: The working day the reply-speed promise is counted in (spec §19).
OFFICE_OPEN = time(9, 0)
OFFICE_CLOSE = time(18, 0)


class DeskError(ValueError):
    """A desk action the item's state does not allow."""


@dataclass(slots=True)
class DeskItem:
    classification: object
    message: object
    contact: object | None = None
    account: object | None = None
    enrollment: object | None = None
    conversation: list = field(default_factory=list)
    paused_colleagues: list = field(default_factory=list)


async def _mailbox_ids(ts, *, user_id: str, team: bool) -> list[str]:
    from nexus.models.engagement import MailboxConnection

    where = [] if team else [MailboxConnection.owner_user_id == user_id]
    return [m.id for m in await ts.list(MailboxConnection, *where)]


async def needs_action(ts, *, user_id: str, team: bool = False) -> list:
    """Open classifications on the caller's mailboxes, oldest first — the queue, in order."""
    from nexus.models.engagement import ReplyClassification

    mailboxes = await _mailbox_ids(ts, user_id=user_id, team=team)
    if not mailboxes:
        return []
    rows = await ts.list(ReplyClassification,
                         ReplyClassification.mailbox_connection_id.in_(mailboxes),
                         ReplyClassification.status == "open")
    return sorted(rows, key=lambda r: r.created_at)


async def scheduled(ts, *, user_id: str, team: bool = False) -> list:
    """Enrollments waiting for a date: a re-engagement, or a return from leave."""
    from sqlalchemy import or_

    from nexus.models.engagement import EngagementEnrollment

    mailboxes = await _mailbox_ids(ts, user_id=user_id, team=team)
    if not mailboxes:
        return []
    rows = await ts.list(
        EngagementEnrollment,
        EngagementEnrollment.mailbox_connection_id.in_(mailboxes),
        or_(EngagementEnrollment.status == "snoozed",
            (EngagementEnrollment.status == "paused")
            & (EngagementEnrollment.status_reason == "out_of_office")))
    return sorted(rows, key=lambda r: r.snoozed_until or r.created_at)


async def handled(ts, *, user_id: str, team: bool = False, limit: int = 100) -> list:
    from nexus.models.engagement import ReplyClassification

    mailboxes = await _mailbox_ids(ts, user_id=user_id, team=team)
    if not mailboxes:
        return []
    rows = await ts.list(ReplyClassification,
                         ReplyClassification.mailbox_connection_id.in_(mailboxes),
                         ReplyClassification.status == "done")
    return sorted(rows, key=lambda r: r.created_at, reverse=True)[:limit]


async def item(ts, classification) -> DeskItem:
    """One item with everything the screen shows: the conversation, who it is with, and the
    colleagues this reply paused."""
    from nexus.models.account import Account, Contact
    from nexus.models.engagement import EngagementEnrollment, EngagementMessage

    message = await ts.get(EngagementMessage, classification.message_id)
    conversation = []
    if message is not None and message.thread_id:
        conversation = list((await ts.session.scalars(
            ts.select(EngagementMessage, EngagementMessage.thread_id == message.thread_id,
                      EngagementMessage.status.in_(("sent", "received", "bounced")))
            .order_by(EngagementMessage.created_at.asc()))).all())
    colleagues = []
    if classification.account_id:
        colleagues = [e for e in await ts.list(
            EngagementEnrollment, EngagementEnrollment.account_id == classification.account_id,
            EngagementEnrollment.status == "paused",
            EngagementEnrollment.status_reason == "colleague_replied")]
    return DeskItem(
        classification=classification, message=message,
        contact=await ts.get(Contact, classification.contact_id)
        if classification.contact_id else None,
        account=await ts.get(Account, classification.account_id)
        if classification.account_id else None,
        enrollment=await ts.get(EngagementEnrollment, classification.enrollment_id)
        if classification.enrollment_id else None,
        conversation=conversation, paused_colleagues=colleagues)


async def draft_response(ts, classification, *, user_id: str) -> dict:
    """The AI's suggested reply, stored on the classification for the SDR to edit (D22)."""
    from nexus.engagement.drafting.drafter import REPLY_DRAFT, draft
    from nexus.engagement.ledger.emit import emit
    from nexus.models.account import Account
    from nexus.models.engagement import EngagementEnrollment, EngagementThread, MailboxConnection

    detail = await item(ts, classification)
    if detail.contact is None or detail.message is None:
        raise DeskError("This reply is not linked to a contact, so there is nobody to answer.")
    mailbox = await ts.get(MailboxConnection, classification.mailbox_connection_id)
    account = detail.account or await ts.get(Account, detail.contact.account_id)
    thread = await ts.get(EngagementThread, detail.message.thread_id) \
        if detail.message.thread_id else None
    enrollment = detail.enrollment or EngagementEnrollment(
        campaign_id="", contact_id=detail.contact.id, account_id=getattr(account, "id", ""),
        contact_timezone=mailbox.timezone or "UTC", current_thread_id=getattr(thread, "id", None))
    written = await draft(ts, enrollment=enrollment, contact=detail.contact, account=account,
                          mailbox=mailbox, kind="response", thread=thread, user_id=user_id,
                          capability=REPLY_DRAFT)
    if not written.ok:
        raise DeskError(f"The response could not be drafted: {written.error}")
    classification.suggested_response = f"{written.subject}\n\n{written.body}".strip()
    await ts.flush()
    await emit(ts, "response.drafted", actor_user_id=user_id,
               refs={"message_id": detail.message.id, "contact_id": detail.contact.id,
                     "account_id": getattr(account, "id", None),
                     "classification_id": classification.id,
                     "thread_id": getattr(thread, "id", None), "mailbox_id": mailbox.id},
               payload={"subject": written.subject, "body": written.body,
                        "context_pack": written.context_pack, "category": classification.category})
    return {"subject": written.subject, "body": written.body,
            "quality_problems": written.problems}


async def send_response(ts, classification, *, user_id: str, subject: str, body: str):
    """Send the SDR's answer in the same thread. The text sent is the text they were looking at."""
    from nexus.core.db import utcnow
    from nexus.engagement.ledger.emit import emit
    from nexus.engagement.sending.service import send
    from nexus.models.account import Contact
    from nexus.models.engagement import EngagementThread, MailboxConnection

    detail = await item(ts, classification)
    contact = detail.contact or (await ts.get(Contact, classification.contact_id)
                                 if classification.contact_id else None)
    if contact is None:
        raise DeskError("This reply is not linked to a contact, so there is nobody to answer.")
    mailbox = await ts.get(MailboxConnection, classification.mailbox_connection_id)
    thread = await ts.get(EngagementThread, detail.message.thread_id) \
        if detail.message is not None and detail.message.thread_id else None
    result = await send(ts, mailbox=mailbox, contact=contact, subject=subject, body=body,
                        thread=thread, kind="response", user_id=user_id,
                        idempotency_key=f"response:{classification.id}",
                        ai_subject=(classification.suggested_response or "").split("\n", 1)[0],
                        ai_body=(classification.suggested_response or "").partition("\n\n")[2])
    if result.sent:
        classification.responded_at = utcnow()
        if classification.category != "unclear":
            classification.status = "done"
        await ts.flush()
        await emit(ts, "response.sent", actor_user_id=user_id,
                   refs={"message_id": result.message_id, "contact_id": contact.id,
                         "account_id": classification.account_id,
                         "classification_id": classification.id,
                         "thread_id": result.thread_id, "mailbox_id": mailbox.id,
                         "answered_message_id": classification.message_id},
                   payload={"subject": subject, "body": body,
                            "ai_body": (classification.suggested_response or "")
                            .partition("\n\n")[2],
                            "conversation": [{"direction": m.direction,
                                              "at": (m.sent_at or m.received_at or m.created_at)
                                              .isoformat(), "body": m.body_text}
                                             for m in detail.conversation]})
    return result


async def save_to_drafts(ts, classification, *, subject: str, body: str) -> str:
    """Put the answer in the SDR's own Drafts folder instead of sending it. Never metered: nothing
    left the building, and charging for pressing Save bills a customer for hesitating."""
    from nexus.engagement.mailboxes.provider import ProviderError
    from nexus.engagement.sending.drafts import save_draft
    from nexus.models.account import Contact
    from nexus.models.engagement import EngagementThread, MailboxConnection

    detail = await item(ts, classification)
    contact = detail.contact or (await ts.get(Contact, classification.contact_id)
                                 if classification.contact_id else None)
    if contact is None:
        raise DeskError("This reply is not linked to a contact, so there is nobody to answer.")
    mailbox = await ts.get(MailboxConnection, classification.mailbox_connection_id)
    answered = detail.message
    thread = await ts.get(EngagementThread, answered.thread_id) \
        if answered is not None and answered.thread_id else None
    try:
        return await save_draft(ts, mailbox=mailbox, contact=contact, subject=subject, body=body,
                                answering=answered, thread=thread)
    except ProviderError as exc:
        raise DeskError(f"The draft could not be saved: {exc}") from exc


async def decide(ts, classification, decision: str, *, user_id: str,
                 reengage_on: date | None = None, note: str = "") -> str:
    """Close an item the way the SDR chose. Every decision is recorded in the ledger."""
    from nexus.core.db import utcnow
    from nexus.engagement.ledger.emit import emit
    from nexus.engagement.sequences.service import set_status
    from nexus.engagement.suppression.service import suppress
    from nexus.engagement.timekeeping import at_local, zone_or_none
    from nexus.models.account import Contact
    from nexus.models.engagement import EngagementEnrollment
    from nexus.outcomes.service import get_outcome_service

    if decision not in DECISIONS:
        raise DeskError(f"Unknown decision {decision!r}.")
    if decision == "reengage" and reengage_on is None:
        raise DeskError("Choose the date to come back on.")
    contact = await ts.get(Contact, classification.contact_id) \
        if classification.contact_id else None
    enrollments = await ts.list(EngagementEnrollment,
                                EngagementEnrollment.contact_id == classification.contact_id) \
        if classification.contact_id else []

    if decision == "reengage":
        for enrollment in enrollments:
            if enrollment.status == "completed":
                continue
            zone = zone_or_none(enrollment.contact_timezone) or UTC
            enrollment.snoozed_until = at_local(reengage_on, REENGAGE_AT, zone)
            await set_status(ts, enrollment, "snoozed", "later", user_id=user_id)
    elif decision == "block":
        if contact is not None and contact.email:
            await suppress(ts, email=contact.email, reason="manual", contact_id=contact.id,
                           source_message_id=classification.message_id,
                           created_by_user_id=user_id)
        for enrollment in enrollments:
            if enrollment.status not in ("stopped", "completed"):
                await set_status(ts, enrollment, "stopped", "manual", user_id=user_id)
    elif decision == "meeting":
        await get_outcome_service().record(
            ts, stage="meeting", account_id=classification.account_id,
            contact_id=classification.contact_id,
            meta={"source": "engagement_reply_desk", "message_id": classification.message_id,
                  "classification_id": classification.id})
        for enrollment in enrollments:
            if enrollment.status not in ("stopped", "completed"):
                await set_status(ts, enrollment, "stopped", "replied", user_id=user_id)
    else:  # close
        for enrollment in enrollments:
            if enrollment.status == "paused" and enrollment.status_reason == "needs_decision":
                await set_status(ts, enrollment, "stopped", "manual", user_id=user_id)

    classification.decision = decision
    classification.decided_by_user_id = user_id
    classification.decided_at = utcnow()
    classification.status = "done"
    await ts.flush()
    await emit(ts, "reply.decided", actor_user_id=user_id,
               refs={"message_id": classification.message_id,
                     "contact_id": classification.contact_id,
                     "account_id": classification.account_id,
                     "classification_id": classification.id},
               payload={"decision": decision, "category": classification.category,
                        "reengage_on": reengage_on.isoformat() if reengage_on else "",
                        "note": note[:500]})
    return decision


async def correct(ts, classification, category: str, *, user_id: str) -> None:
    """The SDR says the AI read it wrong. The correction is the label the ledger trains on."""
    from nexus.engagement.ledger.emit import emit
    from nexus.engagement.replies.classify import CATEGORIES

    if category not in CATEGORIES:
        raise DeskError(f"Unknown category {category!r}.")
    previous = classification.category
    classification.corrected_category = category
    classification.label_source = "sdr_confirmed" if category == previous else "sdr_corrected"
    await ts.flush()
    await emit(ts, "reply.corrected", actor_user_id=user_id,
               refs={"message_id": classification.message_id,
                     "contact_id": classification.contact_id,
                     "account_id": classification.account_id,
                     "classification_id": classification.id},
               payload={"ai_category": previous, "sdr_category": category,
                        "resolved_date": classification.resolved_date.isoformat()
                        if classification.resolved_date else ""})


async def reassign(ts, classification, *, user_id: str) -> None:
    classification.assigned_user_id = user_id
    await ts.flush()


async def resume_colleague(ts, enrollment, *, user_id: str) -> None:
    from nexus.engagement.sequences.service import resume_enrollment

    if enrollment.status_reason != "colleague_replied":
        raise DeskError("This contact was not paused by a colleague's reply.")
    await resume_enrollment(ts, enrollment, user_id=user_id)


async def stop_colleague(ts, enrollment, *, user_id: str) -> None:
    from nexus.engagement.sequences.service import stop_enrollment

    await stop_enrollment(ts, enrollment, "manual", user_id=user_id)


async def remind_unanswered(ts, *, now: datetime) -> int:
    """Nudge the SDR when an interested buyer has been waiting (§19 reply-speed reminder).

    Business hours, not wall-clock hours: a reply that arrives on Friday evening is not late on
    Saturday morning. One reminder per reply — a second would be noise about the same fact.
    """
    from nexus.engagement.replies.alerts import notify
    from nexus.engagement.settings import read_settings
    from nexus.engagement.timekeeping import zone_or_none
    from nexus.models.account import Contact
    from nexus.models.engagement import MailboxConnection, ReplyClassification
    from nexus.models.identity import Tenant

    tenant = await ts.session.get(Tenant, ts.tenant_id)
    hours = read_settings(getattr(tenant, "email_settings", None)).reply_reminder_business_hours
    waiting = await ts.list(ReplyClassification, ReplyClassification.status == "open",
                            ReplyClassification.category.in_(("interested", "question")),
                            ReplyClassification.responded_at.is_(None),
                            ReplyClassification.reminded_at.is_(None))
    reminded = 0
    for classification in waiting:
        mailbox = await ts.get(MailboxConnection, classification.mailbox_connection_id)
        # In the SDR's own working day: "four hours" means four hours they were at work.
        zone = zone_or_none(getattr(mailbox, "timezone", None)) or UTC
        if business_hours_between(classification.created_at, now, zone) < hours:
            continue
        contact = await ts.get(Contact, classification.contact_id) \
            if classification.contact_id else None
        await notify(ts, category=f"reply_{classification.category}",
                     owner_user_id=getattr(mailbox, "owner_user_id", None),
                     account_id=classification.account_id,
                     title=f"Still waiting for your reply to "
                           f"{getattr(contact, 'full_name', 'a buyer')}",
                     body=f"They wrote {hours}+ business hours ago and the answer is still a draft.",
                     meta={"classification_id": classification.id, "reminder": True})
        classification.reminded_at = now
        reminded += 1
    await ts.flush()
    return reminded


def business_hours_between(start: datetime, end: datetime, zone=UTC) -> float:
    """Working hours between two moments: Monday–Friday, 09:00–18:00 in `zone`. Pure.

    Wall-clock hours would make every reply that arrives at six in the evening late by breakfast,
    and the reminder would arrive while nobody is there to act on it. Counting whole weekdays has
    the same fault in a smaller way — it puts a four-hour promise at ten at night.
    """
    from datetime import timedelta

    from nexus.engagement.timekeeping import is_business_day

    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)
    if end <= start:
        return 0.0
    start, end = start.astimezone(zone), end.astimezone(zone)
    hours = 0.0
    day = start.date()
    while day <= end.date():
        if is_business_day(day):
            opens = datetime.combine(day, OFFICE_OPEN, tzinfo=zone)
            closes = datetime.combine(day, OFFICE_CLOSE, tzinfo=zone)
            overlap = min(end, closes) - max(start, opens)
            hours += max(0.0, overlap.total_seconds() / 3600)
        day += timedelta(days=1)
    return hours
```

- [ ] **Step 4: Run** `pytest tests/test_engagement_desk.py -n0 -q -k "not api and not queue_the_item and not mailbox_owner and not dark"` — expected PASS for the service-level tests.

- [ ] **Step 5: Commit**

```bash
git add nexus/engagement/desk nexus/engagement/sending/drafts.py nexus/engagement/drafting/drafter.py nexus/billing/catalog.py nexus/billing/rates.py tests/test_engagement_desk.py
git commit -m "feat(engagement): the reply desk — queue, suggested answer, four decisions"
```

Points worth reading twice:

- **`item()` is one function and both the detail screen and every action use it.** `draft_response`, `send_response` and `save_to_drafts` all start from it, so "which conversation is this" cannot answer differently depending on which button was pressed.
- **`send_response` passes `idempotency_key=f"response:{classification.id}"`**, which is the phase 07 partial unique index. A double-click sends once.
- **The ledger event carries the conversation** as well as the text sent. That pair — what arrived, what a person judged the right answer — is the training example; the text alone is not.
- **`save_to_drafts` goes through `sending/drafts.save_draft`**, the one function that builds a draft (the contact composer uses it too). It threads the draft like a real reply — the thread row's `provider_thread_id`, the answered message's provider id, `In-Reply-To`, and `References` built by `mime.references_for` — and signs it with `sending.service._signature`, so a draft and a send carry the same sign-off. An expired grant marks the mailbox `needs_reauth` before the error reaches the SDR.
- **`decide("close")` stops only an enrollment paused `needs_decision`.** Closing an item is a statement about the reply, not about every sequence the person is in.
- **`business_hours_between` is pure and takes the zone.** `remind_unanswered` reads the mailbox's timezone before the threshold check, so the office day is the SDR's own.

---

### Task 3: The API

**Files:**
- Create: `nexus/api/routers/engagement_desk.py`
- Modify: `nexus/api/routers/__init__.py`

- [ ] **Step 1: Run** `pytest tests/test_engagement_desk.py -n0 -q -k "queue_the_item or mailbox_owner or dark"` — expected FAIL: 404 on `/api/engagement/desk` with the engine on.

- [ ] **Step 2: Implement** `nexus/api/routers/engagement_desk.py`:

```python
"""The reply desk API: the queue, the suggested answer, and the SDR's decision (spec §9).

Dark with the rest of the engine until `engagement_campaigns_enabled` is on. A rep works their own
mailboxes; `?team=true` is honoured only for a manager, who can also reassign an item.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from nexus.api.deps import Principal, get_tenant_session, require
from nexus.api.routers.engagement_campaigns import require_campaigns_enabled
from nexus.core.rbac import Permission, Role, has_permission
from nexus.core.tenancy import TenantSession

router = APIRouter(prefix="/engagement/desk", tags=["engagement"],
                   dependencies=[Depends(require_campaigns_enabled)])


class QueueItemOut(BaseModel):
    id: str
    message_id: str
    category: str
    corrected_category: str | None
    confidence: float
    resolved_date: date | None
    status: str
    decision: str | None
    assigned_user_id: str | None
    contact_id: str | None
    account_id: str | None
    contact_name: str
    contact_email: str
    account_name: str
    subject: str
    preview: str
    received_at: datetime | None
    responded_at: datetime | None


class ScheduledOut(BaseModel):
    enrollment_id: str
    contact_id: str
    contact_name: str
    account_name: str
    campaign_id: str
    status: str
    status_reason: str | None
    due_at: datetime | None


class ConversationMessageOut(BaseModel):
    id: str
    direction: str
    subject: str
    body: str
    at: datetime | None


class ColleagueOut(BaseModel):
    enrollment_id: str
    contact_id: str
    campaign_id: str


class ItemOut(QueueItemOut):
    body: str
    suggested_response: str | None
    conversation: list[ConversationMessageOut]
    paused_colleagues: list[ColleagueOut]


class ResponseIn(BaseModel):
    model_config = {"extra": "forbid"}

    subject: str
    body: str


class DecisionIn(BaseModel):
    model_config = {"extra": "forbid"}

    decision: str
    reengage_on: date | None = None
    note: str = ""


class CorrectionIn(BaseModel):
    model_config = {"extra": "forbid"}

    category: str


def _is_manager(principal: Principal) -> bool:
    return has_permission(Role(principal.role), Permission.manage_engagement)


@dataclass(slots=True)
class _Names:
    contacts: dict = field(default_factory=dict)
    accounts: dict = field(default_factory=dict)

    def contact(self, contact_id: str | None):
        return self.contacts.get(contact_id)

    def account(self, account_id: str | None):
        return self.accounts.get(account_id)


async def _names(ts: TenantSession, contact_ids, account_ids) -> _Names:
    """Who each row is with, in two queries for the whole page rather than two per row."""
    from nexus.models.account import Account, Contact

    contact_ids = {c for c in contact_ids if c}
    account_ids = {a for a in account_ids if a}
    contacts = {c.id: c for c in await ts.list(Contact, Contact.id.in_(contact_ids))} \
        if contact_ids else {}
    account_ids |= {c.account_id for c in contacts.values() if c.account_id}
    accounts = {a.id: a for a in await ts.list(Account, Account.id.in_(account_ids))} \
        if account_ids else {}
    return _Names(contacts=contacts, accounts=accounts)


def _row(classification, message, names: _Names) -> dict:
    body = (getattr(message, "body_text", "") or "")
    contact = names.contact(classification.contact_id)
    account = names.account(classification.account_id
                            or getattr(contact, "account_id", None))
    return {
        "id": classification.id, "message_id": classification.message_id,
        "category": classification.category,
        "corrected_category": classification.corrected_category,
        "confidence": classification.confidence, "resolved_date": classification.resolved_date,
        "status": classification.status, "decision": classification.decision,
        "assigned_user_id": classification.assigned_user_id,
        "contact_id": classification.contact_id, "account_id": classification.account_id,
        # A reply matched to nobody still shows who wrote it, from the message itself.
        "contact_name": getattr(contact, "full_name", "") or "",
        "contact_email": getattr(contact, "email", "") or getattr(message, "from_addr", "") or "",
        "account_name": getattr(account, "name", "") or "",
        "subject": getattr(message, "subject", "") or "",
        "preview": " ".join(body.split())[:280],
        "received_at": getattr(message, "received_at", None),
        "responded_at": classification.responded_at,
    }


async def _classification(ts: TenantSession, classification_id: str, principal: Principal):
    from nexus.models.engagement import MailboxConnection, ReplyClassification

    row = await ts.get(ReplyClassification, classification_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Reply not found")
    mailbox = await ts.get(MailboxConnection, row.mailbox_connection_id)
    if mailbox is None or (mailbox.owner_user_id != principal.user_id
                           and not _is_manager(principal)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Reply not found")
    return row


def _refuse(exc: Exception) -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, str(exc))


@router.get("", response_model=list[QueueItemOut])
async def queue(
    tab: str = "needs_action", team: bool = False,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> list[QueueItemOut]:
    from nexus.engagement.desk import service
    from nexus.models.engagement import EngagementMessage

    scope = team and _is_manager(principal)
    if tab not in ("needs_action", "handled"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown tab")
    reader = service.needs_action if tab == "needs_action" else service.handled
    rows = await reader(ts, user_id=principal.user_id, team=scope)
    if not rows:
        return []
    messages = {m.id: m for m in await ts.list(
        EngagementMessage, EngagementMessage.id.in_([r.message_id for r in rows]))}
    names = await _names(ts, [r.contact_id for r in rows], [r.account_id for r in rows])
    return [QueueItemOut(**_row(r, messages.get(r.message_id), names)) for r in rows]


@router.get("/scheduled", response_model=list[ScheduledOut])
async def scheduled(
    team: bool = False,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> list[ScheduledOut]:
    from nexus.engagement.desk import service

    rows = await service.scheduled(ts, user_id=principal.user_id,
                                   team=team and _is_manager(principal))
    names = await _names(ts, [e.contact_id for e in rows], [e.account_id for e in rows])
    return [ScheduledOut(
        enrollment_id=e.id, contact_id=e.contact_id,
        contact_name=getattr(names.contact(e.contact_id), "full_name", "") or "",
        account_name=getattr(names.account(e.account_id), "name", "") or "",
        campaign_id=e.campaign_id, status=e.status, status_reason=e.status_reason,
        due_at=e.snoozed_until) for e in rows]


@router.get("/{classification_id}", response_model=ItemOut)
async def read_item(
    classification_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> ItemOut:
    from nexus.engagement.desk import service

    classification = await _classification(ts, classification_id, principal)
    detail = await service.item(ts, classification)
    names = _Names(
        contacts={detail.contact.id: detail.contact} if detail.contact else {},
        accounts={detail.account.id: detail.account} if detail.account else {})
    return ItemOut(
        **_row(classification, detail.message, names),
        body=getattr(detail.message, "body_text", "") or "",
        suggested_response=classification.suggested_response,
        conversation=[ConversationMessageOut(
            id=m.id, direction=m.direction, subject=m.subject or "", body=m.body_text or "",
            at=m.sent_at or m.received_at or m.created_at) for m in detail.conversation],
        paused_colleagues=[ColleagueOut(enrollment_id=e.id, contact_id=e.contact_id,
                                        campaign_id=e.campaign_id)
                           for e in detail.paused_colleagues])


@router.post("/{classification_id}/draft")
async def draft(
    classification_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> dict:
    from nexus.engagement.desk import service

    classification = await _classification(ts, classification_id, principal)
    try:
        return await service.draft_response(ts, classification, user_id=principal.user_id)
    except service.DeskError as exc:
        raise _refuse(exc) from exc


@router.post("/{classification_id}/send")
async def send(
    classification_id: str, body: ResponseIn,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> dict:
    from nexus.engagement.desk import service

    classification = await _classification(ts, classification_id, principal)
    try:
        result = await service.send_response(ts, classification, user_id=principal.user_id,
                                             subject=body.subject, body=body.body)
    except service.DeskError as exc:
        raise _refuse(exc) from exc
    return {"outcome": result.outcome, "reason": result.reason, "message_id": result.message_id}


@router.post("/{classification_id}/save-draft")
async def save_draft(
    classification_id: str, body: ResponseIn,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> dict:
    from nexus.engagement.desk import service

    classification = await _classification(ts, classification_id, principal)
    try:
        draft_id = await service.save_to_drafts(ts, classification, subject=body.subject,
                                                body=body.body)
    except service.DeskError as exc:
        raise _refuse(exc) from exc
    return {"provider_draft_id": draft_id}


@router.post("/{classification_id}/decide", status_code=204, response_model=None)
async def decide(
    classification_id: str, body: DecisionIn,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> None:
    from nexus.engagement.desk import service

    classification = await _classification(ts, classification_id, principal)
    try:
        await service.decide(ts, classification, body.decision, user_id=principal.user_id,
                             reengage_on=body.reengage_on, note=body.note)
    except service.DeskError as exc:
        raise _refuse(exc) from exc


@router.post("/{classification_id}/correct", status_code=204, response_model=None)
async def correct(
    classification_id: str, body: CorrectionIn,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> None:
    from nexus.engagement.desk import service

    classification = await _classification(ts, classification_id, principal)
    try:
        await service.correct(ts, classification, body.category, user_id=principal.user_id)
    except service.DeskError as exc:
        raise _refuse(exc) from exc


@router.post("/{classification_id}/assign", status_code=204, response_model=None)
async def assign(
    classification_id: str, user_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_engagement)),
) -> None:
    from nexus.engagement.desk import service

    classification = await _classification(ts, classification_id, principal)
    await service.reassign(ts, classification, user_id=user_id)


@router.post("/colleagues/{enrollment_id}/{action}", status_code=204, response_model=None)
async def colleague(
    enrollment_id: str, action: str,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> None:
    from nexus.engagement.desk import service
    from nexus.models.engagement import EngagementEnrollment

    enrollment = await ts.get(EngagementEnrollment, enrollment_id)
    if enrollment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Contact not found")
    try:
        if action == "resume":
            await service.resume_colleague(ts, enrollment, user_id=principal.user_id)
        elif action == "stop":
            await service.stop_colleague(ts, enrollment, user_id=principal.user_id)
        else:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown action")
    except (service.DeskError, ValueError) as exc:
        raise _refuse(exc) from exc
```

- [ ] **Step 3: Register it.** In `nexus/api/routers/__init__.py` add `engagement_desk` to the import block after `engagement_campaigns`, and `engagement_desk.router` to the router list after `engagement_campaigns.router`.

- [ ] **Step 4: Run** `pytest tests/test_engagement_desk.py -n0 -q` — expected PASS (19).

- [ ] **Step 5: Commit**

```bash
git add nexus/api/routers/engagement_desk.py nexus/api/routers/__init__.py
git commit -m "feat(api): the reply desk endpoints, dark until the engine is on"
```

Notes:

- The router depends on `require_campaigns_enabled`, so while the engine is dark every route 404s — including to someone who knows the path.
- **Every row says who it is with** (`contact_name`, `contact_email`, `account_name`), loaded by `_names` in two queries for the whole page, and the messages in one — never a lookup per row. A reply matched to no contact still shows its sender, from the message's own `From`.
- `_classification` is the authorisation: it resolves the row, reads its mailbox, and 404s (not 403) when the mailbox is neither yours nor visible to a manager. A 403 would confirm the id exists.
- `/scheduled` is declared **before** `/{classification_id}`, or it would be read as an id.
- Every 204 route carries `response_model=None`; FastAPI refuses a 204 with a response model.

---

### Task 4: The reminder job

**Files:**
- Modify: `nexus/workers/tasks.py`, `nexus/workers/scheduler.py`

- [ ] **Step 1: Implement the handler.** In `nexus/workers/tasks.py`, immediately before `handle_sync_mailbox`:

```python
async def handle_remind_replies(payload: dict) -> dict:
    """Nudge SDRs whose interested buyers are still waiting (spec §19). Reads every workspace that
    has an open reply, so it runs on the platform sessionmaker to find them and per tenant to act."""
    from sqlalchemy import select

    from nexus.core.db import get_platform_sessionmaker, utcnow
    from nexus.engagement import config
    from nexus.engagement.desk.service import remind_unanswered
    from nexus.models.engagement import ReplyClassification

    if not config.campaigns_enabled():
        return {"skipped": "engagement campaigns are switched off"}
    now = utcnow()
    async with get_platform_sessionmaker()() as session:
        tenant_ids = (await session.execute(
            select(ReplyClassification.tenant_id)
            .where(ReplyClassification.status == "open")
            .where(ReplyClassification.reminded_at.is_(None))
            .distinct().limit(500))).scalars().all()
    reminded = 0
    for tenant_id in tenant_ids:
        try:
            async with tenant_session(tenant_id) as ts:
                reminded += await remind_unanswered(ts, now=now)
        except Exception:  # one workspace's reminder must not stop the rest
            logger.warning("could not remind %s about waiting replies", tenant_id, exc_info=True)
    return {"tenants": len(tenant_ids), "reminded": reminded}
```

- [ ] **Step 2: Register it** in `HANDLERS`, after `"sync_mailboxes"`:

```python
    "remind_replies": handle_remind_replies,
```

- [ ] **Step 3: Add the enqueue helper**, immediately before `enqueue_ship_ledger`:

```python
async def enqueue_remind_replies(*, queue: TaskQueue | None = None) -> None:
    queue = queue or get_task_queue()
    await queue.enqueue(Job(name="remind_replies", payload={}))
```

- [ ] **Step 4: Schedule it.** In `nexus/workers/scheduler.py` import `enqueue_remind_replies` beside `enqueue_advance_engagement`, and inside the `campaigns_enabled()` block:

```python
                # The poll that catches what a missed notification would have (spec §6).
                await enqueue_sync_mailboxes(queue=queue)
                # The reply-speed reminder: a buyer who said yes is waiting on a person.
                await enqueue_remind_replies(queue=queue)
                count += 3
```

The candidate query is the narrow one — open, never reminded — so a tick on a platform with nothing waiting costs one indexed read and touches no tenant. The job rides the engagement switch, not `automation_enabled`, for the reason `advance_engagement` does: a workspace that launched a campaign expects it to run.

- [ ] **Step 5: Run** `pytest tests/test_engagement_desk.py -n0 -q -k remind` — expected PASS.

- [ ] **Step 6: Commit**

```bash
git add nexus/workers/tasks.py nexus/workers/scheduler.py
git commit -m "feat(workers): remind an SDR when an interested buyer is still waiting"
```

---

### Task 5: The provider double keeps a Drafts folder

**Files:**
- Modify: `tests/test_engagement_sending.py`

- [ ] **Step 1: Add the folder.** In `SentFolder.__init__`, beside `self.delivered`:

```python
        self.drafts: list[bytes] = []
```

- [ ] **Step 2: Add the method**, immediately before `find_sent`:

```python
    async def create_draft(self, mime: bytes, *, thread=None) -> str:
        self.drafts.append(mime)
        return f"draft-{len(self.drafts)}"
```

`create_draft` has been on the `MailProvider` protocol since phase 03 and both live providers implement it; the double did not, because nothing had called it until now.

- [ ] **Step 3: Run** `pytest tests/test_engagement_sending.py tests/test_engagement_desk.py -n0 -q` — expected PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/test_engagement_sending.py
git commit -m "test(engagement): the mailbox double keeps a Drafts folder"
```

---

### Task 6: The whole test file

**Files:**
- Create: `tests/test_engagement_desk.py`

- [ ] **Step 1: Write it**:

```python
"""The reply desk: the three tabs, the suggested answer, the send, and the four decisions
(spec §9, §19, D3, D22).

Everything here runs on the same `Mailbox` provider double as reply ingestion, so a test starts
from a real sent email and a real reply rather than a hand-built classification row.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from nexus.core.config import get_settings
from tests.conftest import auth, signup, tenant_session
from tests.test_engagement_replies import NOW, Mailbox, _mail, _sync
from tests.test_engagement_sequences import _enrollment, _launched, _run


@pytest.fixture
def mailbox_double():
    """The same provider double reply ingestion uses: a Sent folder, a Drafts folder, an inbox."""
    from nexus.engagement.mailboxes import registry

    box = Mailbox()
    registry.set_provider_factory(lambda _connection: box)
    yield box
    registry.set_provider_factory(None)


@pytest.fixture
def engine_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "engagement_campaigns_enabled", True)


async def _replied(slug, monkeypatch, box, *, body="Sounds interesting, let's talk next week."):
    """A launched campaign whose first email went out and drew one reply, already classified."""
    from email import message_from_bytes

    from nexus.models.engagement import ReplyClassification

    tid, _campaign = await _launched(slug, monkeypatch)
    enrollment = await _enrollment(tid)
    await _run(tid, enrollment.id, enrollment.next_action_at + timedelta(seconds=1))
    our_id = message_from_bytes(box.delivered[0])["Message-ID"]
    box.arrive(_mail(sender="Jane0 Buyer <jane0@acme.io>", in_reply_to=our_id, body=body))
    enrollment = await _enrollment(tid)
    await _sync(tid, enrollment.mailbox_connection_id)
    async with tenant_session(tid) as ts:
        classification = await ts.first(ReplyClassification)
        return tid, classification.id, enrollment.mailbox_connection_id


async def _reload(tid, classification_id):
    from nexus.models.engagement import ReplyClassification

    async with tenant_session(tid) as ts:
        return await ts.get(ReplyClassification, classification_id)


async def _owner_of(tid):
    from nexus.models.engagement import MailboxConnection

    async with tenant_session(tid) as ts:
        mailbox = await ts.first(MailboxConnection)
        return mailbox.owner_user_id


# ---- pure rules ----------------------------------------------------------------------------------

def test_waiting_time_is_counted_in_business_hours_not_wall_clock():
    from nexus.engagement.desk.service import business_hours_between

    friday_evening = datetime(2026, 9, 18, 17, 0, tzinfo=UTC)
    # Saturday morning: 14 hours later by the clock, one of them inside a working day.
    assert business_hours_between(friday_evening, datetime(2026, 9, 19, 7, 0, tzinfo=UTC)) == 1
    # Monday 10:00 — one hour of Friday, none of the weekend, one of Monday.
    monday = datetime(2026, 9, 21, 10, 0, tzinfo=UTC)
    assert business_hours_between(friday_evening, monday) == pytest.approx(2.0, abs=0.01)
    assert business_hours_between(monday, friday_evening) == 0
    # The clock runs in the reader's zone: 08:00 UTC is already 09:30 in Kolkata.
    assert business_hours_between(datetime(2026, 9, 21, 8, 0, tzinfo=UTC), monday,
                                  ZoneInfo("Asia/Kolkata")) == pytest.approx(2.0, abs=0.01)


async def test_a_decision_the_desk_does_not_have_is_refused(mailbox_double, monkeypatch):
    from nexus.engagement.desk.service import DeskError, decide

    tid, classification_id, _mailbox = await _replied("deskbad", monkeypatch, mailbox_double)
    async with tenant_session(tid) as ts:
        classification = await _fetch(ts, classification_id)
        with pytest.raises(DeskError):
            await decide(ts, classification, "ghost", user_id="u1")
        # Coming back on a date needs the date: a snooze with no day is a silent stop.
        with pytest.raises(DeskError):
            await decide(ts, classification, "reengage", user_id="u1")


async def _fetch(ts, classification_id):
    from nexus.models.engagement import ReplyClassification

    return await ts.get(ReplyClassification, classification_id)


# ---- the three tabs ------------------------------------------------------------------------------

async def test_the_queue_is_the_reps_own_mailboxes_and_a_manager_can_see_the_team(
    mailbox_double, monkeypatch,
):
    from nexus.engagement.desk.service import handled, needs_action

    tid, classification_id, _mailbox = await _replied("deskqueue", monkeypatch, mailbox_double)
    owner = await _owner_of(tid)
    async with tenant_session(tid) as ts:
        mine = await needs_action(ts, user_id=owner)
        assert [c.id for c in mine] == [classification_id]
        # A colleague works their own mailboxes and sees nothing of this one...
        assert await needs_action(ts, user_id="someone-else") == []
        # ...unless they are a manager asking for the team.
        team = await needs_action(ts, user_id="someone-else", team=True)
        assert [c.id for c in team] == [classification_id]
        # An open reply is not in Handled.
        assert await handled(ts, user_id=owner) == []


async def test_scheduled_lists_what_is_waiting_for_a_date(mailbox_double, monkeypatch):
    from nexus.engagement.desk.service import scheduled

    tid, _classification_id, _mailbox = await _replied(
        "desksched", monkeypatch, mailbox_double, body="Not now — try me in June please.")
    enrollment = await _enrollment(tid)
    assert enrollment.status == "snoozed"
    owner = await _owner_of(tid)
    async with tenant_session(tid) as ts:
        waiting = await scheduled(ts, user_id=owner)
        assert [e.id for e in waiting] == [enrollment.id]


async def test_an_item_carries_the_whole_conversation_and_the_colleagues_it_paused(
    mailbox_double, monkeypatch,
):
    from nexus.engagement.desk.service import item
    from nexus.models.engagement import EngagementEnrollment

    tid, classification_id, _mailbox = await _replied("deskitem", monkeypatch, mailbox_double)
    async with tenant_session(tid) as ts:
        classification = await _fetch(ts, classification_id)
        # A colleague at the same account, paused by this reply (D3).
        colleague = EngagementEnrollment(
            campaign_id=(await ts.first(EngagementEnrollment)).campaign_id + "x",
            contact_id=classification.contact_id, account_id=classification.account_id,
            status="paused", status_reason="colleague_replied")
        ts.add(colleague)
        await ts.flush()
        detail = await item(ts, classification)
    assert [m.direction for m in detail.conversation] == ["out", "in"]
    assert detail.contact.email == "jane0@acme.io" and detail.account.name == "Acme Robotics"
    assert [e.id for e in detail.paused_colleagues] == [colleague.id]


# ---- the suggested answer ------------------------------------------------------------------------

async def test_the_suggested_answer_is_written_charged_and_kept_for_the_sdr(
    mailbox_double, monkeypatch,
):
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.rates import sync_rates
    from nexus.engagement.desk.service import draft_response
    from nexus.models.billing import BillingUsageEvent

    await sync_catalog()
    await sync_rates()
    tid, classification_id, _mailbox = await _replied("deskdraft", monkeypatch, mailbox_double)
    # Metering is what is under test, so it is switched on after the campaign that set it up.
    monkeypatch.setattr(get_settings(), "billing_enforcement", "shadow")
    owner = await _owner_of(tid)
    async with tenant_session(tid) as ts:
        classification = await _fetch(ts, classification_id)
        written = await draft_response(ts, classification, user_id=owner)
    assert written["subject"] and written["body"]
    stored = await _reload(tid, classification_id)
    assert stored.suggested_response.startswith(written["subject"])
    async with tenant_session(tid) as ts:
        events = await ts.list(BillingUsageEvent, BillingUsageEvent.capability_id == "ai.reply_draft")
    # Suggesting a reply is its own line on the bill, not the outbound draft's.
    assert len(events) == 1 and events[0].quantity == 1


async def test_sending_the_answer_threads_it_closes_the_item_and_records_the_conversation(
    mailbox_double, monkeypatch,
):
    from email import message_from_bytes

    from nexus.engagement.desk.service import send_response
    from nexus.engagement.ledger import consent
    from nexus.models.ledger import LedgerOutbox

    tid, classification_id, _mailbox = await _replied("desksend", monkeypatch, mailbox_double)
    owner = await _owner_of(tid)
    async with tenant_session(tid) as ts:
        await consent.record(ts, status_value="on", source="settings", user_id=None)
        classification = await _fetch(ts, classification_id)
        result = await send_response(ts, classification, user_id=owner,
                                     subject="Re: Quick question", body="Thursday at 10 works.")
    assert result.sent
    reply = message_from_bytes(mailbox_double.delivered[-1])
    assert reply["To"] == "jane0@acme.io" and reply["In-Reply-To"]
    stored = await _reload(tid, classification_id)
    assert stored.responded_at is not None and stored.status == "done"
    async with tenant_session(tid) as ts:
        events = await ts.list(LedgerOutbox,
                               LedgerOutbox.event_type == "response.sent")
    # The ledger keeps what the SDR sent AND the conversation it answered — the training pair.
    assert len(events) == 1
    assert events[0].payload["payload"]["body"] == "Thursday at 10 works."
    assert [m["direction"] for m in events[0].payload["payload"]["conversation"]] == ["out", "in"]


async def test_an_unclear_reply_stays_open_after_an_answer_because_it_still_needs_a_decision(
    mailbox_double, monkeypatch,
):
    from nexus.engagement.desk.service import send_response

    tid, classification_id, _mailbox = await _replied(
        "deskunclear", monkeypatch, mailbox_double,
        body="Not interested. Maybe reach out next year.")
    assert (await _reload(tid, classification_id)).category == "unclear"
    owner = await _owner_of(tid)
    async with tenant_session(tid) as ts:
        classification = await _fetch(ts, classification_id)
        await send_response(ts, classification, user_id=owner, subject="Re: Quick question",
                            body="Understood — I will check back then.")
    stored = await _reload(tid, classification_id)
    assert stored.responded_at is not None and stored.status == "open"


async def test_saving_to_drafts_sends_nothing_and_charges_nothing(mailbox_double, monkeypatch):
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.rates import sync_rates
    from nexus.engagement.desk.service import save_to_drafts
    from nexus.models.billing import BillingUsageEvent

    await sync_catalog()
    await sync_rates()
    tid, classification_id, _mailbox = await _replied("deskkeep", monkeypatch, mailbox_double)
    monkeypatch.setattr(get_settings(), "billing_enforcement", "shadow")
    delivered = len(mailbox_double.delivered)
    async with tenant_session(tid) as ts:
        classification = await _fetch(ts, classification_id)
        draft_id = await save_to_drafts(ts, classification, subject="Re: Quick question",
                                        body="Let me come back to you.")
        sends = await ts.list(BillingUsageEvent, BillingUsageEvent.capability_id == "outreach.email_send")
    assert draft_id == "draft-1" and len(mailbox_double.drafts) == 1
    assert len(mailbox_double.delivered) == delivered and sends == []


# ---- the four decisions --------------------------------------------------------------------------

async def test_coming_back_later_snoozes_every_live_enrollment_to_that_morning(
    mailbox_double, monkeypatch,
):
    from nexus.engagement.desk.service import decide

    tid, classification_id, _mailbox = await _replied("deskre", monkeypatch, mailbox_double)
    owner = await _owner_of(tid)
    async with tenant_session(tid) as ts:
        classification = await _fetch(ts, classification_id)
        await decide(ts, classification, "reengage", user_id=owner,
                     reengage_on=date(2027, 3, 4), note="Asked for March")
    enrollment = await _enrollment(tid)
    assert enrollment.status == "snoozed"
    local = enrollment.snoozed_until.astimezone(ZoneInfo(enrollment.contact_timezone))
    assert (local.year, local.month, local.day, local.hour) == (2027, 3, 4, 9)
    stored = await _reload(tid, classification_id)
    assert stored.decision == "reengage" and stored.status == "done"


async def test_blocking_a_person_suppresses_the_address_and_stops_the_sequence(
    mailbox_double, monkeypatch,
):
    from nexus.engagement.desk.service import decide
    from nexus.engagement.suppression.service import active_block

    tid, classification_id, _mailbox = await _replied("deskblock", monkeypatch, mailbox_double)
    owner = await _owner_of(tid)
    async with tenant_session(tid) as ts:
        classification = await _fetch(ts, classification_id)
        await decide(ts, classification, "block", user_id=owner)
        block = await active_block(ts, "jane0@acme.io")
    assert block is not None and block.reason == "manual"
    assert (await _enrollment(tid)).status == "stopped"


async def test_a_meeting_is_recorded_as_an_outcome_the_dashboards_can_see(
    mailbox_double, monkeypatch,
):
    from nexus.engagement.desk.service import decide
    from nexus.models.outcome import Outcome

    tid, classification_id, _mailbox = await _replied("deskmeet", monkeypatch, mailbox_double)
    owner = await _owner_of(tid)
    async with tenant_session(tid) as ts:
        classification = await _fetch(ts, classification_id)
        await decide(ts, classification, "meeting", user_id=owner)
        outcomes = await ts.list(Outcome)
    assert [o.stage for o in outcomes] == ["meeting"]
    assert outcomes[0].meta["source"] == "engagement_reply_desk"
    assert (await _enrollment(tid)).status == "stopped"


async def test_a_correction_is_the_label_the_ledger_trains_on(mailbox_double, monkeypatch):
    from nexus.engagement.desk.service import correct
    from nexus.engagement.ledger import consent
    from nexus.models.ledger import LedgerOutbox

    tid, classification_id, _mailbox = await _replied("deskfix", monkeypatch, mailbox_double)
    owner = await _owner_of(tid)
    async with tenant_session(tid) as ts:
        await consent.record(ts, status_value="on", source="settings", user_id=None)
        classification = await _fetch(ts, classification_id)
        await correct(ts, classification, "question", user_id=owner)
        events = await ts.list(LedgerOutbox,
                               LedgerOutbox.event_type == "reply.corrected")
    stored = await _reload(tid, classification_id)
    assert stored.corrected_category == "question" and stored.label_source == "sdr_corrected"
    assert events[0].payload["payload"]["ai_category"] == "interested"
    # Agreeing with the AI is a different label from correcting it, and worth as much.
    async with tenant_session(tid) as ts:
        classification = await _fetch(ts, classification_id)
        await correct(ts, classification, "interested", user_id=owner)
    assert (await _reload(tid, classification_id)).label_source == "sdr_confirmed"


async def test_a_paused_colleague_resumes_only_because_a_person_said_so(
    mailbox_double, monkeypatch,
):
    from nexus.engagement.desk.service import DeskError, resume_colleague, stop_colleague
    from nexus.models.engagement import EngagementEnrollment

    tid, classification_id, _mailbox = await _replied("deskcol", monkeypatch, mailbox_double)
    owner = await _owner_of(tid)
    async with tenant_session(tid) as ts:
        first = await ts.first(EngagementEnrollment)
        colleague = EngagementEnrollment(
            campaign_id=first.campaign_id, contact_id=first.contact_id + "b",
            account_id=first.account_id, status="paused", status_reason="colleague_replied",
            mailbox_connection_id=first.mailbox_connection_id)
        other = EngagementEnrollment(
            campaign_id=first.campaign_id, contact_id=first.contact_id + "c",
            account_id=first.account_id, status="paused", status_reason="manual")
        ts.add(colleague)
        ts.add(other)
        await ts.flush()
        await resume_colleague(ts, colleague, user_id=owner)
        assert colleague.status == "active"
        # A pause somebody else chose is not the desk's to undo.
        with pytest.raises(DeskError):
            await resume_colleague(ts, other, user_id=owner)
        await stop_colleague(ts, other, user_id=owner)
        assert other.status == "stopped"


# ---- the reply-speed reminder --------------------------------------------------------------------

async def test_a_waiting_buyer_is_reminded_once_after_four_business_hours(
    mailbox_double, monkeypatch,
):
    from nexus.engagement.desk.service import remind_unanswered
    from nexus.models.alerts import Alert

    tid, classification_id, _mailbox = await _replied("deskremind", monkeypatch, mailbox_double)
    async with tenant_session(tid) as ts:
        classification = await _fetch(ts, classification_id)
        classification.created_at = datetime(2026, 9, 22, 9, 0, tzinfo=UTC)  # a Tuesday
        await ts.flush()
    async with tenant_session(tid) as ts:
        # Three hours later: still inside the promise.
        assert await remind_unanswered(ts, now=datetime(2026, 9, 22, 12, 0, tzinfo=UTC)) == 0
        assert await remind_unanswered(ts, now=datetime(2026, 9, 22, 14, 0, tzinfo=UTC)) == 1
        # A second sweep says nothing more about the same fact.
        assert await remind_unanswered(ts, now=datetime(2026, 9, 22, 18, 0, tzinfo=UTC)) == 0
        alerts = await ts.list(Alert)
    assert sum(1 for a in alerts if a.meta.get("reminder")) == 1
    assert (await _reload(tid, classification_id)).reminded_at is not None


async def test_an_answered_reply_is_never_chased(mailbox_double, monkeypatch):
    from nexus.core.db import utcnow
    from nexus.engagement.desk.service import remind_unanswered

    tid, classification_id, _mailbox = await _replied("deskquiet", monkeypatch, mailbox_double)
    async with tenant_session(tid) as ts:
        classification = await _fetch(ts, classification_id)
        classification.created_at = datetime(2026, 9, 22, 9, 0, tzinfo=UTC)
        classification.responded_at = utcnow()
        await ts.flush()
    async with tenant_session(tid) as ts:
        assert await remind_unanswered(ts, now=datetime(2026, 9, 22, 18, 0, tzinfo=UTC)) == 0


# ---- the API -------------------------------------------------------------------------------------

async def test_the_desk_is_not_there_while_the_engine_is_dark(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "engagement_campaigns_enabled", False)
    token = await signup(client, slug="deskdark", email="sam@deskdark.com", company="Dark")
    assert (await client.get("/api/engagement/desk", headers=auth(token))).status_code == 404


async def test_the_queue_the_item_and_a_decision_through_the_api(
    client, engine_on, mailbox_double, monkeypatch,
):
    from nexus.models.account import Account, Contact
    from nexus.models.engagement import (
        EngagementMessage,
        EngagementThread,
        MailboxConnection,
        ReplyClassification,
    )
    from tests.conftest import principal_from_token

    monkeypatch.setattr(get_settings(), "billing_enforcement", "off")
    token = await signup(client, slug="deskapi", email="sam@deskapi.com", company="Desk")
    me = principal_from_token(token)
    async with tenant_session(me.tenant_id) as ts:
        mailbox = MailboxConnection(owner_user_id=me.user_id, provider="google",
                                    email="sam@deskapi.com", status="connected",
                                    timezone="Europe/London")
        account = Account(name="Acme Robotics", domain="acme.io")
        ts.add(mailbox)
        ts.add(account)
        await ts.flush()
        contact = Contact(account_id=account.id, full_name="Jane Buyer", email="jane@acme.io")
        thread = EngagementThread(mailbox_connection_id=mailbox.id, provider_thread_id="t-1",
                                  base_subject="Quick question")
        ts.add(contact)
        ts.add(thread)
        await ts.flush()
        message = EngagementMessage(mailbox_connection_id=mailbox.id, thread_id=thread.id,
                                    contact_id=contact.id, direction="in", kind="reply",
                                    status="received", subject="Re: Quick question",
                                    body_text="Yes — how does Tuesday look?",
                                    from_addr="jane@acme.io", to_addrs=["sam@deskapi.com"],
                                    inbound_kind="human", received_at=NOW)
        ts.add(message)
        await ts.flush()
        ts.add(ReplyClassification(message_id=message.id, mailbox_connection_id=mailbox.id,
                                   contact_id=contact.id, account_id=account.id,
                                   category="question", confidence=0.9, status="open"))
        await ts.flush()

    queue = await client.get("/api/engagement/desk", headers=auth(token))
    assert queue.status_code == 200, queue.text
    rows = queue.json()
    assert len(rows) == 1 and rows[0]["category"] == "question"
    assert rows[0]["preview"] == "Yes — how does Tuesday look?"
    # The queue says who each reply is from without a request per row.
    assert (rows[0]["contact_name"], rows[0]["contact_email"], rows[0]["account_name"]) \
        == ("Jane Buyer", "jane@acme.io", "Acme Robotics")

    item = (await client.get(f"/api/engagement/desk/{rows[0]['id']}",
                             headers=auth(token))).json()
    assert item["body"] == "Yes — how does Tuesday look?"
    assert [m["direction"] for m in item["conversation"]] == ["in"]

    decided = await client.post(f"/api/engagement/desk/{rows[0]['id']}/decide",
                                headers=auth(token), json={"decision": "close"})
    assert decided.status_code == 204, decided.text
    assert (await client.get("/api/engagement/desk", headers=auth(token))).json() == []
    handled = (await client.get("/api/engagement/desk?tab=handled",
                                headers=auth(token))).json()
    assert [r["decision"] for r in handled] == ["close"]

    # A date is required for a re-engagement, and a decision nobody defined is refused.
    refused = await client.post(f"/api/engagement/desk/{rows[0]['id']}/decide",
                                headers=auth(token), json={"decision": "reengage"})
    assert refused.status_code == 409


async def _member(client, owner_token: str, email: str, role: str) -> str:
    r = await client.post("/api/workspace/members", headers=auth(owner_token), json={
        "email": email, "full_name": email.split("@")[0], "password": "password123",
        "role": role})
    assert r.status_code == 201, r.text
    r = await client.post("/api/auth/login", json={"email": email, "password": "password123"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


async def test_a_reply_belongs_to_its_mailbox_owner_and_a_manager_can_hand_it_over(
    client, engine_on,
):
    from nexus.core.security import decode_access_token
    from nexus.models.account import Account, Contact
    from nexus.models.engagement import EngagementMessage, MailboxConnection, ReplyClassification
    from tests.conftest import principal_from_token

    owner_token = await signup(client, slug="deskpriv", email="boss@deskpriv.com", company="Priv")
    me = principal_from_token(owner_token)
    ann = await _member(client, owner_token, "ann@deskpriv.com", "rep")
    bob = await _member(client, owner_token, "bob@deskpriv.com", "rep")
    ann_id = (decode_access_token(ann) or {})["sub"]
    bob_id = (decode_access_token(bob) or {})["sub"]

    async with tenant_session(me.tenant_id) as ts:
        mailbox = MailboxConnection(owner_user_id=ann_id, provider="google",
                                    email="ann@deskpriv.com", status="connected")
        account = Account(name="Acme Robotics", domain="acme.io")
        ts.add(mailbox)
        ts.add(account)
        await ts.flush()
        contact = Contact(account_id=account.id, full_name="Jane Buyer", email="jane@acme.io")
        ts.add(contact)
        await ts.flush()
        message = EngagementMessage(mailbox_connection_id=mailbox.id, contact_id=contact.id,
                                    direction="in", kind="reply", status="received",
                                    subject="Re: Hello", body_text="Who is this?",
                                    from_addr="jane@acme.io", to_addrs=["ann@deskpriv.com"],
                                    inbound_kind="human", received_at=NOW)
        ts.add(message)
        await ts.flush()
        classification = ReplyClassification(message_id=message.id,
                                             mailbox_connection_id=mailbox.id,
                                             contact_id=contact.id, account_id=account.id,
                                             category="question", confidence=0.9, status="open")
        ts.add(classification)
        await ts.flush()
        classification_id = classification.id

    # Ann's mailbox, Ann's reply.
    assert [r["id"] for r in (await client.get("/api/engagement/desk",
                                               headers=auth(ann))).json()] == [classification_id]
    # Bob works his own mailboxes, and asking for the team does not make him a manager.
    assert (await client.get("/api/engagement/desk", headers=auth(bob))).json() == []
    assert (await client.get("/api/engagement/desk?team=true", headers=auth(bob))).json() == []
    assert (await client.get(f"/api/engagement/desk/{classification_id}",
                             headers=auth(bob))).status_code == 404
    assert (await client.post(f"/api/engagement/desk/{classification_id}/assign?user_id={bob_id}",
                              headers=auth(bob))).status_code == 403

    # The owner sees the team's queue and can hand the reply to Bob.
    team = (await client.get("/api/engagement/desk?team=true", headers=auth(owner_token))).json()
    assert [r["id"] for r in team] == [classification_id]
    assigned = await client.post(
        f"/api/engagement/desk/{classification_id}/assign?user_id={bob_id}",
        headers=auth(owner_token))
    assert assigned.status_code == 204, assigned.text
    team = (await client.get("/api/engagement/desk?team=true", headers=auth(owner_token))).json()
    assert team[0]["assigned_user_id"] == bob_id
```

- [ ] **Step 2: Run** `pytest tests/test_engagement_desk.py -q -n 4` — expected PASS (19).

- [ ] **Step 3: Run the neighbours** `pytest tests/test_engagement_*.py tests/test_billing*.py tests/test_worker*.py -q -n 6` — expected PASS.

- [ ] **Step 4: Lint** `ruff check nexus tests` — expected `All checks passed!`.

Three things the tests are shaped around:

- **They start from a real reply.** `_replied` launches a campaign, sends its first email through the double, delivers an answer into the inbox and runs phase 09's ingestion — so every desk test runs against a classification the product actually produced, rather than a row written by hand. The exception is the API tests, which build the row directly because they need named reps in a workspace created through signup.
- **Metering is switched on after the world is built.** `_launched` sets `billing_enforcement` to `off` to get a campaign launched without a credit grant; a test that asserts on usage rows sets `shadow` afterwards, so the rows it counts are only the ones its own action wrote.
- **Ledger assertions record consent first.** `emit()` writes nothing for a workspace whose newest `training_consents` row is not `on`, so the tests that read `ledger_outbox` call `consent.record(..., status_value="on")` inside the test. The envelope nests the event body under `payload["payload"]`.
- **Fixtures are local.** `mailbox_double` and `engine_on` are redefined here rather than imported: ruff reads an imported fixture used as a parameter as a redefinition (F811).

---

## What phase 11 depends on

The desk API is the data the Replies screen renders: `GET /engagement/desk` for Needs action and Handled, `GET /engagement/desk/scheduled`, `GET /engagement/desk/{id}` for the conversation and the suggested answer, and the seven actions (`draft`, `send`, `save-draft`, `decide`, `correct`, `assign`, `colleagues/{id}/{resume|stop}`). Phase 11 adds no server behaviour here — if a screen needs a field this router does not return, that is a change to this phase, not a fetch the page makes for itself.
