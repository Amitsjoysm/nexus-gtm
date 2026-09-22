# Phase 08: Sequences and Drafting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An SDR builds a campaign (contacts, steps, timing, sending mailbox), gets a personalised first email drafted for each contact, reviews and approves them, launches behind a credit gate, and the worker then sends each step when it is due — writing every follow-up just before it goes, from the whole conversation.

**Architecture:** Two packages. `nexus/engagement/drafting/` builds the context pack (§7: the date in the buyer's timezone, this conversation, every other conversation with the person, history, facts), checks personalisation (D17), and drafts through the existing `MessagingAgent` — one drafting path, not two. `nexus/engagement/sequences/` owns the state machine (`service.py`), the timing rules (`timing.py`, pure), the worst-case credit estimate and gate (`estimate.py`), and the heartbeat worker (`advance.py`) that claims due enrollments and calls phase 07's `send()`. The first email is the step-0 outbound row itself — `draft` → `approved` → adopted by `send()` — so the approved text is exactly the text that goes.

**Tech Stack:** async SQLAlchemy 2.0 (`FOR UPDATE SKIP LOCKED` on Postgres), `MessagingAgent` + `check_draft`, `metered()` / `_usage_amount`, FastAPI.

**Roadmap:** [00-roadmap.md](00-roadmap.md). **Spec:** §7, §8, §9, §10, D4, D5, D15, D16, D17, D18, D19, §19 (duplicate-outreach guard). **Depends on:** phase 07.

**Verified:** implemented on `feat/sdr-engagement` on top of phases 01–07 and run in the CI image: `tests/test_engagement_sequences.py` (18 passed — including a three-step sequence sent end to end through the provider double, threaded, with exactly one `Re:`), plus the drafting, quality, sending, scheduler, metering-coverage and route-guard suites it touches (151 passed), and `ruff`.

---

## Decisions this phase makes

- **The two reply capabilities (`ai.reply_classify`, `ai.reply_draft`) are added in phases 09 and 10, where they are metered.** `tests/test_billing_metering_coverage.py` refuses a priced capability with no call site. Until then the estimate prices them at zero, and it picks up their rate cards automatically the day they exist.
- **The engine is dark by construction.** Every new route answers 404 and the worker job is neither enqueued nor effective while `engagement_campaigns_enabled` is off.
- **A follow-up that fails the quality or personalisation check is held for review, never sent** (D17); with `review_every_touch` every follow-up is.
- **A held step waits 15 minutes (or until the mailbox's provider pause lifts)** rather than being retried every tick.

---

## Files

| Action | Path | Responsibility |
|---|---|---|
| Create | `nexus/engagement/drafting/__init__.py`, `context.py`, `personalisation.py`, `drafter.py` | context pack, D17 check, one draft |
| Create | `nexus/engagement/sequences/__init__.py`, `timing.py`, `estimate.py`, `service.py`, `advance.py` | timing, estimate + gate, state machine, worker |
| Create | `nexus/api/routers/engagement_campaigns.py`, `engagement_templates.py` | the API |
| Modify | `nexus/agents/email_quality.py`, `nexus/agents/messaging.py` | the personalisation rule and the context pack |
| Modify | `nexus/engagement/sending/service.py` | adopt the approved draft row |
| Modify | `nexus/workers/tasks.py`, `nexus/workers/scheduler.py`, `nexus/api/routers/__init__.py` | `advance_engagement`, routers |
| Create | `tests/test_engagement_sequences.py` | rules, drafting, gate, worker, API |

---

### Task 1: Timing and the personalisation rule (pure)

- [ ] **Step 1: Write the failing tests** — create `tests/test_engagement_sequences.py` with the header, fixtures, `_world`, `STEPS` and the three pure tests from the final file (Task 7).
- [ ] **Step 2: Run** `pytest tests/test_engagement_sequences.py -n0 -q` — expected FAIL: `No module named 'nexus.engagement.sequences'`.
- [ ] **Step 3: Implement.** `nexus/engagement/drafting/__init__.py`:

```python
"""Drafting: the context pack, the personalisation rule, and one draft (spec §7)."""
```

`nexus/engagement/sequences/__init__.py`:

```python
"""Campaigns, steps, enrollments, timing and the worker that sends due steps (spec §8)."""
```

`nexus/engagement/sequences/timing.py`:

```python
"""When each step of an enrollment is due (spec §8, D19). Pure.

* **First emails** are due when approved (`on_approval`) or at the campaign's `first_send_at`
  (`scheduled`), whichever the campaign chose — and never before the approval, because a scheduled
  time does not approve a draft.
* **Follow-ups** are due `delay_business_days` after the previous outbound email: at the same local
  time of day (`auto`), or at a chosen time on an allowed weekday (`manual`). No send window is
  imposed (D10).
* **Whose clock:** the contact's timezone by default; the SDR's when the campaign says so.
"""
from __future__ import annotations

from datetime import UTC, datetime


def zone_name_for(enrollment, campaign, mailbox) -> str:
    if getattr(campaign, "timezone_mode", "contact") == "sdr":
        return getattr(mailbox, "timezone", "") or "UTC"
    return getattr(enrollment, "contact_timezone", "") or getattr(mailbox, "timezone", "") or "UTC"


def first_due_at(*, first_send_mode: str, first_send_at: datetime | None,
                 approved_at: datetime | None, now: datetime) -> datetime:
    ready = approved_at or now
    if first_send_mode == "scheduled" and first_send_at is not None:
        return max(first_send_at, ready)
    return ready


def followup_due_at(*, step, previous_sent_at: datetime, zone_name: str) -> datetime:
    from nexus.engagement.timekeeping import (
        auto_followup_at,
        manual_followup_at,
        parse_clock,
        zone_or_none,
    )

    zone = zone_or_none(zone_name) or UTC
    delay = max(0, int(getattr(step, "delay_business_days", 0) or 0))
    clock = parse_clock(getattr(step, "send_time_local", None))
    if getattr(step, "timing_mode", "auto") == "manual" and clock is not None:
        return manual_followup_at(previous_sent_at=previous_sent_at, delay_business_days=delay,
                                  send_time_local=clock,
                                  allowed_weekdays=getattr(step, "allowed_weekdays", None) or [],
                                  zone=zone)
    return auto_followup_at(previous_sent_at=previous_sent_at, delay_business_days=delay, zone=zone)


def after_snooze_due_at(*, step, resumed_at: datetime, zone_name: str) -> datetime:
    """After a re-engagement, the remaining steps continue from it, each after its own delay."""
    return followup_due_at(step=step, previous_sent_at=resumed_at, zone_name=zone_name)
```

`nexus/engagement/drafting/personalisation.py`:

```python
"""Does a draft use at least one specific fact about the person or their company? (D17)

A rule in a prompt is a request; this is the check. "Hyper-personalised" is only enforceable if it
means something a reader could point at, so it means: **one of the facts in the context pack shows
up in the email.** A fact "shows up" when a number from it appears (a round size, a headcount, a
year), or when two of its distinctive words do — or half of them, for a short fact. Distinctive means four letters or more and not a
word every email contains, so "Hi", "your", "team" and the company's own name never count — naming
the company is not personalisation, it is a mail merge.

Pure, and deliberately literal: an LLM judge here would pass the drafts it wrote itself.
"""
from __future__ import annotations

import re

#: Words that appear in any sales email and so prove nothing about the reader.
_COMMON = frozenset("""
about after again also always because been before being between both could does doing during
each every from have having here hers into just like make many more most much must need only
other over same should since some such than that their them then there these they this those
through under until very want were what when where which while will with within would your
yours team teams company companies business work working help helps helping looking noticed
great quick chat call time week weeks month months year years thanks best regards hello thought
reach reaching out touch follow following share open worth minutes would love happy sounds
growth scale scaling platform solution solutions product products customers customer
""".split())

_WORD = re.compile(r"[a-z][a-z'\-]{3,}")
_NUMBER = re.compile(r"\d[\d,.]*\d|\d")


def _distinctive(text: str, stop: frozenset[str]) -> set[str]:
    return {w for w in _WORD.findall((text or "").lower()) if w not in _COMMON and w not in stop}


def _numbers(text: str) -> set[str]:
    # A lone digit ("1 thing") proves nothing; a figure with at least two digits does.
    return {n.replace(",", "") for n in _NUMBER.findall(text or "") if len(n.replace(",", "")) >= 2}


def fact_used(body: str, fact: str, *, company_words: frozenset[str] = frozenset()) -> bool:
    body_words = _distinctive(body, company_words)
    fact_words = _distinctive(fact, company_words)
    if _numbers(fact) & _numbers(body):
        return True
    matched = len(fact_words & body_words)
    # Two distinctive words, or half of a short fact's: "Series B" is one word of "raises Series B"
    # and is as specific as an email gets; one word of an eight-word headline is a coincidence.
    return matched >= 2 or (matched >= 1 and matched * 2 >= len(fact_words))


def uses_a_fact(body: str, facts: list[str], *, company_name: str = "") -> bool:
    """True when at least one fact is visibly used. No facts at all is a pass: there is nothing the
    draft could have used, and holding every email for want of research is a different problem."""
    usable = [f for f in (facts or []) if (f or "").strip()]
    if not usable:
        return True
    company_words = frozenset(w.lower() for w in re.findall(r"[A-Za-z']{4,}", company_name or ""))
    return any(fact_used(body, fact, company_words=company_words) for fact in usable)


PROBLEM = ("The email uses no specific fact about the person or their company. Open on one of the "
           "facts given above — a signal, their role or something they posted — in your own words.")
```

In `nexus/agents/email_quality.py`, `check_draft` gains two keyword arguments and one rule at the end:

```python
def check_draft(*, subject, body, first_name, facts=None, company_name: str = "") -> list[str]:
    """Problems with this draft, in the order a reader would notice them. Empty means it is fine.

    Never raises: it is called on whatever the model returned, including None.

    ``facts`` switches on the personalisation rule (D17): the draft must visibly use one of them.
    Callers that pass none — the composer, the orchestrator — are checked exactly as before.
    """
```

```python
    if facts:
        from nexus.engagement.drafting.personalisation import PROBLEM, uses_a_fact

        if not uses_a_fact(text, list(facts), company_name=company_name):
            problems.append(PROBLEM)

    return problems
```

`validate_steps` lives in `service.py` (Task 3); the pure test for it passes after Task 3.

- [ ] **Step 4: Run** — the timing and personalisation tests pass.
- [ ] **Step 5: Commit** — `git commit -am "feat(engagement): timing rules and the personalisation check (D17)"`

---

### Task 2: The context pack and the drafter

- [ ] **Step 1: Implement** `nexus/engagement/drafting/context.py`:

```python
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
from datetime import UTC, datetime

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
    parts.append("THE COMPANY\n" + account_facts(account) + "\n")
    rendered_signals = signal_facts(signals)
    if rendered_signals:
        parts.append("LIVE SIGNALS (strongest first)\n" + rendered_signals + "\n")

    instructions = {
        "first": "This is the FIRST email to this person.",
        "followup": ("This is a FOLLOW-UP in the same thread. Do not repeat the earlier email; add "
                     "one new, specific reason to reply. Never pretend they answered."),
        "reengage": ("They asked us to get back in touch around now. Reference that they asked, in "
                     "one short line, and make it easy to pick the conversation back up."),
        "response": "They replied. Answer what they actually said, then propose one next step.",
    }.get(kind, "")
    angle = (getattr(step, "angle", "") or "").strip()
    block = ["INSTRUCTIONS", f"- {instructions}"]
    if angle:
        block.append(f"- The angle for this step: {angle}")
    parts.append("\n".join(block) + "\n")
    return ContextPack(text="\n".join(parts).strip(), facts=facts, zone_name=zone_name)
```

`nexus/engagement/drafting/drafter.py`:

```python
"""Write one engagement email with the existing drafting pipeline (spec §7, D5, D17).

The model, the prompt rules, the quality check and its one regeneration are `MessagingAgent`'s — a
second drafting path would drift, and the first thing to drift would be the rules that keep invented
facts out of a buyer's inbox. What this adds is the context pack (the conversation, the date, the
history) and the personalisation rule, both passed in as agent inputs.

Each draft is charged as `ai.email_draft`, the capability the composer's drafts already use, so a
campaign's drafts and a one-off draft cost the same.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class Draft:
    subject: str = ""
    body: str = ""
    problems: list[str] = field(default_factory=list)
    context_pack: str = ""
    facts: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.body.strip())


async def draft(ts, *, enrollment, contact, account, mailbox, step=None, kind: str = "first",
                thread=None, user_id: str | None = None, now=None) -> Draft:
    """One draft. Never raises: a draft that cannot be written reports why."""
    from nexus.agents.email_quality import check_draft
    from nexus.agents.messaging import _first_name
    from nexus.agents.runtime import get_agent_runtime
    from nexus.billing.errors import QuotaExceeded
    from nexus.billing.meter import metered
    from nexus.engagement.drafting.context import build_context
    from nexus.engagement.subjects import reply_subject

    pack = await build_context(ts, enrollment=enrollment, contact=contact, account=account,
                               mailbox=mailbox, step=step, kind=kind, now=now)
    try:
        async with metered(ts, "ai.email_draft", user_id=user_id, source="engagement"):
            result = await get_agent_runtime().run(
                "messaging", ts, account_id=account.id, contact_id=contact.id,
                angle=getattr(step, "angle", "") or "", context_pack=pack.text,
                personal_facts=pack.facts,
            )
    except QuotaExceeded:
        return Draft(context_pack=pack.text, facts=pack.facts, error="out_of_credits")
    except Exception as exc:  # a failed draft is a state to show, not a crash
        return Draft(context_pack=pack.text, facts=pack.facts,
                     error=f"{type(exc).__name__}: {exc}"[:300])

    output = result.output or {}
    if result.status != "completed" or output.get("error"):
        return Draft(context_pack=pack.text, facts=pack.facts,
                     error=str(output.get("error") or result.error or "draft_failed"))

    subject = (output.get("subject") or "").strip()
    body = (output.get("body") or "").strip()
    if kind in ("followup", "reengage", "response") and thread is not None:
        # Exactly one "Re:", on the thread's own subject (D16): whatever the model wrote on the
        # subject line, a follow-up in a thread keeps the thread's subject.
        subject = reply_subject(thread.base_subject or subject)
    problems = check_draft(subject=subject, body=body, first_name=_first_name(contact),
                           facts=pack.facts, company_name=account.name or "")
    return Draft(subject=subject, body=body, problems=problems, context_pack=pack.text,
                 facts=pack.facts)
```

In `nexus/agents/messaging.py`, after the `RECENT SIGNALS` block add:

```python
        # The engagement engine's context pack (spec §7): the date in the buyer's timezone, the
        # whole conversation so far and every other conversation with this person. Placed with
        # the facts, before the rules, so "use only facts given above" covers it too.
        context_pack = (ctx.inputs.get("context_pack") or "").strip()
        if context_pack:
            content += f"\n{context_pack}\n"
        facts_to_use = [f for f in (ctx.inputs.get("personal_facts") or []) if f]
```

and pass `facts=facts_to_use, company_name=ctx.account.name or ""` to both `check_draft` calls, so the one regeneration also fixes a draft that used no fact.

- [ ] **Step 2: Run** `pytest tests/test_agent_copy.py tests/test_draft_structure.py tests/test_email_draft_option.py -n0 -q` — expected pass: existing callers pass no facts.
- [ ] **Step 3: Commit** — `git commit -am "feat(engagement): the context pack, drafted through MessagingAgent"`

---

### Task 3: Campaigns, enrolment and the review queue

- [ ] **Step 1: Write the failing tests** — add `test_steps_are_validated_before_anything_is_saved`, `test_enrolment_skips_who_cannot_be_emailed_and_warns_about_duplicate_outreach`, `test_the_recipients_timezone_comes_from_their_account_when_they_have_none`, `test_first_emails_are_drafted_once_personalised_and_waiting_for_review`, `test_an_edited_approval_is_recorded_against_the_ai_text`.
- [ ] **Step 2: Run** — expected FAIL: `No module named 'nexus.engagement.sequences.service'`.
- [ ] **Step 3: Implement** `nexus/engagement/sequences/service.py`:

```python
"""Campaigns, steps, enrollments, the review queue and launch (spec §4, §8, §9, §10).

The state machine an enrollment walks, and who moves it:

    awaiting_review ──approve──▶ active ──(sent last step)──▶ completed
          │                        │  ▲
          │                        │  └──resume── paused (manual | colleague_replied | out_of_office
          │                        │                       | needs_decision | out_of_credits
          │                        │                       | mailbox_disconnected)
          │                        ├──later──▶ snoozed ──(date)──▶ active
          └──remove──▶ stopped ◀───┴──(replied | declined | unsubscribed | bounced | manual)

Every transition goes through `set_status`, which records the ledger event for it, so "why did this
person stop receiving email" has one answer and one place it is written.

A first email is the step-0 outbound message row with status `draft`, then `approved`; `sending`
adopts that row when the step is due, so the text the SDR approved is exactly the text that is sent.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

CAMPAIGN_STATUSES = ("draft", "reviewing", "active", "paused", "completed")
TIMING_MODES = ("auto", "manual")
FIRST_SEND_MODES = ("on_approval", "scheduled")
PAUSE_REASONS = ("manual", "colleague_replied", "out_of_office", "needs_decision",
                 "out_of_credits", "mailbox_disconnected")
STOP_REASONS = ("replied", "declined", "unsubscribed", "bounced", "manual")


class CampaignError(ValueError):
    """A request the campaign's state does not allow. The message says what would."""


class CreditGateRefused(CampaignError):
    def __init__(self, estimate):
        self.estimate = estimate
        super().__init__(
            f"This campaign can cost up to {estimate.worst_credits:g} credits and the workspace has "
            f"{estimate.balance:g}. Top up {estimate.shortfall:g} credits, or remove contacts or "
            "steps, to launch it.")


@dataclass(slots=True)
class EnrollResult:
    added: list[str] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    #: The duplicate-outreach guard (§19): already in another active campaign in this workspace.
    warnings: list[dict] = field(default_factory=list)


# ---- campaigns and steps ------------------------------------------------------------------------

def validate_steps(specs: list[dict]) -> list[dict]:
    """Normalise step specs from a template or a form. Raises `CampaignError` on a bad one."""
    from nexus.engagement.timekeeping import parse_clock

    if not specs:
        raise CampaignError("A campaign needs at least one step.")
    if len(specs) > 12:
        raise CampaignError("A campaign can have at most 12 steps.")
    clean: list[dict] = []
    for index, spec in enumerate(specs):
        channel = (spec.get("channel") or "email").strip()
        if channel not in ("email", "call"):
            raise CampaignError(f"Step {index + 1}: channel must be email or call.")
        if index == 0 and channel != "email":
            raise CampaignError("The first step must be an email.")
        mode = (spec.get("timing_mode") or "auto").strip()
        if mode not in TIMING_MODES:
            raise CampaignError(f"Step {index + 1}: timing must be auto or manual.")
        delay = int(spec.get("delay_business_days") or 0)
        if index > 0 and not 0 <= delay <= 60:
            raise CampaignError(f"Step {index + 1}: the delay must be 0–60 business days.")
        clock = spec.get("send_time_local") or None
        if mode == "manual" and parse_clock(clock) is None:
            raise CampaignError(f"Step {index + 1}: manual timing needs a send time like 09:30.")
        weekdays = sorted({int(d) for d in (spec.get("allowed_weekdays") or [0, 1, 2, 3, 4])
                           if 0 <= int(d) <= 6}) or [0, 1, 2, 3, 4]
        clean.append({"channel": channel, "angle": (spec.get("angle") or "").strip()[:500],
                      "timing_mode": mode, "delay_business_days": 0 if index == 0 else delay,
                      "send_time_local": clock if mode == "manual" else None,
                      "allowed_weekdays": weekdays})
    return clean


async def create_campaign(ts, *, name: str, owner_user_id: str, mailbox_id: str,
                          steps: list[dict] | None = None, template_id: str | None = None,
                          review_every_touch: bool = False, first_send_mode: str = "on_approval",
                          first_send_at: datetime | None = None, timezone_mode: str = "contact",
                          source_list_id: str | None = None):
    from nexus.models.engagement import EngagementCampaign, MailboxConnection, SequenceTemplate

    mailbox = await ts.get(MailboxConnection, mailbox_id)
    if mailbox is None:
        raise CampaignError("Choose a connected mailbox to send from.")
    if mailbox.owner_user_id != owner_user_id:
        raise CampaignError("A campaign sends from its owner's own mailbox.")
    if first_send_mode not in FIRST_SEND_MODES:
        raise CampaignError("First emails send on approval or at a scheduled time.")
    if first_send_mode == "scheduled" and first_send_at is None:
        raise CampaignError("A scheduled first send needs a date and time.")
    if timezone_mode not in ("contact", "sdr"):
        raise CampaignError("Times follow the contact's timezone or the SDR's.")
    if template_id and not steps:
        template = await ts.get(SequenceTemplate, template_id)
        if template is None:
            raise CampaignError("That sequence template does not exist.")
        steps = list(template.steps or [])
    specs = validate_steps(steps or [])
    campaign = EngagementCampaign(
        name=(name or "").strip()[:200] or "Untitled campaign", owner_user_id=owner_user_id,
        mailbox_connection_id=mailbox.id, status="draft", review_every_touch=review_every_touch,
        first_send_mode=first_send_mode, first_send_at=first_send_at, timezone_mode=timezone_mode,
        source_list_id=source_list_id, template_id=template_id)
    ts.add(campaign)
    await ts.flush()
    await _write_steps(ts, campaign, specs)
    return campaign


async def replace_steps(ts, campaign, specs: list[dict]) -> None:
    if campaign.status not in ("draft", "reviewing"):
        raise CampaignError("Steps can only change before launch.")
    from nexus.models.engagement import EngagementStep

    for step in await ts.list(EngagementStep, EngagementStep.campaign_id == campaign.id):
        await ts.delete(step)
    await ts.flush()
    await _write_steps(ts, campaign, validate_steps(specs))


async def _write_steps(ts, campaign, specs: list[dict]) -> None:
    from nexus.models.engagement import EngagementStep

    for index, spec in enumerate(specs):
        ts.add(EngagementStep(campaign_id=campaign.id, step_index=index, **spec))
    await ts.flush()


async def steps_of(ts, campaign) -> list:
    from nexus.models.engagement import EngagementStep

    rows = await ts.list(EngagementStep, EngagementStep.campaign_id == campaign.id)
    return sorted(rows, key=lambda s: s.step_index)


# ---- enrollment ---------------------------------------------------------------------------------

async def enroll(ts, campaign, contact_ids: list[str]) -> EnrollResult:
    """Add contacts. Skips what cannot be emailed; warns about what someone else is emailing."""
    from nexus.engagement.suppression.service import active_block
    from nexus.engagement.timekeeping import resolve_zone
    from nexus.models.account import Account, Contact
    from nexus.models.engagement import (
        EngagementCampaign,
        EngagementEnrollment,
        MailboxConnection,
    )
    from nexus.models.identity import User

    if campaign.status not in ("draft", "reviewing", "active", "paused"):
        raise CampaignError("Contacts cannot be added to a completed campaign.")
    mailbox = await ts.get(MailboxConnection, campaign.mailbox_connection_id)
    result = EnrollResult()
    existing = {e.contact_id for e in await ts.list(
        EngagementEnrollment, EngagementEnrollment.campaign_id == campaign.id)}
    for contact_id in dict.fromkeys(contact_ids):
        contact = await ts.get(Contact, contact_id)
        if contact is None or getattr(contact, "deleted_at", None) is not None:
            result.skipped.append({"contact_id": contact_id, "reason": "not_found"})
            continue
        if contact_id in existing:
            result.skipped.append({"contact_id": contact_id, "reason": "already_enrolled"})
            continue
        if not (contact.email or "").strip():
            result.skipped.append({"contact_id": contact_id, "reason": "no_email"})
            continue
        block = await active_block(ts, contact.email)
        if block is not None:
            result.skipped.append({"contact_id": contact_id, "reason": f"do_not_contact:{block.reason}"})
            continue
        account = await ts.get(Account, contact.account_id)
        zone = resolve_zone(
            contact_tz=(contact.custom_fields or {}).get("timezone"),
            account_country=getattr(account, "country", None),
            account_region=getattr(account, "region", None),
            sdr_tz=getattr(mailbox, "timezone", None))
        ts.add(EngagementEnrollment(
            campaign_id=campaign.id, contact_id=contact.id, account_id=contact.account_id,
            mailbox_connection_id=campaign.mailbox_connection_id, status="awaiting_review",
            current_step_index=0, contact_timezone=zone.name))
        result.added.append(contact.id)

        others = await ts.list(
            EngagementEnrollment, EngagementEnrollment.contact_id == contact.id,
            EngagementEnrollment.campaign_id != campaign.id,
            EngagementEnrollment.status.in_(("active", "awaiting_review", "paused", "snoozed")))
        for other in others:
            other_campaign = await ts.get(EngagementCampaign, other.campaign_id)
            owner = await ts.session.get(User, other_campaign.owner_user_id) \
                if other_campaign else None
            result.warnings.append({
                "contact_id": contact.id, "campaign_id": other.campaign_id,
                "campaign_name": getattr(other_campaign, "name", ""),
                "owner": getattr(owner, "full_name", "") or getattr(owner, "email", ""),
            })
    await ts.flush()
    return result


async def set_status(ts, enrollment, status: str, reason: str | None = None, *,
                     user_id: str | None = None) -> None:
    """The one way an enrollment changes status, and the ledger event that says so."""
    from nexus.core.db import utcnow
    from nexus.engagement.ledger.emit import emit

    previous = enrollment.status
    if previous == status and enrollment.status_reason == reason:
        return
    enrollment.status = status
    enrollment.status_reason = reason
    now = utcnow()
    if status in ("stopped", "completed"):
        enrollment.finished_at = now
        enrollment.next_action_at = None
    event = {"active": "enrollment.resumed" if previous in ("paused", "snoozed")
             else "enrollment.started",
             "paused": "enrollment.paused", "snoozed": "enrollment.snoozed",
             "stopped": "enrollment.stopped", "completed": "enrollment.completed"}.get(status)
    await ts.flush()
    if event:
        await emit(ts, event, actor_user_id=user_id,
                   refs={"enrollment_id": enrollment.id, "campaign_id": enrollment.campaign_id,
                         "contact_id": enrollment.contact_id,
                         "account_id": enrollment.account_id},
                   payload={"from": previous, "to": status, "reason": reason or "",
                            "step_index": enrollment.current_step_index})


# ---- first emails and the review queue ----------------------------------------------------------

async def draft_first_emails(ts, campaign, *, user_id: str | None = None,
                             limit: int | None = None) -> dict:
    """Draft every step-0 email that has no draft yet. Idempotent: a second run drafts nothing."""
    from nexus.engagement.drafting.drafter import draft
    from nexus.models.account import Account, Contact
    from nexus.models.engagement import (
        EngagementEnrollment,
        EngagementMessage,
        MailboxConnection,
    )

    steps = await steps_of(ts, campaign)
    mailbox = await ts.get(MailboxConnection, campaign.mailbox_connection_id)
    pending = await ts.list(EngagementEnrollment, EngagementEnrollment.campaign_id == campaign.id,
                            EngagementEnrollment.status == "awaiting_review")
    drafted = failed = 0
    errors: list[dict] = []
    for enrollment in pending:
        if limit is not None and drafted >= limit:
            break
        if await first_draft(ts, enrollment) is not None:
            continue
        contact = await ts.get(Contact, enrollment.contact_id)
        account = await ts.get(Account, enrollment.account_id)
        result = await draft(ts, enrollment=enrollment, contact=contact, account=account,
                             mailbox=mailbox, step=steps[0], kind="first", user_id=user_id)
        if not result.ok:
            failed += 1
            errors.append({"contact_id": contact.id, "error": result.error})
            if result.error == "out_of_credits":
                break
            continue
        row = EngagementMessage(
            mailbox_connection_id=mailbox.id, enrollment_id=enrollment.id, contact_id=contact.id,
            direction="out", kind="step", status="draft", step_index=0,
            from_addr=mailbox.email, to_addrs=[contact.email], subject=result.subject,
            body_text=result.body, ai_subject=result.subject, ai_body=result.body,
            quality_problems=result.problems)
        ts.add(row)
        await ts.flush()
        await _emit_draft(ts, "draft.created", row, enrollment, result, user_id)
        drafted += 1
    if campaign.status == "draft" and drafted:
        campaign.status = "reviewing"
    await ts.flush()
    return {"drafted": drafted, "failed": failed, "errors": errors}


async def first_draft(ts, enrollment):
    from nexus.models.engagement import EngagementMessage

    return await ts.first(EngagementMessage, EngagementMessage.enrollment_id == enrollment.id,
                          EngagementMessage.step_index == 0,
                          EngagementMessage.direction == "out")


async def review_queue(ts, campaign) -> list[tuple]:
    """``[(enrollment, draft_row_or_None)]`` for everyone awaiting review, oldest first."""
    from nexus.models.engagement import EngagementEnrollment, EngagementMessage

    waiting = await ts.list(EngagementEnrollment, EngagementEnrollment.campaign_id == campaign.id,
                            EngagementEnrollment.status == "awaiting_review")
    out = []
    for enrollment in sorted(waiting, key=lambda e: e.created_at):
        row = await ts.first(EngagementMessage, EngagementMessage.enrollment_id == enrollment.id,
                             EngagementMessage.step_index == enrollment.current_step_index,
                             EngagementMessage.direction == "out")
        out.append((enrollment, row))
    return out


async def approve(ts, message, *, user_id: str, subject: str | None = None,
                  body: str | None = None) -> None:
    """Approve one draft, with the SDR's edits if any. An edit is recorded against the AI's text."""
    from nexus.core.db import utcnow
    from nexus.models.engagement import EngagementCampaign, EngagementEnrollment

    if message.status not in ("draft", "approved"):
        raise CampaignError("Only a draft can be approved.")
    enrollment = await ts.get(EngagementEnrollment, message.enrollment_id)
    campaign = await ts.get(EngagementCampaign, enrollment.campaign_id)
    if subject is not None:
        message.subject = subject.strip()
    if body is not None:
        message.body_text = body.strip()
    now = utcnow()
    message.status = "approved"
    message.approved_by_user_id = user_id
    message.approved_at = now
    await ts.flush()
    edited = (message.subject != (message.ai_subject or message.subject)
              or message.body_text != (message.ai_body or message.body_text))
    if edited:
        await _emit_edit(ts, message, enrollment, user_id)
    await _emit_draft(ts, "draft.approved", message, enrollment, None, user_id)
    if campaign.status == "active":
        await _activate(ts, campaign, enrollment, message, now, user_id)


async def approve_all_passing(ts, campaign, *, user_id: str) -> int:
    approved = 0
    for _enrollment, row in await review_queue(ts, campaign):
        if row is not None and row.status == "draft" and not row.quality_problems:
            await approve(ts, row, user_id=user_id)
            approved += 1
    return approved


async def regenerate(ts, message, *, user_id: str | None = None):
    """Draft this message again. The new text replaces the old, and so does its quality verdict."""
    from nexus.engagement.drafting.drafter import draft
    from nexus.models.account import Account, Contact
    from nexus.models.engagement import (
        EngagementCampaign,
        EngagementEnrollment,
        MailboxConnection,
    )

    if message.status not in ("draft", "approved"):
        raise CampaignError("Only a draft can be regenerated.")
    enrollment = await ts.get(EngagementEnrollment, message.enrollment_id)
    campaign = await ts.get(EngagementCampaign, enrollment.campaign_id)
    steps = await steps_of(ts, campaign)
    result = await draft(
        ts, enrollment=enrollment, contact=await ts.get(Contact, enrollment.contact_id),
        account=await ts.get(Account, enrollment.account_id),
        mailbox=await ts.get(MailboxConnection, campaign.mailbox_connection_id),
        step=steps[min(message.step_index or 0, len(steps) - 1)],
        kind="first" if not message.step_index else "followup", user_id=user_id)
    if not result.ok:
        raise CampaignError(f"The draft could not be written: {result.error}")
    message.subject = message.ai_subject = result.subject
    message.body_text = message.ai_body = result.body
    message.quality_problems = result.problems
    message.status = "draft"
    message.approved_at = None
    await ts.flush()
    await _emit_draft(ts, "draft.created", message, enrollment, result, user_id)
    return result


async def remove(ts, enrollment, *, user_id: str | None = None) -> None:
    """Take someone out of the campaign before anything was sent to them."""
    from nexus.models.engagement import EngagementMessage

    for row in await ts.list(EngagementMessage, EngagementMessage.enrollment_id == enrollment.id,
                             EngagementMessage.status.in_(("draft", "approved"))):
        await _emit_draft(ts, "draft.rejected", row, enrollment, None, user_id)
        await ts.delete(row)
    await set_status(ts, enrollment, "stopped", "manual", user_id=user_id)


# ---- launch, pause, overrides -------------------------------------------------------------------

async def launch(ts, campaign, *, user_id: str):
    """Run the credit gate (D18), then make every approved draft due. Returns the estimate."""
    from nexus.core.db import utcnow
    from nexus.engagement.sequences.estimate import estimate
    from nexus.models.engagement import EngagementEnrollment, MailboxConnection

    if campaign.status not in ("draft", "reviewing", "paused"):
        raise CampaignError("This campaign is already running or finished.")
    mailbox = await ts.get(MailboxConnection, campaign.mailbox_connection_id)
    if mailbox is None or mailbox.status != "connected":
        raise CampaignError("Reconnect the sending mailbox before launching.")
    result = await estimate(ts, campaign)
    if not result.covered:
        raise CreditGateRefused(result)
    now = utcnow()
    campaign.credit_estimate = result.as_dict()
    campaign.status = "active"
    campaign.pause_reason = None
    campaign.launched_at = campaign.launched_at or now
    await ts.flush()
    for enrollment in await ts.list(EngagementEnrollment,
                                    EngagementEnrollment.campaign_id == campaign.id,
                                    EngagementEnrollment.status == "awaiting_review"):
        row = await first_draft(ts, enrollment)
        if row is not None and row.status == "approved":
            await _activate(ts, campaign, enrollment, row, row.approved_at or now, user_id)
    return result


async def _activate(ts, campaign, enrollment, message, approved_at, user_id) -> None:
    from nexus.core.db import utcnow
    from nexus.engagement.sequences.timing import first_due_at

    enrollment.next_action_at = first_due_at(
        first_send_mode=campaign.first_send_mode, first_send_at=campaign.first_send_at,
        approved_at=approved_at, now=utcnow())
    enrollment.started_at = enrollment.started_at or utcnow()
    await set_status(ts, enrollment, "active", None, user_id=user_id)


async def pause_campaign(ts, campaign, reason: str = "manual") -> None:
    if campaign.status != "active":
        raise CampaignError("Only a running campaign can be paused.")
    campaign.status = "paused"
    campaign.pause_reason = reason
    await ts.flush()


async def resume_campaign(ts, campaign, *, user_id: str) -> None:
    """Resuming re-runs the gate: a campaign paused for credits must not resume into the same
    wall, and a manual pause may have outlived a top-up nobody made."""
    if campaign.status != "paused":
        raise CampaignError("Only a paused campaign can be resumed.")
    await launch(ts, campaign, user_id=user_id)


async def stop_campaign(ts, campaign, *, user_id: str) -> None:
    from nexus.core.db import utcnow
    from nexus.models.engagement import EngagementEnrollment

    for enrollment in await ts.list(
            EngagementEnrollment, EngagementEnrollment.campaign_id == campaign.id,
            EngagementEnrollment.status.in_(("active", "awaiting_review", "paused", "snoozed"))):
        await set_status(ts, enrollment, "stopped", "manual", user_id=user_id)
    campaign.status = "completed"
    campaign.completed_at = utcnow()
    await ts.flush()


async def pause_enrollment(ts, enrollment, reason: str = "manual", *,
                           user_id: str | None = None) -> None:
    if reason not in PAUSE_REASONS:
        raise CampaignError(f"Unknown pause reason {reason!r}.")
    if enrollment.status in ("stopped", "completed"):
        raise CampaignError("This contact has already finished the sequence.")
    await set_status(ts, enrollment, "paused", reason, user_id=user_id)


async def resume_enrollment(ts, enrollment, *, user_id: str | None = None) -> None:
    from nexus.core.db import utcnow

    if enrollment.status not in ("paused", "snoozed"):
        raise CampaignError("Only a paused or snoozed contact can be resumed.")
    enrollment.snoozed_until = None
    if enrollment.next_action_at is None or enrollment.next_action_at < utcnow():
        enrollment.next_action_at = utcnow()
    await set_status(ts, enrollment, "active", None, user_id=user_id)


async def stop_enrollment(ts, enrollment, reason: str = "manual", *,
                          user_id: str | None = None) -> None:
    if reason not in STOP_REASONS:
        raise CampaignError(f"Unknown stop reason {reason!r}.")
    await set_status(ts, enrollment, "stopped", reason, user_id=user_id)


async def move_next(ts, enrollment, when: datetime) -> None:
    """Per-contact override: the next step goes at this time, whatever the step's timing says."""
    if enrollment.status not in ("active", "paused"):
        raise CampaignError("Only a contact who is part-way through can be moved.")
    enrollment.next_action_at = when
    enrollment.next_action_override = True
    await ts.flush()


async def send_now(ts, enrollment) -> None:
    from nexus.core.db import utcnow

    await move_next(ts, enrollment, utcnow())


# ---- ledger ---------------------------------------------------------------------------------------

async def _emit_draft(ts, event_type, message, enrollment, result, user_id) -> None:
    from nexus.engagement.ledger.emit import emit

    payload = {"step_index": message.step_index, "kind": message.kind,
               "subject": message.subject, "body": message.body_text,
               "quality_problems": list(message.quality_problems or [])}
    if result is not None:
        payload["context_pack"] = result.context_pack
        payload["personalisation_facts"] = result.facts
    await emit(ts, event_type, actor_user_id=user_id,
               refs={"enrollment_id": enrollment.id, "campaign_id": enrollment.campaign_id,
                     "contact_id": enrollment.contact_id, "account_id": enrollment.account_id,
                     "message_id": message.id, "mailbox_id": message.mailbox_connection_id},
               payload=payload)


async def _emit_edit(ts, message, enrollment, user_id) -> None:
    from nexus.engagement.ledger.emit import emit

    await emit(ts, "draft.edited", actor_user_id=user_id,
               refs={"enrollment_id": enrollment.id, "campaign_id": enrollment.campaign_id,
                     "contact_id": enrollment.contact_id, "account_id": enrollment.account_id,
                     "message_id": message.id},
               payload={"ai_subject": message.ai_subject or "", "ai_body": message.ai_body or "",
                        "subject": message.subject, "body": message.body_text,
                        "edit_distance": _edit_distance(message.ai_body or "",
                                                        message.body_text or "")})


def _edit_distance(a: str, b: str) -> int:
    """Levenshtein over words: how much the SDR changed, in a unit a person recognises."""
    left, right = a.split(), b.split()
    previous = list(range(len(right) + 1))
    for i, word in enumerate(left, 1):
        current = [i]
        for j, other in enumerate(right, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1,
                               previous[j - 1] + (word != other)))
        previous = current
    return previous[-1]
```

- [ ] **Step 4: Run** — expected pass.
- [ ] **Step 5: Commit** — `git commit -am "feat(engagement): campaigns, enrolment with the duplicate-outreach guard, review queue"`

---

### Task 4: The estimate and the gate (D18)

- [ ] **Step 1: Write the failing tests** — add `test_launch_is_refused_when_the_worst_case_cannot_be_covered` and `test_the_gate_is_skipped_when_billing_is_off`.
- [ ] **Step 2: Run** — expected FAIL: `No module named 'nexus.engagement.sequences.estimate'`.
- [ ] **Step 3: Implement** `nexus/engagement/sequences/estimate.py`:

```python
"""What a campaign can cost, and whether the workspace can cover it before launch (spec §10, D18).

**The worst case is the gate, and it is the literal worst case**: every contact receives every email
step, each one drafted and sent, and every contact replies once, is classified and gets a suggested
response. A campaign that launches on an expected cost runs dry in week three, and a buyer who was
half-way through a sequence goes quiet for reasons they never see.

**The likely cost is shown for information.** It assumes each follow-up reaches the 85% of people
who have not answered yet, and 15% reply — a stated heuristic, not a forecast, and it gates nothing.

Prices come from the live rate cards through the same `_usage_amount` the meter charges with, so the
number on the launch screen is the number that is spent. The gate applies in `shadow` and `on`
billing modes (D18); it is skipped when billing is `off` and for unlimited plan classes.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

DRAFT = "ai.email_draft"
SEND = "outreach.email_send"
CLASSIFY = "ai.reply_classify"
REPLY_DRAFT = "ai.reply_draft"

#: The share of people who have not answered by the next follow-up, and who reply at all.
CONTINUE_RATE = 0.85
REPLY_RATE = 0.15


@dataclass(slots=True)
class Estimate:
    contacts: int
    email_steps: int
    worst_credits: float
    likely_credits: float
    balance: float
    gate_applies: bool
    covered: bool
    shortfall: float
    per_capability: dict

    def as_dict(self) -> dict:
        return asdict(self)


def worst_units(contacts: int, email_steps: int) -> dict[str, float]:
    sends = contacts * email_steps
    return {DRAFT: sends, SEND: sends, CLASSIFY: contacts, REPLY_DRAFT: contacts}


def likely_units(contacts: int, email_steps: int) -> dict[str, float]:
    sends = sum(contacts * CONTINUE_RATE ** i for i in range(email_steps))
    replies = contacts * REPLY_RATE
    return {DRAFT: sends, SEND: sends, CLASSIFY: replies, REPLY_DRAFT: replies}


async def estimate(ts, campaign) -> Estimate:
    from nexus.billing.credits import balance
    from nexus.billing.entitlements import _usage_amount, resolve_entitlement
    from nexus.core.config import get_settings
    from nexus.models.engagement import EngagementEnrollment, EngagementStep

    steps = await ts.list(EngagementStep, EngagementStep.campaign_id == campaign.id)
    email_steps = sum(1 for s in steps if s.channel == "email")
    enrollments = await ts.list(EngagementEnrollment,
                                EngagementEnrollment.campaign_id == campaign.id)
    contacts = sum(1 for e in enrollments if e.status not in ("stopped",))

    worst = worst_units(contacts, email_steps)
    likely = likely_units(contacts, email_steps)
    per: dict[str, dict] = {}
    worst_total = likely_total = 0.0
    unlimited = False
    for capability, quantity in worst.items():
        ent = await resolve_entitlement(ts, capability)
        unlimited = unlimited or ent.mode == "unlimited"
        worst_cost = float(await _usage_amount(ts, ent, quantity) or 0) if quantity else 0.0
        likely_cost = float(await _usage_amount(ts, ent, likely[capability]) or 0) \
            if likely[capability] else 0.0
        per[capability] = {"units": quantity, "credits": worst_cost,
                           "likely_units": round(likely[capability], 1),
                           "likely_credits": round(likely_cost, 2)}
        worst_total += worst_cost
        likely_total += likely_cost

    mode = get_settings().billing_enforcement
    gate_applies = mode in ("shadow", "on") and not unlimited
    available = float(await balance(ts))
    covered = (not gate_applies) or available >= worst_total
    return Estimate(
        contacts=contacts, email_steps=email_steps, worst_credits=round(worst_total, 2),
        likely_credits=round(likely_total, 2), balance=round(available, 2),
        gate_applies=gate_applies, covered=covered,
        shortfall=round(max(0.0, worst_total - available), 2) if gate_applies else 0.0,
        per_capability=per,
    )
```

- [ ] **Step 4: Run** — expected pass: 2 contacts × 3 steps × (2 + 1) = 18 credits refused on an empty balance, accepted after a top-up.
- [ ] **Step 5: Commit** — `git commit -am "feat(engagement): worst-case credit estimate and the launch gate (D18)"`

---

### Task 5: The worker

- [ ] **Step 1: Write the failing tests** — add the five worker tests and `test_the_worker_job_is_dark_until_the_switch_is_on`.
- [ ] **Step 2: Run** — expected FAIL: `No module named 'nexus.engagement.sequences.advance'`.
- [ ] **Step 3: Implement** `nexus/engagement/sequences/advance.py`:

```python
"""The heartbeat's side of a campaign: find what is due, write it just in time, send it (§8, D5).

**Claiming.** Due enrollments are read across workspaces by id only, then each one is processed in
its own tenant session under ``SELECT ... FOR UPDATE SKIP LOCKED`` (spec §8), so two workers never
take the same enrollment and a slow one never blocks the rest. SQLite ignores the lock; the
integration suite proves it on Postgres. Even without the lock the send cannot happen twice — the
outbound row's partial unique index is the second line (phase 07).

**Just in time.** A follow-up is drafted when it is due, from the whole conversation as it stands
(D5): drafting at launch would write week-three emails before week one was answered, and spend
credits on people who replied.

**What each send outcome does to the enrollment:**

| send | enrollment |
|---|---|
| sent | next step scheduled from this send, or completed |
| held (mailbox, provider, retry) | stays due; looked at again after a short wait |
| held (quality) | the draft waits for review (D17) |
| held (credits) | paused `out_of_credits`, and so is the campaign (D18) |
| stopped | stopped, with the reason |
| failed | paused `needs_decision` |
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

logger = logging.getLogger("nexus.engagement.sequences")

#: How long a held step waits before it is looked at again. Longer than a tick, so a paused
#: mailbox is not re-checked every minute; shorter than any delay a person would notice.
RETRY_AFTER = timedelta(minutes=15)
BATCH = 50


async def due_enrollments(now: datetime, limit: int = BATCH) -> list[tuple[str, str]]:
    """``[(tenant_id, enrollment_id)]`` due now, across workspaces, soonest first.

    Read on the platform sessionmaker: under the RLS-bound app role a cross-tenant read returns zero
    rows, and a heartbeat that finds nothing due looks exactly like a quiet day."""
    from sqlalchemy import or_, select

    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.engagement import EngagementCampaign, EngagementEnrollment

    async with get_platform_sessionmaker()() as session:
        rows = (await session.execute(
            select(EngagementEnrollment.tenant_id, EngagementEnrollment.id)
            .join(EngagementCampaign, EngagementCampaign.id == EngagementEnrollment.campaign_id)
            .where(EngagementCampaign.status == "active")
            .where(or_(
                (EngagementEnrollment.status == "active")
                & (EngagementEnrollment.next_action_at <= now),
                (EngagementEnrollment.status == "snoozed")
                & (EngagementEnrollment.snoozed_until <= now),
            ))
            .order_by(EngagementEnrollment.next_action_at)
            .limit(limit)
        )).all()
    return [(tenant_id, enrollment_id) for tenant_id, enrollment_id in rows]


async def advance(now: datetime | None = None, limit: int = BATCH) -> dict:
    """One heartbeat's worth of sends. Never raises for one bad enrollment."""
    from nexus.core.db import utcnow
    from nexus.workers.tasks import tenant_session

    moment = now or utcnow()
    outcomes: dict[str, int] = {}
    for tenant_id, enrollment_id in await due_enrollments(moment, limit):
        try:
            async with tenant_session(tenant_id) as ts:
                outcome = await process(ts, enrollment_id, now=moment)
        except Exception:
            logger.warning("engagement enrollment %s failed to advance", enrollment_id,
                           exc_info=True)
            outcome = "error"
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
    return outcomes


async def process(ts, enrollment_id: str, *, now: datetime) -> str:
    """Advance one enrollment by at most one message. Returns what happened, in one word."""
    from nexus.engagement.sequences.service import set_status, steps_of
    from nexus.models.account import Account, Contact
    from nexus.models.engagement import (
        EngagementCampaign,
        EngagementEnrollment,
        EngagementThread,
        MailboxConnection,
    )

    enrollment = (await ts.session.scalars(
        ts.select(EngagementEnrollment, EngagementEnrollment.id == enrollment_id)
        .with_for_update(skip_locked=True)
    )).first()
    if enrollment is None:
        return "locked"
    campaign = await ts.get(EngagementCampaign, enrollment.campaign_id)
    if campaign is None or campaign.status != "active":
        return "campaign_not_active"

    snoozed = enrollment.status == "snoozed" and enrollment.snoozed_until \
        and enrollment.snoozed_until <= now
    due = enrollment.status == "active" and enrollment.next_action_at \
        and enrollment.next_action_at <= now
    if not (snoozed or due):
        return "not_due"

    steps = await steps_of(ts, campaign)
    if not snoozed and enrollment.current_step_index >= len(steps):
        await set_status(ts, enrollment, "completed")
        return "completed"

    mailbox = await ts.get(MailboxConnection, campaign.mailbox_connection_id)
    contact = await ts.get(Contact, enrollment.contact_id)
    account = await ts.get(Account, enrollment.account_id)
    thread = await ts.get(EngagementThread, enrollment.current_thread_id) \
        if enrollment.current_thread_id else None
    if mailbox is None or contact is None or account is None:
        await set_status(ts, enrollment, "paused", "needs_decision")
        return "missing_records"
    if mailbox.status != "connected":
        await set_status(ts, enrollment, "paused", "mailbox_disconnected")
        return "mailbox_disconnected"

    if snoozed:
        return await _reengage(ts, campaign, enrollment, steps, mailbox, contact, account,
                               thread, now)

    step = steps[enrollment.current_step_index]
    if step.channel == "call":
        return await _call_step(ts, campaign, enrollment, steps, step, contact, now)
    return await _email_step(ts, campaign, enrollment, steps, step, mailbox, contact, account,
                             thread, now)


async def _email_step(ts, campaign, enrollment, steps, step, mailbox, contact, account, thread,
                      now) -> str:
    from nexus.engagement.drafting.drafter import draft
    from nexus.engagement.sequences.service import set_status
    from nexus.models.engagement import EngagementMessage

    index = enrollment.current_step_index
    row = await ts.first(EngagementMessage, EngagementMessage.enrollment_id == enrollment.id,
                         EngagementMessage.step_index == index,
                         EngagementMessage.direction == "out")
    context: dict = {}
    if row is not None and row.status in ("approved", "queued"):
        subject, body = row.subject, row.body_text
        ai_subject, ai_body, problems = row.ai_subject, row.ai_body, []
    elif row is not None and row.status == "draft":
        # Written, not yet approved: it waits for the SDR, it is not re-drafted.
        await set_status(ts, enrollment, "awaiting_review")
        return "awaiting_review"
    elif row is not None and row.status == "sent":
        return await _after_send(ts, campaign, enrollment, steps, row.thread_id, row.sent_at or now)
    elif row is not None and row.status == "failed":
        await set_status(ts, enrollment, "paused", "needs_decision")
        return "failed"
    else:
        if index == 0:
            # A first email is reviewed before it goes (D4); an active enrollment with no approved
            # draft is a state the review queue must resolve, not one the worker papers over.
            await set_status(ts, enrollment, "awaiting_review")
            return "awaiting_review"
        written = await draft(ts, enrollment=enrollment, contact=contact, account=account,
                              mailbox=mailbox, step=step, kind="followup", thread=thread, now=now)
        if not written.ok:
            if written.error == "out_of_credits":
                return await _out_of_credits(ts, campaign, enrollment)
            enrollment.next_action_at = now + RETRY_AFTER
            await ts.flush()
            return "draft_failed"
        subject, body, problems = written.subject, written.body, written.problems
        ai_subject, ai_body = written.subject, written.body
        context = {"context_pack": written.context_pack,
                   "personalisation_facts": written.facts}
        if campaign.review_every_touch or problems:
            # Reviewed on request, or held because it failed a check nobody was there to see (D17).
            ts.add(EngagementMessage(
                mailbox_connection_id=mailbox.id, enrollment_id=enrollment.id,
                contact_id=contact.id, thread_id=getattr(thread, "id", None), direction="out",
                kind="step", status="draft", step_index=index, from_addr=mailbox.email,
                to_addrs=[contact.email], subject=subject, body_text=body,
                ai_subject=ai_subject, ai_body=ai_body, quality_problems=problems))
            await ts.flush()
            await set_status(ts, enrollment, "awaiting_review",
                             "needs_decision" if problems else None)
            return "awaiting_review"

    return await _send(ts, campaign, enrollment, steps, mailbox, contact, thread, subject, body,
                       ai_subject, ai_body, problems, "step", index, None, context, now)


async def _reengage(ts, campaign, enrollment, steps, mailbox, contact, account, thread,
                    now) -> str:
    """The one email a "later, with a date" reply earned (D6), in the same thread."""
    from nexus.engagement.drafting.drafter import draft

    written = await draft(ts, enrollment=enrollment, contact=contact, account=account,
                          mailbox=mailbox, kind="reengage", thread=thread, now=now)
    if not written.ok:
        if written.error == "out_of_credits":
            return await _out_of_credits(ts, campaign, enrollment)
        enrollment.snoozed_until = now + RETRY_AFTER
        await ts.flush()
        return "draft_failed"
    key = f"reengage:{enrollment.id}:{enrollment.snoozed_until.isoformat()}"
    return await _send(ts, campaign, enrollment, steps, mailbox, contact, thread, written.subject,
                       written.body, written.subject, written.body, written.problems, "reengage",
                       None, key, {"context_pack": written.context_pack,
                                   "personalisation_facts": written.facts}, now)


async def _send(ts, campaign, enrollment, steps, mailbox, contact, thread, subject, body,
                ai_subject, ai_body, problems, kind, step_index, key, context, now) -> str:
    from nexus.engagement.sending.service import send
    from nexus.engagement.sequences.service import set_status

    result = await send(ts, mailbox=mailbox, contact=contact, subject=subject, body=body,
                        enrollment=enrollment, thread=thread, step_index=step_index, kind=kind,
                        ai_subject=ai_subject, ai_body=ai_body, quality_problems=problems,
                        idempotency_key=key, user_id=campaign.owner_user_id, context=context)
    if result.outcome == "sent":
        if kind == "reengage":
            enrollment.snoozed_until = None
            await set_status(ts, enrollment, "active")
        return await _after_send(ts, campaign, enrollment, steps, result.thread_id, now,
                                 advance_step=kind == "step")
    if result.outcome == "stopped":
        reason = _stop_reason(result.reason)
        await set_status(ts, enrollment, "stopped", reason)
        return f"stopped:{reason}"
    if result.outcome == "failed":
        await set_status(ts, enrollment, "paused", "needs_decision")
        return "failed"
    if "cover" in result.reason:
        return await _out_of_credits(ts, campaign, enrollment)
    wait = max(now + RETRY_AFTER, mailbox.paused_until or now)
    if kind == "reengage":
        enrollment.snoozed_until = wait
    else:
        enrollment.next_action_at = wait
    await ts.flush()
    return "held"


async def _after_send(ts, campaign, enrollment, steps, thread_id, sent_at, *,
                      advance_step: bool = True) -> str:
    from nexus.engagement.sequences.service import set_status
    from nexus.engagement.sequences.timing import followup_due_at, zone_name_for
    from nexus.models.engagement import MailboxConnection

    if thread_id:
        enrollment.current_thread_id = thread_id
    if advance_step:
        enrollment.current_step_index += 1
    enrollment.next_action_override = False
    if enrollment.current_step_index >= len(steps):
        await set_status(ts, enrollment, "completed")
        return "sent_completed"
    mailbox = await ts.get(MailboxConnection, campaign.mailbox_connection_id)
    enrollment.next_action_at = followup_due_at(
        step=steps[enrollment.current_step_index], previous_sent_at=sent_at,
        zone_name=zone_name_for(enrollment, campaign, mailbox))
    await ts.flush()
    return "sent"


async def _call_step(ts, campaign, enrollment, steps, step, contact, now) -> str:
    """A call step becomes a task in the rep's call queue, and the sequence moves on."""
    from nexus.models.calling import CallTask

    ts.add(CallTask(account_id=enrollment.account_id, contact_id=contact.id,
                    reason=step.angle or f"Step {step.step_index + 1} of {campaign.name}",
                    priority=60, owner_user_id=campaign.owner_user_id, due_at=now,
                    engagement_enrollment_id=enrollment.id))
    await ts.flush()
    return await _after_send(ts, campaign, enrollment, steps, enrollment.current_thread_id, now)


async def _out_of_credits(ts, campaign, enrollment) -> str:
    """D18: a campaign that runs out mid-way pauses — every contact, not only this one."""
    from nexus.engagement.sequences.service import set_status

    await set_status(ts, enrollment, "paused", "out_of_credits")
    campaign.status = "paused"
    campaign.pause_reason = "out_of_credits"
    await ts.flush()
    return "out_of_credits"


def _stop_reason(reason: str) -> str:
    text = (reason or "").lower()
    for word in ("unsubscribed", "declined", "bounced"):
        if word in text:
            return word
    if "replied" in text:
        return "replied"
    return "manual"
```

In `nexus/engagement/sending/service.py`, `_claim_row` adopts an approved draft instead of treating it as a previous attempt:

```python
    existing = await _existing(ts, enrollment, step_index, idempotency_key)
    if existing is not None and existing.status in ("draft", "approved"):
        # The draft the SDR approved (phase 08) IS the outbound row: adopt it, so the text that
        # was approved is exactly the text that is sent, and give it the ids it will carry.
        parent = await _latest_in_thread(ts, thread, exclude_id=existing.id)
        existing.thread_id = existing.thread_id or getattr(thread, "id", None)
        existing.subject, existing.body_text = subject, body
        existing.rfc_message_id = existing.rfc_message_id or mime.new_rfc_message_id(
            _domain(mailbox.email))
        existing.ref_header = existing.ref_header or new_ulid()
        existing.in_reply_to = getattr(parent, "rfc_message_id", "") or ""
        existing.references_header = mime.references_for(
            getattr(parent, "references_header", "") or "",
            getattr(parent, "rfc_message_id", "") or "")
        existing.status = "queued"
        await ts.flush()
        return existing, False
    if existing is not None:
        return existing, True
```

In `nexus/workers/tasks.py`, add above `enqueue_ship_ledger` and register `"advance_engagement": handle_advance_engagement` in `HANDLERS`:

```python
async def handle_advance_engagement(payload: dict) -> dict:
    """Send what is due in running engagement campaigns (spec §8). Does nothing while the engine
    is dark: the switch is re-read here, not only when the heartbeat enqueues."""
    from nexus.engagement import config
    from nexus.engagement.sequences.advance import advance

    if not config.campaigns_enabled():
        return {"skipped": "engagement campaigns are switched off"}
    return await advance()


async def enqueue_advance_engagement(*, queue: TaskQueue | None = None) -> None:
    queue = queue or get_task_queue()
    await queue.enqueue(Job(name="advance_engagement", payload={}))
```

In `nexus/workers/scheduler.py`, import `enqueue_advance_engagement` and after the ledger jobs:

```python
            # Engagement campaigns send on their own switch, not automation_enabled: an SDR who
            # launched a campaign expects it to run. Dark until the cutover flips it on.
            from nexus.engagement import config as engagement_config

            if engagement_config.campaigns_enabled():
                await enqueue_advance_engagement(queue=queue)
                count += 1
```

(The scheduler counts in `test_continuous_automation.py` and `test_crm_auto_sync.py` do not change: the switch is off by default.)

- [ ] **Step 4: Run** `pytest tests/test_engagement_sequences.py tests/test_engagement_sending.py tests/test_continuous_automation.py tests/test_crm_auto_sync.py -n0 -q` — expected pass.
- [ ] **Step 5: Commit** — `git commit -am "feat(engagement): the worker — just-in-time follow-ups, threaded, held when unsafe"`

---

### Task 6: The API

`nexus/api/routers/engagement_campaigns.py`:

```python
"""Engagement campaigns: build, draft, review, launch, and steer (spec §9).

Dark until the cutover: every route answers 404 while `engagement_campaigns_enabled` is off, so the
new engine cannot be reached by URL before it is switched on — the same effect as the hidden nav
item, enforced where it matters.

An SDR works their own campaigns (`run_engagement`); a manager can act on anyone's
(`manage_engagement`). A campaign sends from its owner's own mailbox, so ownership is not a label:
it decides whose name is on every email.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from nexus.api.deps import Principal, get_tenant_session, require
from nexus.core.rbac import Permission, Role, has_permission
from nexus.core.tenancy import TenantSession


async def require_campaigns_enabled() -> None:
    from nexus.engagement import config

    if not config.campaigns_enabled():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")


router = APIRouter(prefix="/engagement", tags=["engagement"],
                   dependencies=[Depends(require_campaigns_enabled)])


class StepIn(BaseModel):
    model_config = {"extra": "forbid"}

    channel: str = "email"
    angle: str = ""
    timing_mode: str = "auto"
    delay_business_days: int = 0
    send_time_local: str | None = None
    allowed_weekdays: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4])


class CampaignIn(BaseModel):
    model_config = {"extra": "forbid"}

    name: str
    mailbox_id: str
    steps: list[StepIn] = Field(default_factory=list)
    template_id: str | None = None
    review_every_touch: bool = False
    first_send_mode: str = "on_approval"
    first_send_at: datetime | None = None
    timezone_mode: str = "contact"
    source_list_id: str | None = None


class StepOut(StepIn):
    step_index: int


class CampaignOut(BaseModel):
    id: str
    name: str
    owner_user_id: str
    mailbox_connection_id: str | None
    status: str
    pause_reason: str | None
    review_every_touch: bool
    first_send_mode: str
    first_send_at: datetime | None
    timezone_mode: str
    launched_at: datetime | None
    steps: list[StepOut]
    counts: dict[str, int]


class ContactsIn(BaseModel):
    model_config = {"extra": "forbid"}

    contact_ids: list[str] = Field(min_length=1, max_length=2000)


class ReviewItemOut(BaseModel):
    enrollment_id: str
    contact_id: str
    message_id: str | None
    subject: str
    body: str
    quality_problems: list[str]
    status: str


class ApproveIn(BaseModel):
    model_config = {"extra": "forbid"}

    subject: str | None = None
    body: str | None = None


class MoveIn(BaseModel):
    model_config = {"extra": "forbid"}

    when: datetime


class EnrollmentOut(BaseModel):
    id: str
    contact_id: str
    account_id: str
    status: str
    status_reason: str | None
    current_step_index: int
    next_action_at: datetime | None
    snoozed_until: datetime | None
    contact_timezone: str


def _is_manager(principal: Principal) -> bool:
    return has_permission(Role(principal.role), Permission.manage_engagement)


async def _campaign(ts: TenantSession, campaign_id: str, principal: Principal):
    from nexus.models.engagement import EngagementCampaign

    campaign = await ts.get(EngagementCampaign, campaign_id)
    if campaign is None or (campaign.owner_user_id != principal.user_id
                            and not _is_manager(principal)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Campaign not found")
    return campaign


async def _out(ts: TenantSession, campaign) -> CampaignOut:
    from nexus.engagement.sequences.service import steps_of
    from nexus.models.engagement import EngagementEnrollment

    counts: dict[str, int] = {}
    for enrollment in await ts.list(EngagementEnrollment,
                                    EngagementEnrollment.campaign_id == campaign.id):
        counts[enrollment.status] = counts.get(enrollment.status, 0) + 1
    return CampaignOut(
        id=campaign.id, name=campaign.name, owner_user_id=campaign.owner_user_id,
        mailbox_connection_id=campaign.mailbox_connection_id, status=campaign.status,
        pause_reason=campaign.pause_reason, review_every_touch=campaign.review_every_touch,
        first_send_mode=campaign.first_send_mode, first_send_at=campaign.first_send_at,
        timezone_mode=campaign.timezone_mode, launched_at=campaign.launched_at,
        steps=[StepOut(step_index=s.step_index, channel=s.channel, angle=s.angle,
                       timing_mode=s.timing_mode, delay_business_days=s.delay_business_days,
                       send_time_local=s.send_time_local,
                       allowed_weekdays=list(s.allowed_weekdays or []))
               for s in await steps_of(ts, campaign)],
        counts=counts)


def _refuse(exc: Exception) -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, str(exc))


@router.get("/campaigns", response_model=list[CampaignOut])
async def list_campaigns(
    team: bool = False,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> list[CampaignOut]:
    from nexus.models.engagement import EngagementCampaign

    where = [] if (team and _is_manager(principal)) else [
        EngagementCampaign.owner_user_id == principal.user_id]
    rows = await ts.list(EngagementCampaign, *where)
    return [await _out(ts, c) for c in sorted(rows, key=lambda c: c.created_at, reverse=True)]


@router.post("/campaigns", response_model=CampaignOut, status_code=201)
async def create(
    body: CampaignIn,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> CampaignOut:
    from nexus.engagement.sequences.service import CampaignError, create_campaign

    try:
        campaign = await create_campaign(
            ts, name=body.name, owner_user_id=principal.user_id, mailbox_id=body.mailbox_id,
            steps=[s.model_dump() for s in body.steps], template_id=body.template_id,
            review_every_touch=body.review_every_touch, first_send_mode=body.first_send_mode,
            first_send_at=body.first_send_at, timezone_mode=body.timezone_mode,
            source_list_id=body.source_list_id)
    except CampaignError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return await _out(ts, campaign)


@router.get("/campaigns/{campaign_id}", response_model=CampaignOut)
async def get_campaign(
    campaign_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> CampaignOut:
    return await _out(ts, await _campaign(ts, campaign_id, principal))


@router.put("/campaigns/{campaign_id}/steps", response_model=CampaignOut)
async def put_steps(
    campaign_id: str, body: list[StepIn],
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> CampaignOut:
    from nexus.engagement.sequences.service import CampaignError, replace_steps

    campaign = await _campaign(ts, campaign_id, principal)
    try:
        await replace_steps(ts, campaign, [s.model_dump() for s in body])
    except CampaignError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return await _out(ts, campaign)


@router.post("/campaigns/{campaign_id}/contacts")
async def add_contacts(
    campaign_id: str, body: ContactsIn,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> dict:
    from nexus.engagement.sequences.service import CampaignError, enroll

    campaign = await _campaign(ts, campaign_id, principal)
    try:
        result = await enroll(ts, campaign, body.contact_ids)
    except CampaignError as exc:
        raise _refuse(exc) from exc
    return {"added": result.added, "skipped": result.skipped, "warnings": result.warnings}


@router.post("/campaigns/{campaign_id}/draft")
async def draft_campaign(
    campaign_id: str, limit: int = 25,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> dict:
    """Draft up to ``limit`` first emails. The screen calls again until nothing is left."""
    from nexus.engagement.sequences.service import draft_first_emails

    campaign = await _campaign(ts, campaign_id, principal)
    return await draft_first_emails(ts, campaign, user_id=principal.user_id,
                                     limit=max(1, min(limit, 50)))


@router.get("/campaigns/{campaign_id}/review", response_model=list[ReviewItemOut])
async def get_review(
    campaign_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> list[ReviewItemOut]:
    from nexus.engagement.sequences.service import review_queue

    campaign = await _campaign(ts, campaign_id, principal)
    return [ReviewItemOut(
        enrollment_id=e.id, contact_id=e.contact_id, message_id=getattr(m, "id", None),
        subject=getattr(m, "subject", "") or "", body=getattr(m, "body_text", "") or "",
        quality_problems=list(getattr(m, "quality_problems", None) or []),
        status=getattr(m, "status", "undrafted")) for e, m in await review_queue(ts, campaign)]


async def _message(ts: TenantSession, message_id: str, principal: Principal):
    from nexus.models.engagement import EngagementEnrollment, EngagementMessage

    message = await ts.get(EngagementMessage, message_id)
    if message is None or message.enrollment_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Draft not found")
    enrollment = await ts.get(EngagementEnrollment, message.enrollment_id)
    await _campaign(ts, enrollment.campaign_id, principal)
    return message


@router.post("/messages/{message_id}/approve", status_code=204, response_model=None)
async def approve_message(
    message_id: str, body: ApproveIn,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> None:
    from nexus.engagement.sequences.service import CampaignError, approve

    message = await _message(ts, message_id, principal)
    try:
        await approve(ts, message, user_id=principal.user_id, subject=body.subject, body=body.body)
    except CampaignError as exc:
        raise _refuse(exc) from exc


@router.post("/messages/{message_id}/regenerate", response_model=ReviewItemOut)
async def regenerate_message(
    message_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> ReviewItemOut:
    from nexus.engagement.sequences.service import CampaignError, regenerate

    message = await _message(ts, message_id, principal)
    try:
        await regenerate(ts, message, user_id=principal.user_id)
    except CampaignError as exc:
        raise _refuse(exc) from exc
    return ReviewItemOut(enrollment_id=message.enrollment_id, contact_id=message.contact_id,
                         message_id=message.id, subject=message.subject, body=message.body_text,
                         quality_problems=list(message.quality_problems or []),
                         status=message.status)


@router.post("/campaigns/{campaign_id}/approve-all")
async def approve_all(
    campaign_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> dict:
    from nexus.engagement.sequences.service import approve_all_passing

    campaign = await _campaign(ts, campaign_id, principal)
    return {"approved": await approve_all_passing(ts, campaign, user_id=principal.user_id)}


@router.get("/campaigns/{campaign_id}/estimate")
async def get_estimate(
    campaign_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> dict:
    from nexus.engagement.sending.limits import sent_today, volume_warning
    from nexus.engagement.sequences.estimate import estimate
    from nexus.models.engagement import EngagementEnrollment, MailboxConnection

    campaign = await _campaign(ts, campaign_id, principal)
    result = await estimate(ts, campaign)
    mailbox = await ts.get(MailboxConnection, campaign.mailbox_connection_id)
    ready = len(await ts.list(EngagementEnrollment,
                              EngagementEnrollment.campaign_id == campaign.id,
                              EngagementEnrollment.status.in_(("awaiting_review", "active"))))
    today = await sent_today(ts, mailbox) if mailbox else 0
    return {**result.as_dict(),
            "volume_warning": volume_warning(getattr(mailbox, "email", ""), today, planned=ready)}


@router.post("/campaigns/{campaign_id}/launch")
async def launch_campaign(
    campaign_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> dict:
    from nexus.engagement.sequences.service import CampaignError, CreditGateRefused, launch

    campaign = await _campaign(ts, campaign_id, principal)
    try:
        result = await launch(ts, campaign, user_id=principal.user_id)
    except CreditGateRefused as exc:
        raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED,
                            {"detail": str(exc), "estimate": exc.estimate.as_dict()}) from exc
    except CampaignError as exc:
        raise _refuse(exc) from exc
    return {"status": campaign.status, "estimate": result.as_dict()}


@router.post("/campaigns/{campaign_id}/{action}", status_code=204, response_model=None)
async def campaign_action(
    campaign_id: str, action: str,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> None:
    from nexus.engagement.sequences import service

    campaign = await _campaign(ts, campaign_id, principal)
    try:
        if action == "pause":
            await service.pause_campaign(ts, campaign)
        elif action == "resume":
            await service.resume_campaign(ts, campaign, user_id=principal.user_id)
        elif action == "stop":
            await service.stop_campaign(ts, campaign, user_id=principal.user_id)
        else:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown action")
    except service.CreditGateRefused as exc:
        raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, str(exc)) from exc
    except service.CampaignError as exc:
        raise _refuse(exc) from exc


@router.get("/campaigns/{campaign_id}/enrollments", response_model=list[EnrollmentOut])
async def list_enrollments(
    campaign_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> list[EnrollmentOut]:
    from nexus.models.engagement import EngagementEnrollment

    campaign = await _campaign(ts, campaign_id, principal)
    rows = await ts.list(EngagementEnrollment, EngagementEnrollment.campaign_id == campaign.id)
    return [EnrollmentOut(
        id=e.id, contact_id=e.contact_id, account_id=e.account_id, status=e.status,
        status_reason=e.status_reason, current_step_index=e.current_step_index,
        next_action_at=e.next_action_at, snoozed_until=e.snoozed_until,
        contact_timezone=e.contact_timezone) for e in rows]


async def _enrollment(ts: TenantSession, enrollment_id: str, principal: Principal):
    from nexus.models.engagement import EngagementEnrollment

    enrollment = await ts.get(EngagementEnrollment, enrollment_id)
    if enrollment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Enrollment not found")
    await _campaign(ts, enrollment.campaign_id, principal)
    return enrollment


@router.post("/enrollments/{enrollment_id}/move", status_code=204, response_model=None)
async def move_enrollment(
    enrollment_id: str, body: MoveIn,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> None:
    from nexus.engagement.sequences.service import CampaignError, move_next

    try:
        await move_next(ts, await _enrollment(ts, enrollment_id, principal), body.when)
    except CampaignError as exc:
        raise _refuse(exc) from exc


@router.post("/enrollments/{enrollment_id}/{action}", status_code=204, response_model=None)
async def enrollment_action(
    enrollment_id: str, action: str,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> None:
    from nexus.engagement.sequences import service

    enrollment = await _enrollment(ts, enrollment_id, principal)
    try:
        if action == "pause":
            await service.pause_enrollment(ts, enrollment, user_id=principal.user_id)
        elif action == "resume":
            await service.resume_enrollment(ts, enrollment, user_id=principal.user_id)
        elif action == "stop":
            await service.stop_enrollment(ts, enrollment, user_id=principal.user_id)
        elif action == "send-now":
            await service.send_now(ts, enrollment)
        elif action == "remove":
            await service.remove(ts, enrollment, user_id=principal.user_id)
        else:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown action")
    except service.CampaignError as exc:
        raise _refuse(exc) from exc
```

`nexus/api/routers/engagement_templates.py`:

```python
"""Sequence templates: reusable step lists a campaign starts from (spec §9 — Cadences becomes this).

A template is only a list of step specs; a campaign copies them at creation, so editing a template
later never changes a campaign that is already sending. Archiving hides a template from the picker
and deletes nothing, because a campaign records which template it came from.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from nexus.api.deps import Principal, get_tenant_session, require
from nexus.api.routers.engagement_campaigns import StepIn, require_campaigns_enabled
from nexus.core.rbac import Permission
from nexus.core.tenancy import TenantSession

router = APIRouter(prefix="/engagement/templates", tags=["engagement"],
                   dependencies=[Depends(require_campaigns_enabled)])


class TemplateIn(BaseModel):
    model_config = {"extra": "forbid"}

    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    steps: list[StepIn] = Field(min_length=1)


class TemplateOut(BaseModel):
    id: str
    name: str
    description: str
    steps: list[dict]
    created_at: datetime


def _out(row) -> TemplateOut:
    return TemplateOut(id=row.id, name=row.name, description=row.description or "",
                       steps=list(row.steps or []), created_at=row.created_at)


def _validated(body: TemplateIn) -> list[dict]:
    from nexus.engagement.sequences.service import CampaignError, validate_steps

    try:
        return validate_steps([s.model_dump() for s in body.steps])
    except CampaignError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc


@router.get("", response_model=list[TemplateOut])
async def list_templates(
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.run_engagement)),
) -> list[TemplateOut]:
    from nexus.models.engagement import SequenceTemplate

    rows = await ts.list(SequenceTemplate, SequenceTemplate.archived_at.is_(None))
    return [_out(r) for r in sorted(rows, key=lambda r: r.name.lower())]


@router.post("", response_model=TemplateOut, status_code=201)
async def create_template(
    body: TemplateIn,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_engagement)),
) -> TemplateOut:
    from nexus.models.engagement import SequenceTemplate

    row = SequenceTemplate(name=body.name.strip(), description=body.description.strip(),
                           steps=_validated(body), created_by_user_id=principal.user_id)
    ts.add(row)
    await ts.flush()
    return _out(row)


@router.put("/{template_id}", response_model=TemplateOut)
async def update_template(
    template_id: str, body: TemplateIn,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_engagement)),
) -> TemplateOut:
    from nexus.models.engagement import SequenceTemplate

    row = await ts.get(SequenceTemplate, template_id)
    if row is None or row.archived_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Template not found")
    row.name, row.description, row.steps = body.name.strip(), body.description.strip(), \
        _validated(body)
    await ts.flush()
    return _out(row)


@router.delete("/{template_id}", status_code=204, response_model=None)
async def archive_template(
    template_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_engagement)),
) -> None:
    from nexus.core.db import utcnow
    from nexus.models.engagement import SequenceTemplate

    row = await ts.get(SequenceTemplate, template_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Template not found")
    row.archived_at = row.archived_at or utcnow()
    await ts.flush()
```

Register both in `nexus/api/routers/__init__.py` beside `engagement_settings`. Note `response_model=None` on every 204 route: FastAPI otherwise infers a model from the `-> None` annotation under `from __future__ import annotations` and refuses to build the route.

- [ ] Run the three API tests; commit — `git commit -am "feat(engagement): campaigns and sequence templates API, dark until switched on"`

---

### Task 7: The whole test file

```python
"""Sequences and drafting: campaigns, enrolment, the review queue, the credit gate, and the worker
that writes each follow-up just before it sends (spec §7-§10, D4, D5, D17, D18, D19).

Drafts come from the real `MessagingAgent` on the offline stub model, and sends go to the
`SentFolder` provider double from `test_engagement_sending.py`, installed through the registry seam.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email import message_from_bytes as _parse
from email.policy import default as _modern

import pytest

from nexus.core.config import get_settings
from tests.conftest import auth, make_tenant, seed_relevance_profile, signup, tenant_session
from tests.test_engagement_sending import SentFolder

NOW = datetime(2026, 9, 22, 9, 0, tzinfo=UTC)   # a Tuesday


@pytest.fixture
def folder():
    from nexus.engagement.mailboxes import registry

    box = SentFolder()
    registry.set_provider_factory(lambda _connection: box)
    yield box
    registry.set_provider_factory(None)


@pytest.fixture
def engine_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "engagement_campaigns_enabled", True)


async def _world(slug: str, *, contacts: int = 2):
    """A workspace with product context, an SDR, a mailbox, and contacts at an account with a
    live signal — the specific fact every draft is expected to use."""
    from nexus.models.account import Account, Contact
    from nexus.models.engagement import MailboxConnection
    from nexus.models.identity import User
    from nexus.models.signal import SignalEvent

    tid = await make_tenant(slug=slug, name=slug.title())
    async with tenant_session(tid) as ts:
        await seed_relevance_profile(ts)
        user = User(email=f"sam@{slug}.com", full_name="Sam Rep", password_hash="x")
        ts.session.add(user)
        await ts.session.flush()
        mailbox = MailboxConnection(owner_user_id=user.id, provider="google",
                                    email=f"sam@{slug}.com", display_name="Sam Rep",
                                    status="connected", timezone="Europe/London")
        account = Account(name="Acme Robotics", domain="acme.io", country="Germany")
        ts.add(mailbox)
        ts.add(account)
        await ts.flush()
        ts.add(SignalEvent(account_id=account.id, kind="funding", source="web",
                           title="Acme Robotics raises $40M Series B", strength=0.9,
                           occurred_at=NOW - timedelta(days=3),
                           dedupe_key=f"{slug}-funding"))
        ids = []
        for i in range(contacts):
            contact = Contact(account_id=account.id, full_name=f"Jane{i} Buyer",
                              email=f"jane{i}@acme.io", title="VP Engineering")
            ts.add(contact)
            await ts.flush()
            ids.append(contact.id)
        return tid, user.id, mailbox.id, ids


STEPS = [
    {"channel": "email", "angle": "Open on the funding round"},
    {"channel": "email", "angle": "A different reason to talk", "delay_business_days": 2},
    {"channel": "email", "angle": "Break-up note", "delay_business_days": 3},
]


# ---- pure rules -----------------------------------------------------------------------------------

def test_steps_are_validated_before_anything_is_saved():
    from nexus.engagement.sequences.service import CampaignError, validate_steps

    clean = validate_steps(STEPS)
    assert clean[0]["delay_business_days"] == 0 and clean[1]["delay_business_days"] == 2
    assert clean[0]["allowed_weekdays"] == [0, 1, 2, 3, 4]
    for bad in ([], [{"channel": "call"}], [{"channel": "sms"}],
                [{"channel": "email"}, {"channel": "email", "timing_mode": "manual"}],
                [{"channel": "email"}, {"channel": "email", "delay_business_days": 99}]):
        with pytest.raises(CampaignError):
            validate_steps(bad)


def test_first_emails_wait_for_approval_and_follow_ups_follow_the_step_timing():
    from types import SimpleNamespace

    from nexus.engagement.sequences.timing import first_due_at, followup_due_at

    approved = datetime(2026, 9, 22, 8, 0, tzinfo=UTC)
    assert first_due_at(first_send_mode="on_approval", first_send_at=None,
                        approved_at=approved, now=NOW) == approved
    later = datetime(2026, 9, 24, 7, 0, tzinfo=UTC)
    assert first_due_at(first_send_mode="scheduled", first_send_at=later,
                        approved_at=approved, now=NOW) == later
    # A scheduled time never goes before the approval: scheduling does not approve.
    assert first_due_at(first_send_mode="scheduled", first_send_at=approved - timedelta(days=2),
                        approved_at=approved, now=NOW) == approved

    friday = datetime(2026, 9, 25, 14, 30, tzinfo=UTC)
    auto = SimpleNamespace(timing_mode="auto", delay_business_days=1)
    assert followup_due_at(step=auto, previous_sent_at=friday, zone_name="UTC") == \
        datetime(2026, 9, 28, 14, 30, tzinfo=UTC)          # Monday, same time of day
    manual = SimpleNamespace(timing_mode="manual", delay_business_days=1,
                             send_time_local="09:15", allowed_weekdays=[2])
    assert followup_due_at(step=manual, previous_sent_at=friday, zone_name="UTC") == \
        datetime(2026, 9, 30, 9, 15, tzinfo=UTC)           # next Wednesday at 09:15


def test_a_draft_must_use_a_specific_fact_and_naming_the_company_does_not_count():
    from nexus.agents.email_quality import check_draft
    from nexus.engagement.drafting.personalisation import PROBLEM, uses_a_fact

    facts = ["Acme Robotics raises $40M Series B", "VP Engineering"]
    assert uses_a_fact("Congrats on the $40M raise.", facts, company_name="Acme Robotics")
    assert uses_a_fact("Saw the Series B news.", facts, company_name="Acme Robotics")
    assert not uses_a_fact("Hi Jane, teams at Acme Robotics love us.", facts,
                           company_name="Acme Robotics")
    assert uses_a_fact("anything at all", [], company_name="")
    body = "Hi Jane,\n\nTeams at Acme Robotics love us. Worth a chat?\n\nBest,\nSam"
    assert PROBLEM in check_draft(subject="Hi", body=body, first_name="Jane", facts=facts,
                                  company_name="Acme Robotics")
    # Without facts the check is exactly what it was for every other caller.
    assert PROBLEM not in check_draft(subject="Hi", body=body, first_name="Jane")


# ---- campaigns, enrolment, drafts -----------------------------------------------------------------

async def test_enrolment_skips_who_cannot_be_emailed_and_warns_about_duplicate_outreach():
    from nexus.engagement.sequences.service import create_campaign, enroll
    from nexus.engagement.suppression.service import suppress
    from nexus.models.account import Contact

    tid, user_id, mailbox_id, contact_ids = await _world("enrol", contacts=3)
    async with tenant_session(tid) as ts:
        no_email = await ts.get(Contact, contact_ids[1])
        no_email.email = ""
        blocked = await ts.get(Contact, contact_ids[2])
        await suppress(ts, email=blocked.email, reason="declined")
        first = await create_campaign(ts, name="Q4", owner_user_id=user_id,
                                      mailbox_id=mailbox_id, steps=STEPS)
        result = await enroll(ts, first, contact_ids + [contact_ids[0]])
        assert result.added == [contact_ids[0]]
        reasons = {s["contact_id"]: s["reason"] for s in result.skipped}
        assert reasons[contact_ids[1]] == "no_email"
        assert reasons[contact_ids[2]] == "do_not_contact:declined"

        second = await create_campaign(ts, name="Webinar follow-up", owner_user_id=user_id,
                                       mailbox_id=mailbox_id, steps=STEPS[:1])
        again = await enroll(ts, second, [contact_ids[0]])
        assert again.added == [contact_ids[0]]
        assert again.warnings[0]["campaign_name"] == "Q4"
        assert again.warnings[0]["owner"] == "Sam Rep"


async def test_the_recipients_timezone_comes_from_their_account_when_they_have_none():
    from nexus.engagement.sequences.service import create_campaign, enroll
    from nexus.models.engagement import EngagementEnrollment

    tid, user_id, mailbox_id, contact_ids = await _world("tz", contacts=1)
    async with tenant_session(tid) as ts:
        campaign = await create_campaign(ts, name="TZ", owner_user_id=user_id,
                                         mailbox_id=mailbox_id, steps=STEPS)
        await enroll(ts, campaign, contact_ids)
        enrollment = await ts.first(EngagementEnrollment)
        assert enrollment.contact_timezone == "Europe/Berlin"


async def test_first_emails_are_drafted_once_personalised_and_waiting_for_review():
    from nexus.engagement.sequences.service import (
        create_campaign,
        draft_first_emails,
        enroll,
        review_queue,
    )

    tid, user_id, mailbox_id, contact_ids = await _world("draft")
    async with tenant_session(tid) as ts:
        campaign = await create_campaign(ts, name="Q4", owner_user_id=user_id,
                                         mailbox_id=mailbox_id, steps=STEPS)
        await enroll(ts, campaign, contact_ids)
        result = await draft_first_emails(ts, campaign, user_id=user_id)
        assert result == {"drafted": 2, "failed": 0, "errors": []}
        assert campaign.status == "reviewing"
        queue = await review_queue(ts, campaign)
        assert len(queue) == 2
        _enrollment, message = queue[0]
        assert message.status == "draft" and message.step_index == 0
        assert "Series B" in message.body_text, "the stub opens on the live signal"
        assert message.quality_problems == []
        assert message.ai_body == message.body_text
        # Idempotent: nothing is drafted twice.
        assert (await draft_first_emails(ts, campaign))["drafted"] == 0


async def test_an_edited_approval_is_recorded_against_the_ai_text():
    from nexus.engagement.ledger import consent
    from nexus.engagement.sequences.service import (
        approve,
        create_campaign,
        draft_first_emails,
        enroll,
        review_queue,
    )
    from nexus.models.ledger import LedgerOutbox

    tid, user_id, mailbox_id, contact_ids = await _world("edit", contacts=1)
    async with tenant_session(tid) as ts:
        await consent.record(ts, status_value="on", source="settings", user_id=None)
        campaign = await create_campaign(ts, name="Q4", owner_user_id=user_id,
                                         mailbox_id=mailbox_id, steps=STEPS)
        await enroll(ts, campaign, contact_ids)
        await draft_first_emails(ts, campaign, user_id=user_id)
        (_enrollment, message), = await review_queue(ts, campaign)
        await approve(ts, message, user_id=user_id, body=message.body_text + "\n\nP.S. Congrats.")
        assert message.status == "approved" and message.approved_by_user_id == user_id
        events = {e.event_type: e for e in await ts.list(LedgerOutbox)}
    assert {"draft.created", "draft.edited", "draft.approved"} <= set(events)
    edited = events["draft.edited"].payload["payload"]
    assert edited["ai_body"] != edited["body"] and edited["edit_distance"] == 2


# ---- the gate --------------------------------------------------------------------------------------

async def test_launch_is_refused_when_the_worst_case_cannot_be_covered(monkeypatch):
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.credits import grant_credits
    from nexus.billing.rates import sync_rates
    from nexus.engagement.sequences.service import (
        CreditGateRefused,
        approve_all_passing,
        create_campaign,
        draft_first_emails,
        enroll,
        launch,
    )

    await sync_catalog()
    await sync_rates()
    monkeypatch.setattr(get_settings(), "billing_enforcement", "shadow")
    tid, user_id, mailbox_id, contact_ids = await _world("gate")
    async with tenant_session(tid) as ts:
        campaign = await create_campaign(ts, name="Q4", owner_user_id=user_id,
                                         mailbox_id=mailbox_id, steps=STEPS)
        await enroll(ts, campaign, contact_ids)
        await draft_first_emails(ts, campaign, user_id=user_id)
        await approve_all_passing(ts, campaign, user_id=user_id)
        with pytest.raises(CreditGateRefused) as refused:
            await launch(ts, campaign, user_id=user_id)
        estimate = refused.value.estimate
        # 2 contacts x 3 email steps x (draft 2 + send 1) = 18 credits in the worst case.
        assert estimate.worst_credits == 18 and estimate.gate_applies
        assert estimate.likely_credits < estimate.worst_credits
        await grant_credits(ts, 500, idempotency_key="gate-topup", reason="test")
        result = await launch(ts, campaign, user_id=user_id)
        assert result.covered and campaign.status == "active"


async def test_the_gate_is_skipped_when_billing_is_off(monkeypatch):
    from nexus.engagement.sequences.estimate import estimate
    from nexus.engagement.sequences.service import create_campaign, enroll

    monkeypatch.setattr(get_settings(), "billing_enforcement", "off")
    tid, user_id, mailbox_id, contact_ids = await _world("gateoff")
    async with tenant_session(tid) as ts:
        campaign = await create_campaign(ts, name="Q4", owner_user_id=user_id,
                                         mailbox_id=mailbox_id, steps=STEPS)
        await enroll(ts, campaign, contact_ids)
        result = await estimate(ts, campaign)
    assert result.covered and not result.gate_applies


# ---- the worker ------------------------------------------------------------------------------------

async def _launched(slug: str, monkeypatch, **campaign_options):
    from nexus.engagement.sequences.service import (
        approve_all_passing,
        create_campaign,
        draft_first_emails,
        enroll,
        launch,
    )

    monkeypatch.setattr(get_settings(), "billing_enforcement", "off")
    tid, user_id, mailbox_id, contact_ids = await _world(slug, contacts=1)
    async with tenant_session(tid) as ts:
        campaign = await create_campaign(ts, name="Q4", owner_user_id=user_id,
                                         mailbox_id=mailbox_id, steps=STEPS, **campaign_options)
        await enroll(ts, campaign, contact_ids)
        await draft_first_emails(ts, campaign, user_id=user_id)
        await approve_all_passing(ts, campaign, user_id=user_id)
        await launch(ts, campaign, user_id=user_id)
        return tid, campaign.id


async def _enrollment(tid):
    from nexus.models.engagement import EngagementEnrollment

    async with tenant_session(tid) as ts:
        return await ts.first(EngagementEnrollment)


async def _run(tid, enrollment_id, when):
    from nexus.engagement.sequences.advance import process

    async with tenant_session(tid) as ts:
        return await process(ts, enrollment_id, now=when)


async def test_a_launched_sequence_sends_threads_its_follow_ups_and_completes(folder, monkeypatch):
    from nexus.models.engagement import EngagementMessage

    tid, _campaign_id = await _launched("seq", monkeypatch)
    enrollment = await _enrollment(tid)
    assert enrollment.status == "active" and enrollment.next_action_at is not None

    assert await _run(tid, enrollment.id, enrollment.next_action_at + timedelta(seconds=1)) \
        == "sent"
    enrollment = await _enrollment(tid)
    assert enrollment.current_step_index == 1 and enrollment.current_thread_id
    first = _parse(folder.delivered[0], policy=_modern)

    # Not due yet: nothing happens.
    assert await _run(tid, enrollment.id, enrollment.next_action_at - timedelta(hours=1)) \
        == "not_due"
    assert await _run(tid, enrollment.id, enrollment.next_action_at + timedelta(seconds=1)) \
        == "sent"
    second = _parse(folder.delivered[1], policy=_modern)
    # The follow-up is written just in time, in the same thread, with exactly one "Re:" (D16).
    assert second["Subject"] == f"Re: {first['Subject']}"
    assert second["In-Reply-To"] == first["Message-ID"]

    enrollment = await _enrollment(tid)
    assert await _run(tid, enrollment.id, enrollment.next_action_at + timedelta(seconds=1)) \
        == "sent_completed"
    enrollment = await _enrollment(tid)
    assert enrollment.status == "completed" and enrollment.next_action_at is None
    async with tenant_session(tid) as ts:
        sent = await ts.list(EngagementMessage, EngagementMessage.status == "sent")
    assert sorted(m.step_index for m in sent) == [0, 1, 2]
    assert len(folder.delivered) == 3


async def test_review_every_touch_holds_each_follow_up_for_the_sdr(folder, monkeypatch):
    tid, _ = await _launched("everytouch", monkeypatch, review_every_touch=True)
    enrollment = await _enrollment(tid)
    await _run(tid, enrollment.id, enrollment.next_action_at + timedelta(seconds=1))
    enrollment = await _enrollment(tid)
    assert await _run(tid, enrollment.id, enrollment.next_action_at + timedelta(seconds=1)) \
        == "awaiting_review"
    assert (await _enrollment(tid)).status == "awaiting_review"
    assert len(folder.delivered) == 1


async def test_a_reply_or_unsubscribe_before_the_step_stops_the_sequence(folder, monkeypatch):
    from nexus.engagement.suppression.service import suppress
    from nexus.models.account import Contact

    tid, _ = await _launched("unsub", monkeypatch)
    enrollment = await _enrollment(tid)
    async with tenant_session(tid) as ts:
        contact = await ts.get(Contact, enrollment.contact_id)
        await suppress(ts, email=contact.email, reason="unsubscribed")
    assert await _run(tid, enrollment.id, enrollment.next_action_at + timedelta(seconds=1)) \
        == "stopped:unsubscribed"
    enrollment = await _enrollment(tid)
    assert enrollment.status == "stopped" and enrollment.status_reason == "unsubscribed"
    assert folder.delivered == []


async def test_a_disconnected_mailbox_pauses_rather_than_failing(folder, monkeypatch):
    from nexus.models.engagement import MailboxConnection

    tid, _ = await _launched("disc", monkeypatch)
    enrollment = await _enrollment(tid)
    async with tenant_session(tid) as ts:
        mailbox = await ts.get(MailboxConnection, enrollment.mailbox_connection_id)
        mailbox.status = "needs_reauth"
    assert await _run(tid, enrollment.id, enrollment.next_action_at + timedelta(seconds=1)) \
        == "mailbox_disconnected"
    enrollment = await _enrollment(tid)
    assert (enrollment.status, enrollment.status_reason) == ("paused", "mailbox_disconnected")


async def test_the_heartbeat_finds_due_work_only_in_running_campaigns(folder, monkeypatch):
    from nexus.engagement.sequences.advance import due_enrollments
    from nexus.engagement.sequences.service import pause_campaign
    from nexus.models.engagement import EngagementCampaign

    tid, campaign_id = await _launched("due", monkeypatch)
    enrollment = await _enrollment(tid)
    later = enrollment.next_action_at + timedelta(seconds=1)
    assert (tid, enrollment.id) in await due_enrollments(later)
    async with tenant_session(tid) as ts:
        await pause_campaign(ts, await ts.get(EngagementCampaign, campaign_id))
    assert (tid, enrollment.id) not in await due_enrollments(later)


async def test_the_worker_job_is_dark_until_the_switch_is_on(monkeypatch):
    from nexus.workers.queue import InMemoryTaskQueue
    from nexus.workers.scheduler import _enqueue_due
    from nexus.workers.tasks import HANDLERS

    monkeypatch.setattr(get_settings(), "engagement_campaigns_enabled", False)
    assert "skipped" in await HANDLERS["advance_engagement"]({})
    queue = InMemoryTaskQueue()
    await _enqueue_due(queue)
    names = []
    while (job := await queue.dequeue(timeout=0)) is not None:
        names.append(job.name)
    assert "advance_engagement" not in names

    monkeypatch.setattr(get_settings(), "engagement_campaigns_enabled", True)
    queue = InMemoryTaskQueue()
    await _enqueue_due(queue)
    names = []
    while (job := await queue.dequeue(timeout=0)) is not None:
        names.append(job.name)
    assert "advance_engagement" in names


# ---- the API ---------------------------------------------------------------------------------------

async def test_the_campaign_api_is_invisible_while_the_engine_is_dark(client):
    token = await signup(client, slug="darkapi", email="owner@darkapi.com", company="Dark")
    assert (await client.get("/api/engagement/campaigns", headers=auth(token))).status_code == 404
    assert (await client.get("/api/engagement/templates", headers=auth(token))).status_code == 404


async def test_build_draft_review_and_launch_through_the_api(client, engine_on, monkeypatch):
    from nexus.models.account import Account, Contact
    from nexus.models.engagement import MailboxConnection
    from nexus.models.signal import SignalEvent
    from tests.conftest import principal_from_token

    monkeypatch.setattr(get_settings(), "billing_enforcement", "off")
    token = await signup(client, slug="flowapi", email="sam@flowapi.com", company="Flow")
    me = principal_from_token(token)
    async with tenant_session(me.tenant_id) as ts:
        await seed_relevance_profile(ts)
        mailbox = MailboxConnection(owner_user_id=me.user_id, provider="google",
                                    email="sam@flowapi.com", status="connected",
                                    timezone="Europe/London")
        account = Account(name="Acme Robotics", domain="acme.io")
        ts.add(mailbox)
        ts.add(account)
        await ts.flush()
        ts.add(SignalEvent(account_id=account.id, kind="funding", source="web",
                           title="Acme Robotics raises $40M Series B", strength=0.9,
                           occurred_at=NOW, dedupe_key="flowapi-funding"))
        contact = Contact(account_id=account.id, full_name="Jane Buyer", email="jane@acme.io")
        ts.add(contact)
        await ts.flush()
        mailbox_id, contact_id = mailbox.id, contact.id

    template = await client.post("/api/engagement/templates", headers=auth(token), json={
        "name": "Three touches", "steps": STEPS})
    assert template.status_code == 201, template.text
    created = await client.post("/api/engagement/campaigns", headers=auth(token), json={
        "name": "Q4", "mailbox_id": mailbox_id, "template_id": template.json()["id"]})
    assert created.status_code == 201, created.text
    campaign = created.json()
    assert [s["step_index"] for s in campaign["steps"]] == [0, 1, 2]

    added = await client.post(f"/api/engagement/campaigns/{campaign['id']}/contacts",
                              headers=auth(token), json={"contact_ids": [contact_id]})
    assert added.json()["added"] == [contact_id]
    drafted = await client.post(f"/api/engagement/campaigns/{campaign['id']}/draft",
                                headers=auth(token))
    assert drafted.json()["drafted"] == 1
    review = (await client.get(f"/api/engagement/campaigns/{campaign['id']}/review",
                               headers=auth(token))).json()
    assert review[0]["status"] == "draft" and review[0]["quality_problems"] == []
    approve = await client.post(f"/api/engagement/messages/{review[0]['message_id']}/approve",
                                headers=auth(token), json={})
    assert approve.status_code == 204, approve.text
    estimate = (await client.get(f"/api/engagement/campaigns/{campaign['id']}/estimate",
                                 headers=auth(token))).json()
    assert estimate["contacts"] == 1 and estimate["email_steps"] == 3
    launched = await client.post(f"/api/engagement/campaigns/{campaign['id']}/launch",
                                 headers=auth(token))
    assert launched.status_code == 200, launched.text
    assert launched.json()["status"] == "active"
    enrollments = (await client.get(f"/api/engagement/campaigns/{campaign['id']}/enrollments",
                                    headers=auth(token))).json()
    assert enrollments[0]["status"] == "active" and enrollments[0]["next_action_at"]


async def test_a_colleague_cannot_see_or_steer_another_reps_campaign(client, engine_on):
    from nexus.engagement.sequences.service import create_campaign
    from nexus.models.engagement import MailboxConnection
    from nexus.models.identity import User

    token = await signup(client, slug="colleague", email="rep@colleague.com", company="Col")
    from tests.conftest import principal_from_token

    me = principal_from_token(token)
    async with tenant_session(me.tenant_id) as ts:
        other = User(email="other@colleague.com", full_name="Other Rep", password_hash="x")
        ts.session.add(other)
        await ts.session.flush()
        mailbox = MailboxConnection(owner_user_id=other.id, provider="google",
                                    email="other@colleague.com", status="connected")
        ts.add(mailbox)
        await ts.flush()
        campaign = await create_campaign(ts, name="Theirs", owner_user_id=other.id,
                                         mailbox_id=mailbox.id, steps=STEPS[:1])
        campaign_id = campaign.id
    # The signer-up is the workspace owner, a manager: they CAN see it. A rep could not; the
    # service-level rule is that the owner or a manager acts on a campaign.
    seen = await client.get(f"/api/engagement/campaigns/{campaign_id}", headers=auth(token))
    assert seen.status_code == 200
    mine = (await client.get("/api/engagement/campaigns", headers=auth(token))).json()
    assert all(c["id"] != campaign_id for c in mine), "the default list is your own campaigns"
```

RUN_RESULT:

```
18 passed in 190.64s    # tests/test_engagement_sequences.py
151 passed in 788.41s   # drafting, quality, sending, scheduler, metering coverage, route guards
All checks passed!      # ruff check nexus tests tests_live
```

---

## What later phases depend on

- **Phase 09** sets `snoozed` + `snoozed_until` for "later, with a date" replies and `paused` with `colleague_replied` / `out_of_office`; `advance.process` already sends the one re-engagement at `snoozed_until` and resumes the remaining steps.
- **Phase 10** drafts responses through `drafting.drafter.draft(kind="response")` and sends them through `sending.service.send(kind="response")`.
- **Phase 11** builds the screens on this API; **phase 12** reads `enrollment.*` and `message.sent` events and the enrollment rows for reporting.
