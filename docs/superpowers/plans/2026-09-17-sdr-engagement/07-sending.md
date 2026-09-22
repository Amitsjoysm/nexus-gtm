# Phase 07: Sending Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Send one email from an SDR's own Gmail or Microsoft 365 mailbox — threaded, with one-click unsubscribe, checked immediately before the provider call — and never deliver the same message twice, whatever the provider does.

**Architecture:** `nexus/engagement/sending/` holds four modules. `mime.py` (pure) builds the raw RFC 5322 message both providers are sent, because Graph's JSON send cannot carry `List-Unsubscribe`. `checks.py` (pure) turns already-loaded rows into `send`, `hold` or `stop`. `service.send()` writes the outbound row `queued` before the provider call, reconciles against the Sent folder by `X-Nexus-Ref` after any uncertain result, pauses the mailbox on a provider limit, meters the send and records `message.sent`. `limits.py` counts today's sends in the owner's timezone for the volume warning.

**Tech Stack:** Python `email` (MIME), async SQLAlchemy 2.0 savepoints, the phase 03 provider protocol, `metered()`.

**Roadmap:** [00-roadmap.md](00-roadmap.md). **Spec:** §5, D9, D10, D11, D16, D17. **Depends on:** phases 03 (providers), 04 (do-not-contact, unsubscribe links), 05 (`emit`).

**Verified:** implemented on `feat/sdr-engagement` on top of phases 01–06 and run in the CI image: `tests/test_engagement_sending.py` (14 passed), the engagement and mailbox suites, `ruff check nexus tests tests_live`, and `npm run typecheck`.

---

## Files

| Action | Path | Responsibility |
|---|---|---|
| Create | `nexus/engagement/sending/__init__.py` | package |
| Create | `nexus/engagement/sending/mime.py` | the message and its headers |
| Create | `nexus/engagement/sending/checks.py` | send / hold / stop |
| Create | `nexus/engagement/sending/service.py` | exactly-once send |
| Create | `nexus/engagement/sending/limits.py` | today's count, the volume warning |
| Modify | `nexus/engagement/mailboxes/registry.py` | `set_provider_factory` test seam |
| Modify | `nexus/api/routers/engagement_mailboxes.py`, `frontend/src/lib/types.ts`, `frontend/src/pages/engagement/MailboxesPage.tsx` | the warning on the mailbox card |
| Create | `tests/test_engagement_sending.py` | message, checks, volume, exactly-once |

---

### Task 1: The message

**Files:** Create `nexus/engagement/sending/__init__.py`, `nexus/engagement/sending/mime.py`; Test `tests/test_engagement_sending.py`

- [ ] **Step 1: Write the failing tests** — create `tests/test_engagement_sending.py` with the docstring, imports, `NOW`, `SentFolder`, the `folder` fixture, `_world`, `_load` and the three message tests from the final file (Task 5).
- [ ] **Step 2: Run** `pytest tests/test_engagement_sending.py -n0 -q` — expected FAIL: `No module named 'nexus.engagement.sending'`.
- [ ] **Step 3: Implement.** `nexus/engagement/sending/__init__.py`:

```python
"""Sending from an SDR's own mailbox: MIME, pre-send checks, exactly-once (spec §5)."""
```

`nexus/engagement/sending/mime.py`:

```python
"""The MIME message we hand to Gmail or Graph (spec §5).

Both providers are sent **raw MIME** rather than their JSON send APIs, for one reason: the
unsubscribe headers. Graph's `sendMail` JSON cannot set `List-Unsubscribe`, and RFC 8058 one-click
is what keeps bulk outreach out of spam folders and gives a recipient a way out that is not a reply
(D11). Plain text only, matching the existing composer (D9).

Five headers carry the whole threading and compliance story:

* ``Message-ID`` — ours, generated and stored BEFORE the send, so a reply's ``In-Reply-To`` can be
  matched to the message it answers even if the provider call times out.
* ``In-Reply-To`` / ``References`` — the latest message in the conversation and the whole chain, so
  every mail client shows one thread (D16).
* ``List-Unsubscribe`` / ``List-Unsubscribe-Post`` — the signed one-click link and a mailto.
* ``X-Nexus-Ref`` — a ULID used only to find a message in Sent when we are not sure it was sent.
"""
from __future__ import annotations

from email.message import EmailMessage
from email.utils import formatdate, make_msgid

MAX_REFERENCES = 20


def new_rfc_message_id(domain: str = "") -> str:
    """A globally unique ``Message-ID``. The domain is cosmetic; uniqueness comes from the UUID."""
    return make_msgid(domain=(domain or "").strip() or None)


def references_for(parent_references: str, parent_message_id: str) -> str:
    """The References chain for a reply: the parent's chain plus the parent itself.

    Trimmed to the most recent ``MAX_REFERENCES``, keeping the FIRST id: clients use the head to
    identify the thread's root, and an unbounded chain is a header that grows with every follow-up.
    """
    ids = [part for part in (parent_references or "").split() if part]
    if parent_message_id and parent_message_id not in ids:
        ids.append(parent_message_id)
    if len(ids) > MAX_REFERENCES:
        ids = ids[:1] + ids[-(MAX_REFERENCES - 1):]
    return " ".join(ids)


def opt_out_line(unsubscribe_url: str) -> str:
    """One plain line under the signature (D11). A reply of "no" is offered first, because for a
    real prospect that is the outcome an SDR wants to hear about."""
    return f'Not relevant? Just reply "no", or unsubscribe: {unsubscribe_url}'


def compose_body(body: str, signature: str, unsubscribe_url: str) -> str:
    """Body → signature → opt-out line, each separated by a blank line.

    Idempotent about the opt-out line: `outreach/signature.append_signature` already tolerates a rep
    who typed their own sign-off, and a draft that was shown with its footer must not gain a second
    one when it is sent."""
    text = (body or "").rstrip()
    signature = (signature or "").strip()
    if signature and signature not in text:
        text = f"{text}\n\n{signature}"
    line = opt_out_line(unsubscribe_url)
    if unsubscribe_url and unsubscribe_url not in text:
        text = f"{text}\n\n{line}"
    return text + "\n"


def build_message(
    *,
    from_addr: str,
    from_name: str = "",
    to_addr: str,
    subject: str,
    body: str,
    message_id: str,
    ref: str,
    unsubscribe_url: str = "",
    unsubscribe_mailto: str = "",
    signature: str = "",
    in_reply_to: str = "",
    references: str = "",
    cc: list[str] | None = None,
) -> EmailMessage:
    """The message, ready to serialise. Pure: same inputs, same bytes."""
    message = EmailMessage()
    message["From"] = f"{from_name} <{from_addr}>" if from_name else from_addr
    message["To"] = to_addr
    if cc:
        message["Cc"] = ", ".join(cc)
    message["Subject"] = subject
    message["Message-ID"] = message_id
    message["Date"] = formatdate(localtime=True)
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
    if references:
        message["References"] = references
    if unsubscribe_url:
        targets = [f"<{unsubscribe_url}>"]
        if unsubscribe_mailto:
            targets.append(f"<mailto:{unsubscribe_mailto}>")
        message["List-Unsubscribe"] = ", ".join(targets)
        # RFC 8058: without this header a scanner that follows the link cannot unsubscribe anyone,
        # and with it the client shows its own Unsubscribe button instead of a spam report.
        message["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    message["X-Nexus-Ref"] = ref
    message.set_content(compose_body(body, signature, unsubscribe_url))
    return message


def to_bytes(message: EmailMessage) -> bytes:
    return message.as_bytes()


def to_text(message: EmailMessage) -> str:
    return message.as_string()
```

- [ ] **Step 4: Run** — expected `3 passed`.
- [ ] **Step 5: Commit** — `git add nexus/engagement/sending tests/test_engagement_sending.py && git commit -m "feat(engagement): the raw message, with threading and one-click unsubscribe headers"`

---

### Task 2: The checks

**Files:** Create `nexus/engagement/sending/checks.py`

- [ ] **Step 1: Write the failing test** — add `test_stops_come_before_holds_and_a_first_touch_is_not_held_for_quality`.
- [ ] **Step 2: Run** — expected FAIL: `No module named 'nexus.engagement.sending.checks'`.
- [ ] **Step 3: Implement** `nexus/engagement/sending/checks.py`:

```python
"""The checks that run immediately before every send (spec §5).

**At send time, not at scheduling time.** A step is scheduled days ahead; between then and the send
the person can unsubscribe, reply, be paused by a colleague, or the mailbox can lose its grant. Each
of those makes the send wrong, and only the last check before the provider call can see them.

The outcome is one of three words, and the difference between them is what happens next:

* ``send`` — go ahead.
* ``hold`` — do not send, leave the step due. The condition passes: a paused mailbox reconnects, a
  provider limit resets, a colleague resumes. Nothing is lost.
* ``stop`` — do not send, and this enrollment is over for this contact: they unsubscribed, they
  replied, or they are on the do-not-contact list.

Everything here is a decision ABOUT already-loaded rows, so the ordering is cheap to state: the
stops come first, because a workspace that has been told to stop must stop even if its mailbox is
also broken.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

SEND = "send"
HOLD = "hold"
STOP = "stop"


@dataclass(frozen=True, slots=True)
class Decision:
    outcome: str
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome == SEND


GO = Decision(SEND)


def decide(
    *,
    suppressed_reason: str = "",
    inbound_since_scheduled: bool = False,
    enrollment_status: str = "active",
    paused_by_colleague: bool = False,
    mailbox_status: str = "connected",
    mailbox_paused_until: datetime | None = None,
    quality_problems: list[str] | None = None,
    is_first_touch: bool = False,
    credits_ok: bool = True,
    now: datetime | None = None,
) -> Decision:
    """Pure. One call per send, with rows the caller has already read."""
    if suppressed_reason:
        return Decision(STOP, f"the address is on the do-not-contact list ({suppressed_reason})")
    if inbound_since_scheduled:
        # They answered. Whatever this step was going to say, it is now the wrong thing to say.
        return Decision(STOP, "the contact replied after this step was scheduled")
    if enrollment_status in ("stopped", "completed"):
        return Decision(STOP, f"the enrollment is {enrollment_status}")
    if paused_by_colleague or enrollment_status == "paused":
        return Decision(HOLD, "a colleague paused this enrollment")
    if enrollment_status == "snoozed":
        return Decision(HOLD, "the enrollment is snoozed")
    if mailbox_status != "connected":
        return Decision(HOLD, f"the mailbox is {mailbox_status}")
    if mailbox_paused_until and now and mailbox_paused_until > now:
        return Decision(HOLD, "the mailbox is paused until its provider limit resets")
    problems = [p for p in (quality_problems or []) if p]
    if problems and not is_first_touch:
        # A first email is reviewed by a person before it goes; a follow-up is drafted just in time
        # and nobody is watching, so a failing one waits for review rather than reaching a buyer
        # (D17).
        return Decision(HOLD, f"the draft needs review: {', '.join(problems[:3])}")
    if not credits_ok:
        return Decision(HOLD, "the workspace cannot cover this send")
    return GO
```

- [ ] **Step 4: Run** — expected `4 passed`.
- [ ] **Step 5: Commit** — `git commit -am "feat(engagement): pre-send checks — stops before holds, a watched first email is not held"`

---

### Task 3: Today's count and the warning

**Files:** Create `nexus/engagement/sending/limits.py`; Modify `nexus/api/routers/engagement_mailboxes.py`, `frontend/src/lib/types.ts`, `frontend/src/pages/engagement/MailboxesPage.tsx`

- [ ] **Step 1: Write the failing tests** — add `test_the_volume_warning_appears_only_above_fifty_and_never_blocks` and `test_today_is_the_mailbox_owners_day_not_the_utc_day`.
- [ ] **Step 2: Run** — expected FAIL: `No module named 'nexus.engagement.sending.limits'`.
- [ ] **Step 3: Implement** `nexus/engagement/sending/limits.py`:

```python
"""How much a mailbox has sent today, and the warning above 50 (spec §5, D10).

**A warning, never a block.** The product does not cap sending (D10): a hard limit would stop an SDR
who knows their domain is warmed, and the real limits are the providers' own, which pause the mailbox
when they bite. What the product owes the SDR is the number and the risk, stated before they approve
a batch and while they are over it.

"Today" is the mailbox owner's day, from local midnight in the mailbox's timezone. A UTC day would
reset mid-afternoon for an SDR in California and count yesterday's evening sends for one in Sydney.
"""
from __future__ import annotations

from datetime import UTC, datetime

#: Above this many a day, the chance of being marked as spam rises for most domains.
VOLUME_WARNING_THRESHOLD = 50


async def sent_today(ts, mailbox, *, now: datetime | None = None) -> int:
    """Outbound messages this mailbox has sent since its local midnight."""
    from sqlalchemy import func, select

    from nexus.core.db import utcnow
    from nexus.engagement.timekeeping import local_day_start, zone_or_none
    from nexus.models.engagement import EngagementMessage

    moment = now or utcnow()
    start = local_day_start(moment, zone_or_none(mailbox.timezone) or UTC)
    # `ts.select` takes a model; an aggregate names the tenant explicitly instead.
    stmt = (select(func.count(EngagementMessage.id))
            .where(EngagementMessage.tenant_id == ts.tenant_id)
            .where(EngagementMessage.mailbox_connection_id == mailbox.id)
            .where(EngagementMessage.direction == "out")
            .where(EngagementMessage.status == "sent")
            .where(EngagementMessage.sent_at >= start))
    return int((await ts.session.execute(stmt)).scalar_one() or 0)


def volume_warning(mailbox_email: str, today: int, planned: int = 0) -> str:
    """The sentence the launch, approval and mailbox screens show, or ``""`` below the threshold."""
    total = max(0, int(today)) + max(0, int(planned))
    if total <= VOLUME_WARNING_THRESHOLD:
        return ""
    return (f"This will send about {total} emails from {mailbox_email} today. Above "
            f"{VOLUME_WARNING_THRESHOLD} a day raises the chance of being marked as spam.")
```

In `nexus/api/routers/engagement_mailboxes.py`, add to `MailboxOut`:

```python
    # Sent since the owner's local midnight, and the warning above 50 (D10). Never a block.
    sent_today: int = 0
    volume_warning: str = ""
```

and in `_out`, import `sent_today, volume_warning` from `nexus.engagement.sending.limits`, compute `today = await sent_today(ts, row)` after `settings`, and pass `sent_today=today, volume_warning=volume_warning(row.email, today)`.

In `frontend/src/lib/types.ts`, add to `ConnectedMailbox`:

```ts
  /** Sent since the owner's local midnight. */
  sent_today: number;
  /** Non-empty above 50 a day (D10). A warning, never a block. */
  volume_warning: string;
```

In `frontend/src/pages/engagement/MailboxesPage.tsx`, under the `last_error` line of the mailbox card:

```tsx
        {mailbox.volume_warning && (
          <p className={styles.error} role="status">
            <Badge tone="warning" dot>{mailbox.sent_today} sent today</Badge>{" "}
            {mailbox.volume_warning}
          </p>
        )}
```

- [ ] **Step 4: Run** `pytest tests/test_engagement_sending.py tests/test_engagement_mailboxes.py -n0 -q` then `cd frontend && npm run typecheck` — expected pass, clean.
- [ ] **Step 5: Commit** — `git commit -am "feat(engagement): today's count in the owner's timezone and the over-50 warning"`

---

### Task 4: Exactly once

**Files:** Create `nexus/engagement/sending/service.py`; Modify `nexus/engagement/mailboxes/registry.py`

- [ ] **Step 1: Write the failing tests** — add the eight exactly-once tests: sent once and not resent, timeout after delivery reconciled, timeout before delivery retried with the same ids, provider limit pauses the mailbox, suppressed address stopped before any row, expired grant held, follow-up answers the latest message, and the ledger event.
- [ ] **Step 2: Run** — expected FAIL: `cannot import name 'set_provider_factory'`.
- [ ] **Step 3: Implement.** `nexus/engagement/mailboxes/registry.py`, replacing the file:

```python
"""The one place a connected mailbox becomes a provider object."""
from __future__ import annotations

from collections.abc import Callable

from nexus.engagement.mailboxes.provider import MailProvider

#: The test seam, in the mould of `set_crm_connector` and `set_task_queue`: when installed it builds
#: the provider for every connection and token refresh is skipped. Production never sets it, and
#: what the real Gmail and Graph providers do is proved against the real services in
#: `tests_live/engagement/`, not here.
_factory: Callable[[object], MailProvider] | None = None


def set_provider_factory(factory: Callable[[object], MailProvider] | None) -> None:
    global _factory
    _factory = factory


def make_provider(provider: str, *, access_token: str, email: str = "") -> MailProvider:
    if provider == "google":
        from nexus.engagement.mailboxes.gmail import GmailProvider

        return GmailProvider(access_token=access_token, email=email)
    if provider == "microsoft":
        from nexus.engagement.mailboxes.graph import GraphProvider

        return GraphProvider(access_token=access_token, email=email)
    raise ValueError(f"unknown mailbox provider {provider!r}")


def provider_for(connection, access_token: str) -> MailProvider:
    if _factory is not None:
        return _factory(connection)
    return make_provider(connection.provider, access_token=access_token, email=connection.email)


async def open_provider(ts, connection) -> MailProvider:
    """A provider with a fresh access token. Raises ``AuthExpired`` (and marks the connection
    ``needs_reauth``) when the grant is gone."""
    if _factory is not None:
        return _factory(connection)
    from nexus.engagement.mailboxes.tokens import fresh_access_token

    return provider_for(connection, await fresh_access_token(ts, connection))
```

`nexus/engagement/sending/service.py`:

```python
"""Sending one email from an SDR's own mailbox, exactly once (spec §5).

The shape of this module is set by one requirement: **a send that we are not sure about must never
become a second email to a real buyer.** So:

1. The outbound row is written **before** the provider call, carrying the `Message-ID` and the
   `X-Nexus-Ref` the message will have. The partial unique index on
   ``(enrollment_id, step_index) WHERE direction = 'out'`` means a concurrent worker writing the
   same step loses to a constraint rather than to a race.
2. A retry **reuses that row**, with the same ids, and asks the provider's Sent folder for the ref
   before sending anything (``find_sent``). A timeout is therefore recoverable without a duplicate.
3. A provider quota or rate limit pauses the MAILBOX until its reset time and leaves the row queued.
   The step stays due; nothing is lost and nothing is sent twice.

The pre-send checks in `checks.py` run here, immediately before the provider call, because the
person can unsubscribe or reply between the scheduling and the send.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

logger = logging.getLogger("nexus.engagement.sending")

#: A provider that rate-limits without saying for how long. Long enough to matter, short enough that
#: a mailbox is not out of action for an afternoon.
DEFAULT_PAUSE_MINUTES = 30


@dataclass(slots=True)
class SendResult:
    outcome: str                 # sent | held | stopped | failed
    message_id: str = ""         # our row id
    provider_message_id: str = ""
    thread_id: str = ""
    reason: str = ""
    reconciled: bool = False     # the provider had it already; we did not send again

    @property
    def sent(self) -> bool:
        return self.outcome == "sent"


async def send(
    ts,
    *,
    mailbox,
    contact,
    subject: str,
    body: str,
    enrollment=None,
    thread=None,
    step_index: int | None = None,
    kind: str = "step",
    ai_subject: str | None = None,
    ai_body: str | None = None,
    quality_problems: list[str] | None = None,
    idempotency_key: str | None = None,
    user_id: str | None = None,
    context: dict | None = None,
    inbound_since: datetime | None = None,
) -> SendResult:
    """Send one message, or say why it was held or stopped. Never sends the same message twice."""
    from nexus.billing.entitlements import preflight
    from nexus.billing.meter import metered
    from nexus.core.db import utcnow
    from nexus.engagement.mailboxes.provider import (
        AuthExpired,
        NotFound,
        ProviderError,
        ProviderLimit,
        ThreadRef,
        TransientError,
    )
    from nexus.engagement.mailboxes.registry import open_provider
    from nexus.engagement.sending import checks, mime
    from nexus.engagement.suppression.service import active_block
    from nexus.engagement.suppression.tokens import unsubscribe_url

    now = utcnow()
    to_addr = (getattr(contact, "email", "") or "").strip()
    if not to_addr:
        return SendResult("stopped", reason="the contact has no email address")

    block = await active_block(ts, to_addr)
    decision = checks.decide(
        suppressed_reason=getattr(block, "reason", "") if block else "",
        inbound_since_scheduled=bool(inbound_since),
        enrollment_status=getattr(enrollment, "status", "active"),
        mailbox_status=mailbox.status,
        mailbox_paused_until=mailbox.paused_until,
        quality_problems=quality_problems,
        is_first_touch=(step_index == 0),
        credits_ok=(await preflight(ts, "outreach.email_send")).allowed,
        now=now,
    )
    if not decision.ok:
        return SendResult("stopped" if decision.outcome == checks.STOP else "held",
                          reason=decision.reason)

    row, existing = await _claim_row(
        ts, mailbox=mailbox, contact=contact, enrollment=enrollment, thread=thread,
        step_index=step_index, kind=kind, subject=subject, body=body, to_addr=to_addr,
        ai_subject=ai_subject, ai_body=ai_body, quality_problems=quality_problems,
        idempotency_key=idempotency_key,
    )
    if row.status == "sent":
        return SendResult("sent", message_id=row.id,
                          provider_message_id=row.provider_message_id or "",
                          thread_id=row.thread_id or "", reconciled=True)
    if row.status == "failed":
        return SendResult("failed", message_id=row.id, reason=row.error or "")

    provider = await open_provider(ts, mailbox)

    if existing:
        # This row has been through a provider call before. Ask the Sent folder first: a timeout is
        # the one failure where "try again" and "already delivered" look identical from here.
        found = await _find_already_sent(provider, row, to_addr, now)
        if found is not None:
            await _mark_sent(ts, row, found, now)
            return SendResult("sent", message_id=row.id,
                              provider_message_id=found.provider_message_id,
                              thread_id=row.thread_id or "", reconciled=True)

    thread_ref = None
    if thread is not None:
        parent = await _latest_in_thread(ts, thread, exclude_id=row.id)
        thread_ref = ThreadRef(
            provider_thread_id=thread.provider_thread_id,
            reply_to_provider_message_id=getattr(parent, "provider_message_id", "") or "",
            in_reply_to=row.in_reply_to or "",
            references=row.references_header or "",
        )

    message = mime.build_message(
        from_addr=mailbox.email, from_name=mailbox.display_name, to_addr=to_addr,
        subject=subject, body=body, message_id=row.rfc_message_id, ref=row.ref_header,
        unsubscribe_url=unsubscribe_url(ts.tenant_id, contact.id),
        unsubscribe_mailto=mailbox.email,
        signature=await _signature(ts, mailbox), in_reply_to=row.in_reply_to or "",
        references=row.references_header or "",
    )

    try:
        async with metered(ts, "outreach.email_send", user_id=user_id, source="engagement",
                           idempotency_key=f"outreach.email_send:{row.id}"):
            sent = await provider.send(mime.to_bytes(message), thread=thread_ref)
    except ProviderLimit as exc:
        await _pause_mailbox(ts, mailbox, exc.retry_at, now)
        row.error = f"provider limit: {exc}"[:500]
        await ts.flush()
        return SendResult("held", message_id=row.id,
                          reason="the mailbox hit its provider sending limit and is paused")
    except AuthExpired:
        mailbox.status = "needs_reauth"
        row.error = "the mailbox grant expired"
        await ts.flush()
        return SendResult("held", message_id=row.id, reason="the mailbox needs reconnecting")
    except (TransientError, TimeoutError) as exc:
        found = await _find_already_sent(provider, row, to_addr, now)
        if found is not None:
            await _mark_sent(ts, row, found, now)
            await _record(ts, row, mailbox=mailbox, contact=contact, enrollment=enrollment,
                          user_id=user_id, context=context or {}, now=now)
            return SendResult("sent", message_id=row.id,
                              provider_message_id=found.provider_message_id,
                              thread_id=row.thread_id or "", reconciled=True)
        row.error = f"{type(exc).__name__}: {exc}"[:500]
        await ts.flush()
        return SendResult("held", message_id=row.id,
                          reason="the provider did not answer; the send will be retried")
    except (NotFound, ProviderError) as exc:
        row.status = "failed"
        row.error = f"{type(exc).__name__}: {exc}"[:500]
        await ts.flush()
        return SendResult("failed", message_id=row.id, reason=str(exc)[:200])

    await _mark_sent(ts, row, sent, now)
    await _record(ts, row, mailbox=mailbox, contact=contact, enrollment=enrollment,
                  user_id=user_id, context=context or {}, now=now)
    return SendResult("sent", message_id=row.id,
                      provider_message_id=row.provider_message_id or "",
                      thread_id=row.thread_id or "")


async def _claim_row(ts, *, mailbox, contact, enrollment, thread, step_index, kind, subject, body,
                     to_addr, ai_subject, ai_body, quality_problems, idempotency_key):
    """The outbound row, created or re-found. Returns ``(row, existed_before)``.

    The unique index is the arbiter, not a read-then-write: two workers claiming the same step at
    the same moment is exactly the case a SELECT-then-INSERT would send twice."""
    from sqlalchemy.exc import IntegrityError

    from nexus.engagement.ids import new_ulid
    from nexus.engagement.sending import mime
    from nexus.models.engagement import EngagementMessage

    existing = await _existing(ts, enrollment, step_index, idempotency_key)
    if existing is not None:
        return existing, True

    parent = await _latest_in_thread(ts, thread)
    row = EngagementMessage(
        mailbox_connection_id=mailbox.id,
        thread_id=getattr(thread, "id", None),
        enrollment_id=getattr(enrollment, "id", None),
        contact_id=contact.id,
        direction="out",
        kind=kind,
        status="queued",
        step_index=step_index,
        idempotency_key=idempotency_key,
        rfc_message_id=mime.new_rfc_message_id(_domain(mailbox.email)),
        ref_header=new_ulid(),
        in_reply_to=getattr(parent, "rfc_message_id", "") or "",
        references_header=mime.references_for(
            getattr(parent, "references_header", "") or "",
            getattr(parent, "rfc_message_id", "") or ""),
        from_addr=mailbox.email,
        to_addrs=[to_addr],
        subject=subject,
        body_text=body,
        ai_subject=ai_subject,
        ai_body=ai_body,
        quality_problems=list(quality_problems or []),
    )
    try:
        async with ts.session.begin_nested():
            ts.add(row)
            await ts.flush()
    except IntegrityError:
        # Another worker claimed this step between our read and our write. Theirs is the row.
        other = await _existing(ts, enrollment, step_index, idempotency_key)
        if other is None:
            raise
        return other, True
    return row, False


async def _existing(ts, enrollment, step_index, idempotency_key):
    from nexus.models.engagement import EngagementMessage

    if enrollment is not None and step_index is not None:
        stmt = (ts.select(EngagementMessage)
                .where(EngagementMessage.enrollment_id == enrollment.id)
                .where(EngagementMessage.step_index == step_index)
                .where(EngagementMessage.direction == "out"))
        found = (await ts.session.scalars(stmt)).first()
        if found is not None:
            return found
    if idempotency_key:
        stmt = (ts.select(EngagementMessage)
                .where(EngagementMessage.idempotency_key == idempotency_key))
        return (await ts.session.scalars(stmt)).first()
    return None


async def _latest_in_thread(ts, thread, *, exclude_id: str | None = None):
    """The message a reply should answer: the newest in the thread, inbound or outbound (D16)."""
    from nexus.models.engagement import EngagementMessage

    if thread is None:
        return None
    stmt = (ts.select(EngagementMessage)
            .where(EngagementMessage.thread_id == thread.id)
            .where(EngagementMessage.status != "queued")
            .order_by(EngagementMessage.created_at.desc()))
    if exclude_id:
        stmt = stmt.where(EngagementMessage.id != exclude_id)
    return (await ts.session.scalars(stmt.limit(1))).first()


async def _find_already_sent(provider, row, to_addr: str, now: datetime):
    """Ask the Sent folder whether this exact message is already there. Never raises."""
    try:
        return await provider.find_sent(ref_header=row.ref_header, to=to_addr,
                                        around=row.created_at or now)
    except Exception:
        logger.warning("could not reconcile message %s against the Sent folder", row.id,
                       exc_info=True)
        return None


async def _mark_sent(ts, row, sent, now: datetime) -> None:
    row.status = "sent"
    row.sent_at = now
    row.error = None
    row.provider_message_id = sent.provider_message_id or row.provider_message_id
    if sent.provider_thread_id:
        thread = await thread_for(ts, row, sent.provider_thread_id, now)
        row.thread_id = thread.id
    await ts.flush()


async def thread_for(ts, row, provider_thread_id: str, now: datetime):
    """The thread row for this provider conversation, created on first sight.

    Unique on ``(mailbox_connection_id, provider_thread_id)``, so two messages landing in one
    conversation converge on one row instead of splitting the timeline."""
    from sqlalchemy.exc import IntegrityError

    from nexus.engagement.subjects import normalize_subject
    from nexus.models.engagement import EngagementThread

    stmt = (ts.select(EngagementThread)
            .where(EngagementThread.mailbox_connection_id == row.mailbox_connection_id)
            .where(EngagementThread.provider_thread_id == provider_thread_id))
    thread = (await ts.session.scalars(stmt)).first()
    if thread is not None:
        thread.last_message_at = now
        await ts.flush()
        return thread
    thread = EngagementThread(
        mailbox_connection_id=row.mailbox_connection_id,
        provider_thread_id=provider_thread_id,
        contact_id=row.contact_id,
        enrollment_id=row.enrollment_id,
        base_subject=normalize_subject(row.subject),
        last_message_at=now,
    )
    try:
        async with ts.session.begin_nested():
            ts.add(thread)
            await ts.flush()
    except IntegrityError:
        thread = (await ts.session.scalars(stmt)).first()
        if thread is None:
            raise
    return thread


async def _pause_mailbox(ts, mailbox, retry_at: datetime | None, now: datetime) -> None:
    """A provider limit pauses the MAILBOX, not the campaign: the limit is per sending account, and
    the steps stay due so they resume the moment it lifts (D10)."""
    mailbox.paused_until = retry_at or (now + timedelta(minutes=DEFAULT_PAUSE_MINUTES))
    mailbox.last_error = "paused until the provider sending limit resets"
    await ts.flush()


async def _signature(ts, mailbox) -> str:
    """The mailbox's own sign-off, else the workspace default (`outreach/signature.py`).

    One primary-key read per send. The alternative — passing it in — would make every caller
    responsible for a rule that belongs to the mailbox, and the rule already exists."""
    from nexus.models.identity import Tenant
    from nexus.outreach.signature import resolve_signature

    tenant = await ts.session.get(Tenant, ts.tenant_id)
    return resolve_signature(getattr(tenant, "email_settings", None),
                             {"signature": mailbox.signature})


async def _record(ts, row, *, mailbox, contact, enrollment, user_id, context: dict,
                  now: datetime) -> None:
    """The ledger event for a message that left the building (spec §18.4 `message.sent`)."""
    from nexus.engagement.ledger.emit import emit
    from nexus.engagement.timekeeping import to_local, zone_or_none

    zone_name = getattr(enrollment, "contact_timezone", "") or mailbox.timezone or "UTC"
    local = to_local(now, zone_or_none(zone_name) or UTC)
    payload = {
        "kind": row.kind,
        "step_index": row.step_index,
        "subject": row.subject,
        "body": row.body_text,
        "sent_local_hour": local.hour,
        "sent_local_weekday": local.weekday(),
        "contact_timezone": zone_name,
        "mailbox_provider": mailbox.provider,
    }
    payload.update(context or {})
    await emit(ts, "message.sent", actor_user_id=user_id,
               refs={"contact_id": contact.id, "account_id": getattr(contact, "account_id", None),
                     "enrollment_id": getattr(enrollment, "id", None),
                     "campaign_id": getattr(enrollment, "campaign_id", None),
                     "thread_id": row.thread_id, "message_id": row.id, "mailbox_id": mailbox.id},
               payload=payload)


def _domain(email: str) -> str:
    return email.split("@", 1)[1] if "@" in (email or "") else ""
```

Four decisions in that file that are easy to get wrong:

- **The row is claimed through the unique index, not a read-then-write.** Two workers on one step both pass a SELECT; only one INSERT survives the index, and the loser adopts the winner's row.
- **A retry reuses the row's `Message-ID` and `X-Nexus-Ref`.** New ids on a retry would make the Sent-folder search useless and give the buyer two messages that each look original.
- **A provider limit pauses the mailbox, and the row stays `queued`.** The step is still due; the next attempt is held by the checks until `paused_until` passes, without asking the provider.
- **A permanent refusal (`NotFound`, other `ProviderError`) marks the row `failed`**, which is terminal for that step: retrying an invalid recipient is a bounce every tick.

- [ ] **Step 4: Run** `pytest tests/test_engagement_sending.py -n0 -q` — expected all pass.
- [ ] **Step 5: Commit** — `git add nexus/engagement && git commit -m "feat(engagement): send exactly once — claim, reconcile by X-Nexus-Ref, pause on provider limits"`

---

### Task 5: The whole test file

```python
"""Sending: the MIME we hand to Gmail and Graph, the pre-send checks, and exactly-once (spec §5).

The provider here is `SentFolder`, a test double installed through
`registry.set_provider_factory` — the same kind of seam as `set_crm_connector`. It keeps a real Sent
folder in memory, so the reconciliation tests exercise the thing that matters: after a timeout, is
the message found by its `X-Nexus-Ref`, and is it never delivered twice? What Gmail and Graph
actually do with the bytes is proved against the real services in `tests_live/engagement/`.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email import message_from_bytes as _parse
from email.policy import default as _modern

import pytest

from tests.conftest import make_tenant, tenant_session

NOW = datetime(2026, 9, 22, 10, 0, tzinfo=UTC)


def message_from_bytes(raw: bytes):
    """Parsed with the modern policy, which is what gives a message `get_content()`."""
    return _parse(raw, policy=_modern)


class SentFolder:
    """A mailbox that records every delivery and can be told to fail the next call."""

    provider = "google"

    def __init__(self):
        self.delivered: list[bytes] = []
        self.calls = 0
        self.fail_next: Exception | None = None
        self.deliver_then_fail = False

    async def send(self, mime: bytes, *, thread=None):
        from nexus.engagement.mailboxes.provider import SentRef

        self.calls += 1
        if self.fail_next is not None:
            error, self.fail_next = self.fail_next, None
            if self.deliver_then_fail:
                # The provider accepted the message and the answer never reached us.
                self.delivered.append(mime)
            raise error
        self.delivered.append(mime)
        return SentRef(provider_message_id=f"pm-{len(self.delivered)}",
                       provider_thread_id=getattr(thread, "provider_thread_id", "") or "thread-1")

    async def find_sent(self, *, ref_header: str, to: str, around):
        from nexus.engagement.mailboxes.provider import SentRef

        for index, raw in enumerate(self.delivered, 1):
            if message_from_bytes(raw)["X-Nexus-Ref"] == ref_header:
                return SentRef(provider_message_id=f"pm-{index}", provider_thread_id="thread-1")
        return None


@pytest.fixture
def folder():
    from nexus.engagement.mailboxes import registry

    box = SentFolder()
    registry.set_provider_factory(lambda _connection: box)
    yield box
    registry.set_provider_factory(None)


async def _world(slug: str):
    """A workspace with an SDR, a connected mailbox, an account and a contact."""
    from nexus.models.account import Account, Contact
    from nexus.models.engagement import MailboxConnection
    from nexus.models.identity import User

    tid = await make_tenant(slug=slug, name=slug.title())
    async with tenant_session(tid) as ts:
        user = User(email=f"sam@{slug}.com", full_name="Sam Rep", password_hash="x")
        ts.session.add(user)
        await ts.session.flush()
        mailbox = MailboxConnection(owner_user_id=user.id, provider="google",
                                    email=f"sam@{slug}.com", display_name="Sam Rep",
                                    status="connected", timezone="Europe/London",
                                    signature="Sam\nSDR, Seller Co")
        account = Account(name="Acme Robotics", domain="acme.io")
        ts.add(mailbox)
        ts.add(account)
        await ts.flush()
        contact = Contact(account_id=account.id, full_name="Jane Buyer", email="jane@acme.io")
        ts.add(contact)
        await ts.flush()
        return tid, mailbox.id, contact.id, user.id


async def _load(ts, mailbox_id, contact_id):
    from nexus.models.account import Contact
    from nexus.models.engagement import MailboxConnection

    return await ts.get(MailboxConnection, mailbox_id), await ts.get(Contact, contact_id)


# ---- the message ----------------------------------------------------------------------------------

def test_the_message_carries_threading_unsubscribe_and_reconciliation_headers():
    from nexus.engagement.sending import mime

    message = mime.build_message(
        from_addr="sam@seller.com", from_name="Sam Rep", to_addr="jane@acme.io",
        subject="Re: Quick question", body="Hi Jane,\n\nFollowing up.", message_id="<m2@seller>",
        ref="01JREF", unsubscribe_url="https://app.example.com/api/u/tok",
        unsubscribe_mailto="sam@seller.com", signature="Sam", in_reply_to="<m1@seller>",
        references="<m1@seller>")
    parsed = message_from_bytes(mime.to_bytes(message))
    assert parsed["Message-ID"] == "<m2@seller>"
    assert parsed["In-Reply-To"] == "<m1@seller>" and parsed["References"] == "<m1@seller>"
    assert parsed["List-Unsubscribe"] == ("<https://app.example.com/api/u/tok>, "
                                          "<mailto:sam@seller.com>")
    assert parsed["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert parsed["X-Nexus-Ref"] == "01JREF"
    assert parsed.get_content_type() == "text/plain"


def test_the_body_ends_with_the_signature_then_one_opt_out_line():
    from nexus.engagement.sending import mime

    url = "https://app.example.com/api/u/tok"
    text = mime.compose_body("Hi Jane,\n\nWorth a chat?", "Sam", url)
    assert text.index("Worth a chat?") < text.index("Sam") < text.index(url)
    assert text.count('Just reply "no"') == 1
    # Shown in the composer with its footer, sent again: still one footer and one signature.
    assert mime.compose_body(text, "Sam", url) == text


def test_references_keep_the_thread_root_and_stay_bounded():
    from nexus.engagement.sending import mime

    chain = " ".join(f"<m{i}@x>" for i in range(30))
    refs = mime.references_for(chain, "<m30@x>").split()
    assert refs[0] == "<m0@x>" and refs[-1] == "<m30@x>"
    assert len(refs) == mime.MAX_REFERENCES
    assert mime.references_for("", "<first@x>") == "<first@x>"


# ---- the checks -----------------------------------------------------------------------------------

def test_stops_come_before_holds_and_a_first_touch_is_not_held_for_quality():
    from nexus.engagement.sending import checks

    stopped = checks.decide(suppressed_reason="unsubscribed", mailbox_status="needs_reauth")
    assert stopped.outcome == checks.STOP and "do-not-contact" in stopped.reason
    assert checks.decide(inbound_since_scheduled=True).outcome == checks.STOP
    assert checks.decide(enrollment_status="completed").outcome == checks.STOP
    assert checks.decide(paused_by_colleague=True).outcome == checks.HOLD
    assert checks.decide(mailbox_status="needs_reauth").outcome == checks.HOLD
    assert checks.decide(mailbox_paused_until=NOW + timedelta(minutes=5), now=NOW).outcome \
        == checks.HOLD
    assert checks.decide(mailbox_paused_until=NOW - timedelta(minutes=5), now=NOW).ok
    # A follow-up nobody is watching waits for review; a reviewed first email does not (D17).
    assert checks.decide(quality_problems=["no greeting"]).outcome == checks.HOLD
    assert checks.decide(quality_problems=["no greeting"], is_first_touch=True).ok
    assert checks.decide(credits_ok=False).outcome == checks.HOLD


# ---- volume ---------------------------------------------------------------------------------------

def test_the_volume_warning_appears_only_above_fifty_and_never_blocks():
    from nexus.engagement.sending.limits import volume_warning

    assert volume_warning("sam@x.com", 30, planned=20) == ""
    warning = volume_warning("sam@x.com", 40, planned=20)
    assert "about 60 emails from sam@x.com today" in warning and "spam" in warning


async def test_today_is_the_mailbox_owners_day_not_the_utc_day():
    from nexus.engagement.sending.limits import sent_today
    from nexus.models.engagement import EngagementMessage

    tid, mailbox_id, contact_id, _user = await _world("vol")
    async with tenant_session(tid) as ts:
        mailbox, contact = await _load(ts, mailbox_id, contact_id)
        mailbox.timezone = "America/Los_Angeles"
        # 06:00 UTC on the 22nd is 23:00 on the 21st in Los Angeles: yesterday, locally.
        for sent_at in (datetime(2026, 9, 22, 6, 0, tzinfo=UTC),
                        datetime(2026, 9, 22, 8, 0, tzinfo=UTC),
                        datetime(2026, 9, 22, 18, 0, tzinfo=UTC)):
            ts.add(EngagementMessage(mailbox_connection_id=mailbox.id, contact_id=contact.id,
                                     direction="out", status="sent", sent_at=sent_at))
        await ts.flush()
        assert await sent_today(ts, mailbox, now=datetime(2026, 9, 22, 19, 0, tzinfo=UTC)) == 2


# ---- exactly once ---------------------------------------------------------------------------------

async def test_a_message_is_sent_once_threaded_and_asking_again_does_not_resend(folder):
    from nexus.engagement.sending.service import send
    from nexus.models.engagement import EngagementMessage, EngagementThread

    tid, mailbox_id, contact_id, user_id = await _world("once")
    async with tenant_session(tid) as ts:
        mailbox, contact = await _load(ts, mailbox_id, contact_id)
        result = await send(ts, mailbox=mailbox, contact=contact, subject="Quick question",
                            body="Hi Jane,\n\nWorth a chat?", idempotency_key="k-1",
                            user_id=user_id)
        assert result.sent and not result.reconciled
        row = await ts.get(EngagementMessage, result.message_id)
        assert row.status == "sent" and row.provider_message_id == "pm-1"
        thread = await ts.get(EngagementThread, row.thread_id)
        assert thread.provider_thread_id == "thread-1" and thread.base_subject == "Quick question"
        delivered = message_from_bytes(folder.delivered[0])
        assert delivered["X-Nexus-Ref"] == row.ref_header
        assert delivered["Message-ID"] == row.rfc_message_id
        assert "Sam\nSDR, Seller Co" in delivered.get_content()

        again = await send(ts, mailbox=mailbox, contact=contact, subject="Quick question",
                           body="Hi Jane,\n\nWorth a chat?", idempotency_key="k-1")
    assert again.sent and again.reconciled and again.message_id == result.message_id
    assert folder.calls == 1 and len(folder.delivered) == 1


async def test_a_timeout_after_delivery_is_reconciled_from_the_sent_folder(folder):
    from nexus.engagement.mailboxes.provider import TransientError
    from nexus.engagement.sending.service import send

    tid, mailbox_id, contact_id, _user = await _world("recon")
    folder.fail_next = TransientError("read timed out")
    folder.deliver_then_fail = True
    async with tenant_session(tid) as ts:
        mailbox, contact = await _load(ts, mailbox_id, contact_id)
        result = await send(ts, mailbox=mailbox, contact=contact, subject="Hi", body="Hi Jane",
                            idempotency_key="k-2")
    assert result.sent and result.reconciled
    assert len(folder.delivered) == 1, "a timed-out send must never become a second email"


async def test_a_timeout_before_delivery_is_retried_with_the_same_ids(folder):
    from nexus.engagement.mailboxes.provider import TransientError
    from nexus.engagement.sending.service import send
    from nexus.models.engagement import EngagementMessage

    tid, mailbox_id, contact_id, _user = await _world("retry")
    folder.fail_next = TransientError("connection reset")
    async with tenant_session(tid) as ts:
        mailbox, contact = await _load(ts, mailbox_id, contact_id)
        first = await send(ts, mailbox=mailbox, contact=contact, subject="Hi", body="Hi Jane",
                           idempotency_key="k-3")
        assert first.outcome == "held" and not folder.delivered
        row = await ts.get(EngagementMessage, first.message_id)
        ref, message_id = row.ref_header, row.rfc_message_id

        second = await send(ts, mailbox=mailbox, contact=contact, subject="Hi", body="Hi Jane",
                            idempotency_key="k-3")
    assert second.sent and second.message_id == first.message_id
    assert len(folder.delivered) == 1
    delivered = message_from_bytes(folder.delivered[0])
    assert delivered["X-Nexus-Ref"] == ref and delivered["Message-ID"] == message_id


async def test_a_provider_limit_pauses_the_mailbox_and_the_step_stays_due(folder):
    from nexus.engagement.mailboxes.provider import ProviderLimit
    from nexus.engagement.sending.service import send
    from nexus.models.engagement import EngagementMessage

    tid, mailbox_id, contact_id, _user = await _world("limit")
    reset = datetime(2030, 1, 1, tzinfo=UTC)
    folder.fail_next = ProviderLimit("quota", status=429, retry_at=reset)
    async with tenant_session(tid) as ts:
        mailbox, contact = await _load(ts, mailbox_id, contact_id)
        result = await send(ts, mailbox=mailbox, contact=contact, subject="Hi", body="Hi Jane",
                            idempotency_key="k-4")
        assert result.outcome == "held" and "limit" in result.reason
        assert mailbox.paused_until == reset
        assert (await ts.get(EngagementMessage, result.message_id)).status == "queued"
        # While paused, the check holds it before the provider is even asked.
        again = await send(ts, mailbox=mailbox, contact=contact, subject="Hi", body="Hi Jane",
                           idempotency_key="k-4")
    assert again.outcome == "held" and "paused" in again.reason
    assert folder.calls == 1 and not folder.delivered


async def test_a_suppressed_address_is_stopped_before_anything_is_written(folder):
    from nexus.engagement.sending.service import send
    from nexus.engagement.suppression.service import suppress
    from nexus.models.engagement import EngagementMessage

    tid, mailbox_id, contact_id, _user = await _world("dnc")
    async with tenant_session(tid) as ts:
        mailbox, contact = await _load(ts, mailbox_id, contact_id)
        await suppress(ts, email="Jane@Acme.io", reason="unsubscribed", contact_id=contact.id)
        result = await send(ts, mailbox=mailbox, contact=contact, subject="Hi", body="Hi",
                            idempotency_key="k-5")
        assert result.outcome == "stopped" and "do-not-contact" in result.reason
        assert await ts.list(EngagementMessage) == []
    assert folder.calls == 0


async def test_an_expired_grant_holds_the_send_and_marks_the_mailbox(folder):
    from nexus.engagement.mailboxes.provider import AuthExpired
    from nexus.engagement.sending.service import send

    tid, mailbox_id, contact_id, _user = await _world("reauth")
    folder.fail_next = AuthExpired("invalid_grant", status=401)
    async with tenant_session(tid) as ts:
        mailbox, contact = await _load(ts, mailbox_id, contact_id)
        result = await send(ts, mailbox=mailbox, contact=contact, subject="Hi", body="Hi",
                            idempotency_key="k-6")
        assert result.outcome == "held" and mailbox.status == "needs_reauth"


async def test_a_follow_up_answers_the_latest_message_in_its_thread(folder):
    from nexus.engagement.sending.service import send
    from nexus.models.engagement import EngagementMessage, EngagementThread

    tid, mailbox_id, contact_id, _user = await _world("follow")
    async with tenant_session(tid) as ts:
        mailbox, contact = await _load(ts, mailbox_id, contact_id)
        first = await send(ts, mailbox=mailbox, contact=contact, subject="Quick question",
                           body="Hi Jane", idempotency_key="k-7")
        first_row = await ts.get(EngagementMessage, first.message_id)
        thread = await ts.get(EngagementThread, first_row.thread_id)
        follow = await send(ts, mailbox=mailbox, contact=contact, subject="Re: Quick question",
                            body="Following up", thread=thread, idempotency_key="k-8")
        follow_row = await ts.get(EngagementMessage, follow.message_id)
    assert follow_row.in_reply_to == first_row.rfc_message_id
    assert follow_row.references_header == first_row.rfc_message_id
    assert follow_row.thread_id == first_row.thread_id
    delivered = message_from_bytes(folder.delivered[1])
    assert delivered["In-Reply-To"] == first_row.rfc_message_id


async def test_a_sent_message_is_recorded_in_the_ledger_for_a_consenting_workspace(folder):
    from nexus.engagement.ledger import consent
    from nexus.engagement.sending.service import send
    from nexus.models.ledger import LedgerOutbox

    tid, mailbox_id, contact_id, _user = await _world("ledg")
    async with tenant_session(tid) as ts:
        await consent.record(ts, status_value="on", source="settings", user_id=None)
        mailbox, contact = await _load(ts, mailbox_id, contact_id)
        result = await send(ts, mailbox=mailbox, contact=contact, subject="Hi", body="Hi Jane",
                            step_index=None, idempotency_key="k-9",
                            context={"context_pack": "Account: Acme Robotics"})
        events = await ts.list(LedgerOutbox)
    sent = [e for e in events if e.event_type == "message.sent"]
    assert len(sent) == 1
    payload = sent[0].payload["payload"]
    assert payload["context_pack"] == "Account: Acme Robotics"
    assert payload["mailbox_provider"] == "google"
    assert sent[0].payload["refs"]["message_id"] == result.message_id
```

Run: `pytest tests/test_engagement_sending.py tests/test_engagement_mailboxes.py tests/test_engagement_suppression.py -n0 -q`, `ruff check nexus tests tests_live`, `cd frontend && npm run typecheck`.

RUN_RESULT:

```
42 passed in 95.26s   # sending + mailboxes + suppression + design tokens
All checks passed!    # ruff check nexus tests tests_live
tsc --noEmit          # clean
```

---

## What phase 08 depends on

- `send()` is the only way an engagement message leaves; phase 08's `advance_engagement` calls it per due step with `enrollment`, `step_index`, `thread` (the enrollment's `current_thread_id`), `ai_subject`/`ai_body`, `quality_problems` and `context` (the context pack and personalisation facts for the ledger).
- `SendResult.outcome` drives the enrollment: `sent` advances it, `held` leaves the step due, `stopped` stops the enrollment, `failed` marks the step failed and alerts the SDR.
- `limits.volume_warning` is what the launch and approval screens show.
