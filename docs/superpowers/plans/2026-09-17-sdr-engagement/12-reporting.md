# Phase 12: Reporting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A manager can see what a campaign achieved and how fast the team answers; an SDR starts the day from one ordered list; a mailbox that bounces too much says so; and the dashboards and account tiering that read `outcomes` keep working on the new engine.

**Architecture:** `nexus/engagement/reports/` holds four modules, each answering one question: `service.py` (a campaign's results; response times), `today.py` (the Today plan), `health.py` (seven-day bounce rate) and `outcomes.py` (engagement activity written as `Outcome` rows). One router, `engagement_reports.py`, serves the first three; mailbox health rides on the existing My mailboxes rows. The screens add a Results tab to the campaign page, a Today card to the dashboard, a response-time readout to Replies, and a health line to each mailbox.

**Tech Stack:** FastAPI, async SQLAlchemy, React + TypeScript, CSS Modules.

**Roadmap:** [00-roadmap.md](00-roadmap.md). **Spec:** §11, §13 (Outcome rows unchanged), §19 (Today, reply speed, mailbox health). **Depends on:** phases 09, 11.

**Verified:** implemented on `feat/sdr-engagement` on top of phases 01–11. In the CI image: `tests/test_engagement_reporting.py` (8 passed: the bounce-rate rule; the funnel counted in people, with the reply credited to the step that drew it; one `sent`, one `replied` and one `meeting` outcome however many times a person wrote; the outcomes API taking an engagement campaign; response time in business hours across a weekend; the Today order; the bounce rate on My mailboxes; the reports dark with the engine), the four new structural checks in `tests/test_engagement_screens_ui.py`, and every engagement, tiering, outcome, analytics, rep-dashboard, metering-coverage and plan-gated-nav test (323 passed together), `ruff`, `npm run typecheck`, `npm run build`. In the browser against the isolated preview: Today listed the waiting buyers first and the opening emails to approve after them; a campaign's Results showed the funnel and what the replies said; Replies showed the response-time line; My mailboxes showed the week's bounce rate; no horizontal overflow.

---

## Decisions this phase makes

- **Everything is counted in people, not messages.** A buyer who writes three times is one reply to the campaign; counting messages lets a talkative prospect make a weak campaign look good. The one per-message figure is "sent" per step, because a step goes to each person once.
- **A reply belongs to the last step sent before it.** Crediting the first step would give the opening email every reply a follow-up earned, which is the question the per-step table exists to answer.
- **Attribution follows the thread as well as the enrollment.** A reply attaches only to a *live* enrollment (phase 09), so a second reply after the first stopped the sequence carries none. The thread still records the enrollment that started it, and the report reads that.
- **Response time is in business hours in the mailbox's zone**, the clock the reply-speed reminder runs on: a reply that arrived on Friday evening and was answered on Monday morning was answered promptly. Median with the 90th percentile beside it, because one slow week hides inside an average.
- **Outcomes: one `sent` per campaign touch, one `replied` per person, one `meeting` per booking.** Matching the old engine's per-touch `sent` keeps the funnel continuous across the switch. One-off sends and desk answers are not campaign touches. An out-of-office is not the person replying, and does not make the real reply look like a second one. The new campaign travels in `meta.engagement_campaign_id`; `Outcome.campaign_id` stays the old table's foreign key (§13). Each write runs in a savepoint and never raises: a failed outcome row must not roll back the send it describes.
- **Today is ordered by the cost of leaving each thing**: answers first, then decisions, colleagues, calls, opening emails to approve, and people returning today. Within a kind, the longest-waiting first. Ranking by reply likelihood is phase 13's; age is the honest order until then.
- **The bounce warning needs 20 sends to mean anything.** One bounce in five is 20% and says nothing. Above 3% of at least 20, the mailbox warns; it never blocks, like the volume warning (D10).
- **The funnel is one series in one hue.** No legend; every bar carries its count and its share. Bounces are a problem, not a stage, so they sit beside the funnel with a status icon and words. The accent was run through the palette validator against both theme surfaces (passes). A table carries the per-step numbers.

---

## Files

| Action | Path | Responsibility |
|---|---|---|
| Create | `nexus/engagement/reports/__init__.py`, `service.py`, `today.py`, `health.py`, `outcomes.py` | the four questions |
| Create | `nexus/api/routers/engagement_reports.py` | `/engagement/reports/...`, `/engagement/today` |
| Modify | `nexus/api/routers/__init__.py` | register it |
| Modify | `nexus/api/routers/engagement_mailboxes.py` | health on each mailbox row |
| Modify | `nexus/engagement/sending/service.py`, `replies/ingest.py`, `desk/service.py` | write the outcomes |
| Modify | `nexus/api/schemas.py`, `nexus/api/routers/outcomes.py` | `engagement_campaign_id` on the outcomes API |
| Modify | `frontend/src/lib/types.ts`, `frontend/src/lib/api.ts` | reporting types and client |
| Create | `frontend/src/pages/engagement/ReportsPanel.tsx` (+ CSS), `frontend/src/components/engagement/TodayPlan.tsx`, `MailboxHealth.tsx`, `ResponseTimes.tsx` (+ CSS) | the screens |
| Modify | `CampaignDetailPage.tsx`, `DashboardPage.tsx`, `MailboxesPage.tsx`, `ReplyDeskPage.tsx` | where they appear |
| Create/Modify | `tests/test_engagement_reporting.py`, `tests/test_engagement_screens_ui.py`, `tests/test_engagement_desk.py` | tests |

---

### Task 1: A campaign's results and response times

**Files:** Create `nexus/engagement/reports/__init__.py`, `nexus/engagement/reports/service.py`.

- [ ] **Step 1: Write the failing tests** — create `tests/test_engagement_reporting.py` from Task 6. Run `pytest tests/test_engagement_reporting.py -n0 -q -k "funnel or response_time"` — expected FAIL: `No module named 'nexus.engagement.reports'`.

- [ ] **Step 2: Implement** `nexus/engagement/reports/__init__.py`:

```python
"""Engagement reporting: campaign results, response times, the Today plan, mailbox health (§11, §19)."""
```

and `nexus/engagement/reports/service.py`:

```python
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
```

- [ ] **Step 3: Run** the same selection — expected PASS.

---

### Task 2: Outcomes for the dashboards

**Files:** Create `nexus/engagement/reports/outcomes.py`; modify the sender, ingestion, the desk, the outcomes API.

- [ ] **Step 1: Implement** `nexus/engagement/reports/outcomes.py`:

```python
"""Engagement activity as `Outcome` rows, so the dashboards and account tiering keep working (§11).

The attribution dashboards, the ROI rollup and `tiering.classify` all read `outcomes`. The old
engine wrote a `sent` outcome per touch; without the same here, a workspace that moved to the new
engine would watch its funnel go flat while it sent more than ever.

* **sent** — every campaign step or re-engagement that left, like the old engine's per-touch row.
  One-off sends and desk answers are not campaign touches and are not counted.
* **replied** — once per person per campaign (their first human reply), or once per thread for a
  reply to a one-off email. An out-of-office is not a reply from the person.
* **meeting** — written by the reply desk's "Meeting booked", with the same attribution.

`Outcome.campaign_id` is the OLD campaigns table's foreign key and stays so (spec §13); the new
campaign travels in `meta.engagement_campaign_id` beside `enrollment_id` and `message_id`.

**Never raises, and never takes the caller down with it.** Each write runs in a savepoint: a
failed outcome row must not roll back the send it describes, which would turn "the email left" into
"the email left and we have no record of it".
"""
from __future__ import annotations

import logging

logger = logging.getLogger("nexus.engagement.reports.outcomes")

COUNTED_SEND_KINDS = ("step", "reengage")


def attribution(enrollment, message=None) -> dict:
    meta = {"source": "engagement"}
    if enrollment is not None:
        meta["engagement_campaign_id"] = enrollment.campaign_id
        meta["enrollment_id"] = enrollment.id
    if message is not None:
        meta["message_id"] = message.id
        if message.step_index is not None:
            meta["step_index"] = message.step_index
    return meta


async def _record(ts, stage: str, *, account_id, contact_id, meta: dict) -> None:
    from nexus.outcomes.service import get_outcome_service

    try:
        async with ts.session.begin_nested():
            await get_outcome_service().record(ts, stage=stage, account_id=account_id,
                                               contact_id=contact_id, meta=meta)
    except Exception:
        logger.warning("could not record the %s outcome", stage, exc_info=True)


async def record_sent(ts, message, enrollment) -> None:
    if enrollment is None or message.kind not in COUNTED_SEND_KINDS:
        return
    await _record(ts, "sent", account_id=enrollment.account_id, contact_id=message.contact_id,
                  meta=attribution(enrollment, message))


async def record_replied(ts, message, classification) -> None:
    """The person's first human reply to this campaign (or to this thread, for a one-off)."""
    from sqlalchemy import func, select

    from nexus.models.engagement import (
        EngagementEnrollment,
        EngagementMessage,
        ReplyClassification,
    )

    if classification.category == "out_of_office" or message.inbound_kind != "human":
        return
    scope = (EngagementMessage.enrollment_id == message.enrollment_id
             if message.enrollment_id else EngagementMessage.thread_id == message.thread_id)
    # An earlier out-of-office was not them replying, so it does not make this the second reply.
    earlier = (await ts.session.execute(
        select(func.count()).select_from(EngagementMessage)
        .join(ReplyClassification, ReplyClassification.message_id == EngagementMessage.id)
        .where(EngagementMessage.tenant_id == ts.tenant_id)
        .where(scope)
        .where(EngagementMessage.direction == "in")
        .where(EngagementMessage.inbound_kind == "human")
        .where(EngagementMessage.id != message.id)
        .where(ReplyClassification.category != "out_of_office"))).scalar_one()
    if earlier:
        return
    enrollment = await ts.get(EngagementEnrollment, message.enrollment_id) \
        if message.enrollment_id else None
    await _record(ts, "replied", account_id=classification.account_id,
                  contact_id=message.contact_id, meta=attribution(enrollment, message))
```

- [ ] **Step 2: Write them where the events happen.** `nexus/engagement/sending/service.py`:

```diff
diff --git a/nexus/engagement/sending/service.py b/nexus/engagement/sending/service.py
index c6ad84d..49d85f0 100644
--- a/nexus/engagement/sending/service.py
+++ b/nexus/engagement/sending/service.py
@@ -400,4 +400,8 @@ async def _record(ts, row, *, mailbox, contact, enrollment, user_id, context: di
     }
     payload.update(context or {})
+    from nexus.engagement.reports.outcomes import record_sent
+
+    # The dashboards, the ROI rollup and account tiering read `outcomes` (spec §11).
+    await record_sent(ts, row, enrollment)
     await emit(ts, "message.sent", actor_user_id=user_id,
                refs={"contact_id": contact.id, "account_id": getattr(contact, "account_id", None),
```

`nexus/engagement/replies/ingest.py`:

```diff
diff --git a/nexus/engagement/replies/ingest.py b/nexus/engagement/replies/ingest.py
index d506145..f803e0a 100644
--- a/nexus/engagement/replies/ingest.py
+++ b/nexus/engagement/replies/ingest.py
@@ -187,4 +187,7 @@ async def _classify_and_act(ts, row, found, mailbox, parsed) -> str:
     classification.action_taken = action[:40]
     await ts.flush()
+    from nexus.engagement.reports.outcomes import record_replied
+
+    await record_replied(ts, row, classification)
     await emit(ts, "reply.classified", refs=_refs(row, found, mailbox, classification),
                payload={"category": verdict.category, "confidence": verdict.confidence,
```

`nexus/engagement/desk/service.py` (the meeting gains the same attribution):

```diff
diff --git a/nexus/engagement/desk/service.py b/nexus/engagement/desk/service.py
index c634456..a06363e 100644
--- a/nexus/engagement/desk/service.py
+++ b/nexus/engagement/desk/service.py
@@ -266,8 +266,12 @@ async def decide(ts, classification, decision: str, *, user_id: str,
                 await set_status(ts, enrollment, "stopped", "manual", user_id=user_id)
     elif decision == "meeting":
+        from nexus.engagement.reports.outcomes import attribution
+
+        enrollment = await ts.get(EngagementEnrollment, classification.enrollment_id)             if classification.enrollment_id else None
         await get_outcome_service().record(
             ts, stage="meeting", account_id=classification.account_id,
             contact_id=classification.contact_id,
-            meta={"source": "engagement_reply_desk", "message_id": classification.message_id,
+            meta={**attribution(enrollment), "source": "engagement_reply_desk",
+                  "message_id": classification.message_id,
                   "classification_id": classification.id})
         for enrollment in enrollments:
```

- [ ] **Step 3: The outcomes API takes an engagement campaign.** `nexus/api/schemas.py`:

```diff
diff --git a/nexus/api/schemas.py b/nexus/api/schemas.py
index 0054b44..be3e1b9 100644
--- a/nexus/api/schemas.py
+++ b/nexus/api/schemas.py
@@ -548,4 +548,7 @@ class OutcomeIn(BaseModel):
     contact_id: str | None = None
     campaign_id: str | None = None  # attribute this outcome to the campaign that drove it
+    # A campaign on the engagement engine. `campaign_id` is the old campaigns table's foreign key and
+    # stays so (spec §13), so the new one is validated and carried in `meta` instead.
+    engagement_campaign_id: str | None = None
     meta: dict = Field(default_factory=dict)
 
```

`nexus/api/routers/outcomes.py`:

```diff
diff --git a/nexus/api/routers/outcomes.py b/nexus/api/routers/outcomes.py
index ea3d0ce..bb3c448 100644
--- a/nexus/api/routers/outcomes.py
+++ b/nexus/api/routers/outcomes.py
@@ -59,4 +59,11 @@ async def record_outcome(
     if body.campaign_id is not None and await ts.get(Campaign, body.campaign_id) is None:
         raise HTTPException(status.HTTP_404_NOT_FOUND, "Campaign not found")
+    meta = dict(body.meta)
+    if body.engagement_campaign_id is not None:
+        from nexus.models.engagement import EngagementCampaign
+
+        if await ts.get(EngagementCampaign, body.engagement_campaign_id) is None:
+            raise HTTPException(status.HTTP_404_NOT_FOUND, "Campaign not found")
+        meta["engagement_campaign_id"] = body.engagement_campaign_id
     outcome = await get_outcome_service().record(
         ts,
@@ -66,5 +73,5 @@ async def record_outcome(
         contact_id=body.contact_id,
         campaign_id=body.campaign_id,
-        meta=body.meta,
+        meta=meta,
     )
     return _outcome_out(outcome)
```

- [ ] **Step 4: One existing test counted every outcome.** `test_a_meeting_is_recorded_as_an_outcome_the_dashboards_can_see` asserted the meeting was the only row; sends and replies now write their own. Apply to `tests/test_engagement_desk.py`:

```diff
diff --git a/tests/test_engagement_desk.py b/tests/test_engagement_desk.py
index 8f886ab..971fb47 100644
--- a/tests/test_engagement_desk.py
+++ b/tests/test_engagement_desk.py
@@ -303,7 +303,9 @@ async def test_a_meeting_is_recorded_as_an_outcome_the_dashboards_can_see(
         classification = await _fetch(ts, classification_id)
         await decide(ts, classification, "meeting", user_id=owner)
-        outcomes = await ts.list(Outcome)
-    assert [o.stage for o in outcomes] == ["meeting"]
-    assert outcomes[0].meta["source"] == "engagement_reply_desk"
+        meetings = await ts.list(Outcome, Outcome.stage == "meeting")
+    # Sends and replies write their own outcomes (phase 12); this is about the meeting.
+    assert len(meetings) == 1
+    assert meetings[0].meta["source"] == "engagement_reply_desk"
+    assert meetings[0].meta["engagement_campaign_id"]
     assert (await _enrollment(tid)).status == "stopped"
 
```

- [ ] **Step 5: Run** `pytest tests/test_engagement_reporting.py tests/test_engagement_desk.py tests/test_outcomes.py tests/test_refresh_tiering.py -n0 -q` — expected PASS.

---

### Task 3: Today and mailbox health

**Files:** Create `nexus/engagement/reports/today.py`, `health.py`; modify `engagement_mailboxes.py`.

- [ ] **Step 1: Implement** `nexus/engagement/reports/today.py`. `call_tasks.due_at` is a plain timezone column that SQLite returns naive, hence `_aware`.

```python
"""Today: where an SDR starts (spec §19, "Where do I start?").

One ordered list per SDR, built from what the engine already knows. The order is the order of cost
if left: a buyer who said yes and is waiting goes cold fastest, then replies only a person can
decide, then colleagues a reply paused, then calls due, then opening emails waiting for approval,
then the people coming back today, who need nothing but are worth knowing about.

Within a kind, the longest-waiting first. Ranking by reply likelihood is phase 13's (insights); until
then age is the honest order, and it is also what the reply-speed reminder measures.

Scoped to the caller's own mailboxes and campaigns: this is a personal list, and a manager's team
view is the reply desk's toggle.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

#: The order kinds appear in, and what each one asks of the SDR.
KINDS = ("reply", "decide", "colleagues", "call", "review", "returning")


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

    def as_dict(self) -> dict:
        return asdict(self)


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
                                   f"/engagement/replies?reply={r.id}", arrived(r)))
        else:
            said = {"interested": "They're interested.", "question": "They asked a question.",
                    "referral": "They pointed you to someone else."}.get(category, "They replied.")
            items.append(TodayItem("reply", f"Answer {who(r.contact_id)}", said,
                                   f"/engagement/replies?reply={r.id}", arrived(r)))

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
                               "/calls", c.due_at))

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
                               e.snoozed_until))

    order = {kind: i for i, kind in enumerate(KINDS)}
    return sorted(items, key=lambda i: order[i.kind])
```

- [ ] **Step 2: Implement** `nexus/engagement/reports/health.py`:

```python
"""Mailbox health: the bounce rate over the last seven days (spec §19).

Deliverability decays quietly. A mailbox that bounces more than 3% of what it sends is on its way to
the spam folder for everyone, including the buyers whose addresses are good. The figure sits next
to the over-50-a-day warning on My mailboxes, where the SDR already looks.

A warning, never a block, like the volume warning (D10): the fix is usually the list, and stopping
the mailbox would stop the good sends with the bad.

Below `MIN_SENT` sends there is no rate to speak of: one bounce in five sends is 20% and means
nothing, so the figure is reported but no warning is raised.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

WINDOW_DAYS = 7
WARN_ABOVE = 0.03
MIN_SENT = 20


@dataclass(slots=True)
class MailboxHealth:
    sent_7d: int
    bounced_7d: int
    bounce_rate_7d: float
    warning: str

    def as_dict(self) -> dict:
        return asdict(self)


def assess(sent: int, bounced: int) -> MailboxHealth:
    """Pure: the rate and whether it warrants a warning."""
    rate = round(bounced / sent, 4) if sent else 0.0
    warning = ""
    if sent >= MIN_SENT and rate > WARN_ABOVE:
        warning = (f"{rate:.1%} of this week's emails bounced. Above {WARN_ABOVE:.0%}, providers "
                   "start sending the rest to spam. Check the addresses before sending more.")
    return MailboxHealth(sent_7d=sent, bounced_7d=bounced, bounce_rate_7d=rate, warning=warning)


async def mailbox_health(ts, mailbox, *, now: datetime) -> MailboxHealth:
    from sqlalchemy import func, select

    from nexus.models.engagement import EngagementMessage

    since = now - timedelta(days=WINDOW_DAYS)
    rows = (await ts.session.execute(
        select(EngagementMessage.status, func.count())
        .where(EngagementMessage.tenant_id == ts.tenant_id)
        .where(EngagementMessage.mailbox_connection_id == mailbox.id)
        .where(EngagementMessage.direction == "out")
        .where(EngagementMessage.status.in_(("sent", "bounced")))
        .where(EngagementMessage.sent_at >= since)
        .group_by(EngagementMessage.status))).all()
    counts = {status: int(n) for status, n in rows}
    bounced = counts.get("bounced", 0)
    # A bounced message was sent first: it counts in both.
    return assess(counts.get("sent", 0) + bounced, bounced)
```

- [ ] **Step 3: Health on each mailbox row.** Apply to `nexus/api/routers/engagement_mailboxes.py`:

```diff
diff --git a/nexus/api/routers/engagement_mailboxes.py b/nexus/api/routers/engagement_mailboxes.py
index 2834487..d613166 100644
--- a/nexus/api/routers/engagement_mailboxes.py
+++ b/nexus/api/routers/engagement_mailboxes.py
@@ -54,4 +54,9 @@ class MailboxOut(BaseModel):
     sent_today: int = 0
     volume_warning: str = ""
+    # Bounces over the last seven days, and the warning above 3% (spec §19). Never a block either.
+    sent_7d: int = 0
+    bounced_7d: int = 0
+    bounce_rate_7d: float = 0.0
+    health_warning: str = ""
 
 
@@ -90,4 +95,6 @@ async def _settings(ts: TenantSession):
 
 async def _out(ts: TenantSession, row: MailboxConnection, principal: Principal) -> MailboxOut:
+    from nexus.core.db import utcnow
+    from nexus.engagement.reports.health import mailbox_health
     from nexus.engagement.sending.limits import sent_today, volume_warning
     from nexus.engagement.settings import effective_confidence
@@ -95,4 +102,5 @@ async def _out(ts: TenantSession, row: MailboxConnection, principal: Principal)
     settings = await _settings(ts)
     today = await sent_today(ts, row)
+    health = await mailbox_health(ts, row, now=utcnow())
     return MailboxOut(
         id=row.id, provider=row.provider, email=row.email, display_name=row.display_name or "",
@@ -106,4 +114,6 @@ async def _out(ts: TenantSession, row: MailboxConnection, principal: Principal)
         created_at=row.created_at, sent_today=today,
         volume_warning=volume_warning(row.email, today),
+        sent_7d=health.sent_7d, bounced_7d=health.bounced_7d,
+        bounce_rate_7d=health.bounce_rate_7d, health_warning=health.warning,
     )
 
```

---

### Task 4: The reports router

- [ ] **Step 1: Implement** `nexus/api/routers/engagement_reports.py`:

```python
"""Engagement reporting: a campaign's results, response times, and the Today plan (spec §11, §19).

Dark with the rest of the engine. A campaign's results are visible to whoever may open the campaign
(its owner, or a manager); response times are the caller's own unless a manager asks for the team;
Today is always the caller's own.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from nexus.api.deps import Principal, get_tenant_session, require
from nexus.api.routers.engagement_campaigns import _campaign, _is_manager, require_campaigns_enabled
from nexus.core.rbac import Permission
from nexus.core.tenancy import TenantSession

router = APIRouter(prefix="/engagement", tags=["engagement"],
                   dependencies=[Depends(require_campaigns_enabled)])


class StepResultOut(BaseModel):
    step_index: int
    channel: str
    sent: int
    replies: int
    reply_rate: float


class CampaignReportOut(BaseModel):
    contacts: int
    sent: int
    bounced: int
    replied: int
    positive: int
    meetings: int
    reply_rate: float
    positive_rate: float
    steps: list[StepResultOut]
    categories: dict[str, int]


class ResponseTimeOut(BaseModel):
    user_id: str
    name: str
    answered: int
    waiting: int
    median_hours: float | None
    p90_hours: float | None


class TodayItemOut(BaseModel):
    kind: str
    title: str
    detail: str
    link: str
    at: datetime | None
    count: int


@router.get("/reports/campaigns/{campaign_id}", response_model=CampaignReportOut)
async def campaign_results(
    campaign_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> CampaignReportOut:
    from nexus.engagement.reports.service import campaign_report

    campaign = await _campaign(ts, campaign_id, principal)
    return CampaignReportOut(**(await campaign_report(ts, campaign)).as_dict())


@router.get("/reports/response-times", response_model=list[ResponseTimeOut])
async def response_times(
    team: bool = False, days: int = 30,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> list[ResponseTimeOut]:
    from dataclasses import asdict

    from nexus.core.db import utcnow
    from nexus.engagement.reports.service import response_times as measure

    rows = await measure(ts, user_id=principal.user_id, team=team and _is_manager(principal),
                         now=utcnow(), days=max(1, min(days, 90)))
    return [ResponseTimeOut(**asdict(r)) for r in rows]


@router.get("/today", response_model=list[TodayItemOut])
async def today(
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> list[TodayItemOut]:
    from nexus.core.db import utcnow
    from nexus.engagement.reports.today import today as plan

    return [TodayItemOut(**i.as_dict()) for i in await plan(ts, user_id=principal.user_id,
                                                            now=utcnow())]
```

- [ ] **Step 2: Register it** in `nexus/api/routers/__init__.py`:

```diff
diff --git a/nexus/api/routers/__init__.py b/nexus/api/routers/__init__.py
index f3dac62..05759b5 100644
--- a/nexus/api/routers/__init__.py
+++ b/nexus/api/routers/__init__.py
@@ -32,4 +32,5 @@ from nexus.api.routers import (
     engagement_campaigns,
     engagement_desk,
+    engagement_reports,
     engagement_settings,
     engagement_templates,
@@ -88,4 +89,5 @@ all_routers = [
     engagement_campaigns.router,
     engagement_desk.router,
+    engagement_reports.router,
     engagement_settings.router,
     engagement_templates.router,
```

- [ ] **Step 3: Run** `pytest tests/test_engagement_reporting.py -n0 -q` — expected PASS (8).

- [ ] **Step 4: Commit**

```bash
git add nexus/engagement/reports nexus/api/routers nexus/api/schemas.py nexus/engagement/sending/service.py nexus/engagement/replies/ingest.py nexus/engagement/desk/service.py tests/test_engagement_reporting.py tests/test_engagement_desk.py
git commit -m "feat(engagement): results, response times, Today, mailbox health and outcomes"
```

---

### Task 5: The screens

- [ ] **Step 1: Types and client** — `frontend/src/lib/types.ts`:

```diff
diff --git a/frontend/src/lib/types.ts b/frontend/src/lib/types.ts
index 2ee6407..18f6c21 100644
--- a/frontend/src/lib/types.ts
+++ b/frontend/src/lib/types.ts
@@ -2211,4 +2211,9 @@ export interface ConnectedMailbox {
   /** Non-empty above 50 a day (D10). A warning, never a block. */
   volume_warning: string;
+  /** This week's sends and bounces, and the warning above 3% (spec §19). Never a block. */
+  sent_7d: number;
+  bounced_7d: number;
+  bounce_rate_7d: number;
+  health_warning: string;
   created_at: string;
 }
@@ -2492,2 +2497,47 @@ export interface WorkspaceEngagementSettings {
   can_edit: boolean;
 }
+
+// ---- engagement: reporting (phase 12) ----------------------------------------------------------
+
+export interface CampaignStepResult {
+  step_index: number;
+  channel: StepChannel;
+  sent: number;
+  replies: number;
+  reply_rate: number;
+}
+
+/** GET /engagement/reports/campaigns/{id}: counted in people, not messages. */
+export interface CampaignReport {
+  contacts: number;
+  sent: number;
+  bounced: number;
+  replied: number;
+  positive: number;
+  meetings: number;
+  reply_rate: number;
+  positive_rate: number;
+  steps: CampaignStepResult[];
+  categories: Record<string, number>;
+}
+
+export interface ResponseTimeRow {
+  user_id: string;
+  name: string;
+  answered: number;
+  waiting: number;
+  /** Business hours, in the mailbox's own zone. */
+  median_hours: number | null;
+  p90_hours: number | null;
+}
+
+export type TodayKind = "reply" | "decide" | "colleagues" | "call" | "review" | "returning";
+
+export interface TodayItem {
+  kind: TodayKind;
+  title: string;
+  detail: string;
+  link: string;
+  at: string | null;
+  count: number;
+}
```

`frontend/src/lib/api.ts`:

```diff
diff --git a/frontend/src/lib/api.ts b/frontend/src/lib/api.ts
index 2deedf2..79229d3 100644
--- a/frontend/src/lib/api.ts
+++ b/frontend/src/lib/api.ts
@@ -93,4 +93,7 @@ import type {
   ReplyCategory,
   WorkspaceEngagementSettings,
+  CampaignReport,
+  ResponseTimeRow,
+  TodayItem,
   DoNotContactEntry,
   LedgerStatus,
@@ -1344,4 +1347,17 @@ export class ApiClient {
   }
 
+  // ---- engagement: reporting ----
+  campaignReport(id: string, signal?: AbortSignal) {
+    return this.request<CampaignReport>(`/engagement/reports/campaigns/${id}`, { signal });
+  }
+  responseTimes(team = false, signal?: AbortSignal) {
+    return this.request<ResponseTimeRow[]>("/engagement/reports/response-times", {
+      query: { team }, signal,
+    });
+  }
+  engagementToday(signal?: AbortSignal) {
+    return this.request<TodayItem[]>("/engagement/today", { signal });
+  }
+
   // ---- engagement: sequence templates ----
   listSequenceTemplates(signal?: AbortSignal) {
```

- [ ] **Step 2: Results** — `frontend/src/pages/engagement/ReportsPanel.tsx`. Bar length is data, so it passes through the `--w` custom property; everything else is the stylesheet.

```tsx
import type { CSSProperties } from "react";
import { EmptyState, ErrorState, Icons, Skeleton } from "@/components/ui";
import { CATEGORY } from "@/components/engagement/labels";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import type { CampaignReport, ReplyCategory } from "@/lib/types";
import styles from "./ReportsPanel.module.css";

/**
 * What a campaign achieved (spec §11), counted in people, not messages.
 *
 * The funnel is one series, so one hue and no legend: every bar carries its own count and its
 * share of the people sent to. Bounces are a problem, not a stage, so they sit beside the funnel
 * with a status icon rather than as a bar in it. Per step, a reply belongs to the last email the
 * person got before replying, so a follow-up that earned a reply gets the credit. A table under
 * each chart carries the same numbers for anyone who reads tables rather than bars.
 */

function pct(n: number): string {
  return `${Math.round(n * 1000) / 10}%`;
}

export function ReportsPanel({ campaignId }: { campaignId: string }) {
  const api = useApiClient();
  const report = useApi<CampaignReport>((s) => api.campaignReport(campaignId, s), [campaignId]);

  if (report.error) {
    return <ErrorState title="Couldn't load the results" message={report.error.detail} onRetry={report.refetch} />;
  }
  if (!report.data) return <Skeleton width="100%" height={320} />;
  const r = report.data;
  if (r.sent === 0) {
    return (
      <EmptyState icon={<Icons.TrendUpIcon />} title="Nothing sent yet"
        description="Results appear once the first emails go out: who replied, to which step, and what they said." />
    );
  }

  const funnel: [string, number][] = [
    ["People", r.contacts], ["Sent to", r.sent], ["Replied", r.replied],
    ["Positive", r.positive], ["Meetings", r.meetings],
  ];
  const widest = Math.max(1, ...funnel.map(([, n]) => n));
  const categories = Object.entries(r.categories);
  const mostCategory = Math.max(1, ...categories.map(([, n]) => n));
  const emailSteps = r.steps.filter((s) => s.channel === "email");

  return (
    <div className={styles.panel}>
      <section aria-labelledby="funnel-title" className={styles.block}>
        <h3 id="funnel-title" className={styles.title}>People, from added to meeting</h3>
        <ol className={styles.bars}>
          {funnel.map(([label, n], i) => (
            <li key={label} className={styles.barRow}>
              <span className={styles.label}>{label}</span>
              <span className={styles.track} aria-hidden="true">
                <span className={styles.bar} style={{ "--w": `${(n / widest) * 100}%` } as CSSProperties}
                  title={i > 0 ? `${n} of ${r.sent} sent to (${pct(n / (r.sent || 1))})` : `${n} people`} />
              </span>
              <span className={styles.value}>
                {n.toLocaleString()}
                {i > 1 && <span className={styles.rate}> · {pct(n / r.sent)}</span>}
              </span>
            </li>
          ))}
        </ol>
        {r.bounced > 0 && (
          <p className={styles.bounced} role="status">
            <Icons.AlertTriangleIcon aria-hidden className={styles.statusIcon} />
            {r.bounced} {r.bounced === 1 ? "address" : "addresses"} bounced ({pct(r.bounced / r.sent)} of those sent to).
            Bounces are stopped and added to the do-not-contact list.
          </p>
        )}
      </section>

      {emailSteps.length > 0 && (
        <section aria-labelledby="steps-title" className={styles.block}>
          <h3 id="steps-title" className={styles.title}>Reply rate by email</h3>
          <table className={styles.table}>
            <caption className={styles.caption}>
              A reply counts for the last email the person received before replying.
            </caption>
            <thead>
              <tr>
                <th scope="col">Email</th>
                <th scope="col" className={styles.num}>Sent</th>
                <th scope="col" className={styles.num}>Replies</th>
                <th scope="col" className={styles.num}>Reply rate</th>
              </tr>
            </thead>
            <tbody>
              {emailSteps.map((s, i) => (
                <tr key={s.step_index}>
                  <th scope="row">{i === 0 ? "Opening email" : `Follow-up ${i}`}</th>
                  <td className={styles.num}>{s.sent.toLocaleString()}</td>
                  <td className={styles.num}>{s.replies.toLocaleString()}</td>
                  <td className={styles.num}>{s.sent ? pct(s.reply_rate) : "Not sent yet"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      {categories.length > 0 && (
        <section aria-labelledby="categories-title" className={styles.block}>
          <h3 id="categories-title" className={styles.title}>What the replies said</h3>
          <ol className={styles.bars}>
            {categories.map(([category, n]) => (
              <li key={category} className={styles.barRow}>
                <span className={styles.label}>{CATEGORY[category as ReplyCategory]?.label ?? category}</span>
                <span className={styles.track} aria-hidden="true">
                  <span className={styles.bar} style={{ "--w": `${(n / mostCategory) * 100}%` } as CSSProperties}
                    title={`${n} ${n === 1 ? "reply" : "replies"}`} />
                </span>
                <span className={styles.value}>{n.toLocaleString()}</span>
              </li>
            ))}
          </ol>
        </section>
      )}
    </div>
  );
}
```

```css
.panel {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(20rem, 1fr));
  gap: var(--space-5);
}

.block {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  padding: var(--space-5);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background: var(--surface);
  min-width: 0;
}

.title {
  margin: 0;
  font-size: var(--text-md);
  font-weight: var(--weight-semibold);
  color: var(--text);
}

/* ---- bars: one series, one hue, direct labels ---- */

.bars {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  margin: 0;
  padding: 0;
  list-style: none;
}

.barRow {
  display: grid;
  grid-template-columns: 7rem minmax(0, 1fr) auto;
  align-items: center;
  gap: var(--space-3);
}

.label {
  font-size: var(--text-sm);
  color: var(--text-muted);
}

.track {
  display: block;
  height: 12px;
  border-radius: 0 4px 4px 0;
  background: var(--surface-3);
}

.bar {
  display: block;
  width: var(--w, 0%);
  min-width: 2px;
  height: 100%;
  border-radius: 0 4px 4px 0;
  background: var(--accent);
  transition: width var(--dur-slow) var(--ease-out);
}

.value {
  min-width: 5.5rem;
  text-align: right;
  font-size: var(--text-sm);
  font-variant-numeric: tabular-nums;
  color: var(--text);
}

.rate {
  color: var(--text-muted);
}

.bounced {
  display: flex;
  align-items: flex-start;
  gap: var(--space-2);
  margin: 0;
  padding: var(--space-3);
  border: 1px solid var(--warning);
  border-radius: var(--radius);
  background: var(--warning-quiet);
  font-size: var(--text-sm);
  line-height: var(--leading);
  color: var(--text);
}

.statusIcon {
  flex-shrink: 0;
  width: 16px;
  height: 16px;
  margin-top: 2px;
  color: var(--warning);
}

/* ---- table ---- */

.table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--text-sm);
}

.caption {
  padding-bottom: var(--space-2);
  text-align: left;
  color: var(--text-muted);
  line-height: var(--leading);
}

.table th,
.table td {
  padding: var(--space-2) 0;
  border-bottom: 1px solid var(--border);
  text-align: left;
  font-weight: var(--weight-normal);
  color: var(--text);
}

.table thead th {
  color: var(--text-muted);
  font-weight: var(--weight-medium);
}

.table .num {
  text-align: right;
  font-variant-numeric: tabular-nums;
}

@media (prefers-reduced-motion: reduce) {
  .bar {
    transition: none;
  }
}
```

- [ ] **Step 3: Today** — `frontend/src/components/engagement/TodayPlan.tsx`. It renders nothing when empty or unreadable: the dashboard has plenty else, and an error card about an optional list is noise on every load.

```tsx
import { Link } from "react-router-dom";
import { Card, CardHeader, Icons, Skeleton } from "@/components/ui";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import type { TodayItem, TodayKind } from "@/lib/types";
import styles from "./TodayPlan.module.css";

/**
 * Today (spec §19, "Where do I start?"): one ordered list, built by the server, in the order of what
 * costs most if left. A buyer who said yes and is waiting comes first; people returning today come
 * last, because they need nothing.
 *
 * Renders nothing when there is nothing to do, and nothing when it cannot load: the dashboard has
 * plenty else to show, and an error card about an optional list is noise on every page load.
 */

const KIND: Record<TodayKind, { label: string; icon: JSX.Element }> = {
  reply: { label: "Answer", icon: <Icons.MessageIcon /> },
  decide: { label: "Decide", icon: <Icons.HelpCircleIcon /> },
  colleagues: { label: "Colleagues", icon: <Icons.UsersIcon /> },
  call: { label: "Call", icon: <Icons.PhoneIcon /> },
  review: { label: "Review", icon: <Icons.CheckIcon /> },
  returning: { label: "Returning", icon: <Icons.RefreshIcon /> },
};

export function TodayPlan() {
  const api = useApiClient();
  const plan = useApi<TodayItem[]>((s) => api.engagementToday(s), []);

  if (plan.error || (plan.data && plan.data.length === 0)) return null;
  return (
    <Card padding="md" className={styles.card}>
      <CardHeader
        title="Today"
        subtitle={plan.data ? `${plan.data.length} ${plan.data.length === 1 ? "thing" : "things"}, most urgent first` : undefined}
      />
      {!plan.data ? (
        <div className={styles.loading}>
          <Skeleton width="100%" height={44} />
          <Skeleton width="100%" height={44} />
        </div>
      ) : (
        <ol className={styles.list}>
          {plan.data.map((item, i) => {
            const kind = KIND[item.kind];
            return (
              <li key={`${item.kind}-${i}`}>
                <Link to={item.link} className={styles.item}>
                  <span className={styles.icon} aria-hidden="true">{kind.icon}</span>
                  <span className={styles.text}>
                    <span className={styles.title}>{item.title}</span>
                    <span className={styles.detail}>{item.detail}</span>
                  </span>
                  <span className={styles.kind}>{kind.label}</span>
                </Link>
              </li>
            );
          })}
        </ol>
      )}
    </Card>
  );
}
```

```css
.card {
  margin-bottom: var(--space-5);
}

.loading {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.list {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  margin: 0;
  padding: 0;
  list-style: none;
}

.item {
  display: grid;
  grid-template-columns: 32px minmax(0, 1fr) auto;
  align-items: center;
  gap: var(--space-3);
  min-height: 44px;
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius);
  color: inherit;
  text-decoration: none;
  transition: background var(--dur-fast) var(--ease-out);
}

.item:hover {
  background: var(--surface-2);
}

.item:focus-visible {
  outline: 2px solid var(--ring);
  outline-offset: 2px;
}

.icon {
  display: inline-grid;
  place-items: center;
  width: 32px;
  height: 32px;
  border-radius: var(--radius);
  background: var(--accent-quiet);
  color: var(--accent);
}

.icon :global(svg) {
  width: 16px;
  height: 16px;
}

.text {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.title {
  font-weight: var(--weight-medium);
  color: var(--text);
  overflow-wrap: anywhere;
}

.detail {
  font-size: var(--text-sm);
  color: var(--text-muted);
}

.kind {
  font-size: var(--text-xs);
  color: var(--text-muted);
}

@media (max-width: 640px) {
  .kind {
    display: none;
  }
}
```

- [ ] **Step 4: Mailbox health** — `frontend/src/components/engagement/MailboxHealth.tsx`:

```tsx
import { Badge } from "@/components/ui";
import type { ConnectedMailbox } from "@/lib/types";
import styles from "./MailboxHealth.module.css";

/**
 * This week's bounce rate for one mailbox, beside the over-50-a-day warning (spec §19). Status
 * colour never stands alone: the badge says the number, and a warning says what to do.
 */
export function MailboxHealth({ mailbox }: { mailbox: ConnectedMailbox }) {
  if (mailbox.sent_7d === 0) return null;
  const rate = `${Math.round(mailbox.bounce_rate_7d * 1000) / 10}%`;
  return (
    <div className={styles.health}>
      <p className={styles.line}>
        <Badge tone={mailbox.health_warning ? "warning" : "neutral"} dot>{rate} bounced</Badge>
        <span className={styles.muted}>
          {mailbox.bounced_7d} of {mailbox.sent_7d} sent in the last 7 days
        </span>
      </p>
      {mailbox.health_warning && <p className={styles.warning} role="status">{mailbox.health_warning}</p>}
    </div>
  );
}
```

```css
.health {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.line {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
  margin: 0;
}

.muted {
  font-size: var(--text-sm);
  color: var(--text-muted);
}

.warning {
  margin: 0;
  padding: var(--space-3) var(--space-4);
  border: 1px solid var(--warning);
  border-radius: var(--radius);
  background: var(--warning-quiet);
  color: var(--text);
  font-size: var(--text-sm);
  line-height: var(--leading);
}
```

- [ ] **Step 5: Response times** — `frontend/src/components/engagement/ResponseTimes.tsx`:

```tsx
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import type { ResponseTimeRow } from "@/lib/types";
import styles from "./ResponseTimes.module.css";

/**
 * How fast replies that needed an answer were answered, over the last 30 days (spec §11), in
 * business hours in each mailbox's own zone: the clock the reply-speed reminder runs on. A median
 * with the 90th percentile beside it, because one slow week hides inside an average.
 *
 * One line for yourself; a table when a manager is looking at the team.
 */

function hours(h: number | null): string {
  if (h === null) return "no answers yet";
  if (h < 1) return `${Math.max(1, Math.round(h * 60))} min`;
  return `${Math.round(h * 10) / 10} h`;
}

export function ResponseTimes({ team }: { team: boolean }) {
  const api = useApiClient();
  const rows = useApi<ResponseTimeRow[]>((s) => api.responseTimes(team, s), [team]);
  if (rows.error || !rows.data || rows.data.length === 0) return null;

  if (!team) {
    const r = rows.data[0];
    if (r.answered === 0) {
      return (
        <p className={styles.line} aria-live="polite">
          Last 30 days: none answered yet
          {r.waiting > 0 && <>; {r.waiting} waiting for an answer</>}.
        </p>
      );
    }
    return (
      <p className={styles.line} aria-live="polite">
        Last 30 days: {r.answered} {r.answered === 1 ? "reply" : "replies"} answered, typically in{" "}
        <strong>{hours(r.median_hours)}</strong> of working time
        {r.p90_hours !== null && <> (9 in 10 within {hours(r.p90_hours)})</>}
        {r.waiting > 0 && <>; {r.waiting} still waiting</>}.
      </p>
    );
  }
  return (
    <table className={styles.table}>
      <caption className={styles.caption}>
        Time to first answer over the last 30 days, in working hours.
      </caption>
      <thead>
        <tr>
          <th scope="col">Person</th>
          <th scope="col" className={styles.num}>Answered</th>
          <th scope="col" className={styles.num}>Typical</th>
          <th scope="col" className={styles.num}>9 in 10 within</th>
          <th scope="col" className={styles.num}>Waiting</th>
        </tr>
      </thead>
      <tbody>
        {rows.data.map((r) => (
          <tr key={r.user_id}>
            <th scope="row">{r.name || "Unknown"}</th>
            <td className={styles.num}>{r.answered}</td>
            <td className={styles.num}>{hours(r.median_hours)}</td>
            <td className={styles.num}>{hours(r.p90_hours)}</td>
            <td className={styles.num}>{r.waiting}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
```

```css
.line {
  margin: 0;
  font-size: var(--text-sm);
  color: var(--text-muted);
  line-height: var(--leading);
}

.line strong {
  color: var(--text);
  font-weight: var(--weight-semibold);
}

.table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--text-sm);
}

.caption {
  padding-bottom: var(--space-2);
  text-align: left;
  color: var(--text-muted);
}

.table th,
.table td {
  padding: var(--space-2) var(--space-2) var(--space-2) 0;
  border-bottom: 1px solid var(--border);
  text-align: left;
  font-weight: var(--weight-normal);
  color: var(--text);
}

.table thead th {
  color: var(--text-muted);
  font-weight: var(--weight-medium);
}

.table .num {
  text-align: right;
  font-variant-numeric: tabular-nums;
}
```

- [ ] **Step 6: Place them.** `CampaignDetailPage.tsx` (a Results tab once the campaign is past setting up):

```diff
diff --git a/frontend/src/pages/engagement/CampaignDetailPage.tsx b/frontend/src/pages/engagement/CampaignDetailPage.tsx
index 9526ce7..3f5b78d 100644
--- a/frontend/src/pages/engagement/CampaignDetailPage.tsx
+++ b/frontend/src/pages/engagement/CampaignDetailPage.tsx
@@ -20,4 +20,5 @@ import type {
 } from "@/lib/types";
 import { LaunchPanel } from "./LaunchPanel";
+import { ReportsPanel } from "./ReportsPanel";
 import { ReviewQueue } from "./ReviewQueue";
 import styles from "./Engagement.module.css";
@@ -32,5 +33,5 @@ import styles from "./Engagement.module.css";
  */
 
-type TabKey = "people" | "review" | "launch" | "steps";
+type TabKey = "people" | "results" | "review" | "launch" | "steps";
 const SETTING_UP = new Set(["draft", "reviewing"]);
 
@@ -115,4 +116,6 @@ export function CampaignDetailPage() {
   const tabs = [
     { value: "people", label: "People", count: total },
+    // Once anything could have gone out, what came of it.
+    ...(settingUp ? [] : [{ value: "results", label: "Results" }]),
     { value: "review", label: "Review", count: awaiting },
     ...(canLaunch ? [{ value: "launch", label: c.status === "paused" ? "Resume" : "Launch" }] : []),
@@ -182,4 +185,10 @@ export function CampaignDetailPage() {
       </TabPanel>
 
+      {!settingUp && (
+        <TabPanel id="campaign-panel-results" active={tab === "results"}>
+          <ReportsPanel campaignId={c.id} />
+        </TabPanel>
+      )}
+
       <TabPanel id="campaign-panel-review" active={tab === "review"}>
         <ReviewQueue campaignId={c.id} onChanged={refresh} />
```

`DashboardPage.tsx` (Today, only with the engine on):

```diff
diff --git a/frontend/src/pages/DashboardPage.tsx b/frontend/src/pages/DashboardPage.tsx
index 6e69f58..9ef75c6 100644
--- a/frontend/src/pages/DashboardPage.tsx
+++ b/frontend/src/pages/DashboardPage.tsx
@@ -23,4 +23,6 @@ import { useLivePoll } from "@/hooks/useLivePoll";
 import { useApiClient, useAuth } from "@/app/AuthContext";
 import { useSignalWindow } from "@/app/SignalWindowContext";
+import { useEngineOn } from "@/app/EngagementContext";
+import { TodayPlan } from "@/components/engagement/TodayPlan";
 import { ApiError } from "@/lib/api";
 import { formatNumber, formatPercent, humanize, timeAgo } from "@/lib/format";
@@ -89,4 +91,5 @@ export function DashboardPage() {
   const isRep = session?.role === "rep";
   const [seeding, setSeeding] = useState(false);
+  const engineOn = useEngineOn() === true;
 
   const overview = useApi<AnalyticsOverview>((signal) => api.analyticsOverview(signal), []);
@@ -179,4 +182,7 @@ export function DashboardPage() {
       />
 
+      {/* Where to start today: replies waiting, decisions, calls, drafts to approve (spec §19). */}
+      {engineOn && <TodayPlan />}
+
       {/* Workspace setup (ICP, plays) is not a rep's to do, and they cannot open either page. Reps
           only ever missed it because their overview used to 403; with a real one it would nag
```

`MailboxesPage.tsx`:

```diff
diff --git a/frontend/src/pages/engagement/MailboxesPage.tsx b/frontend/src/pages/engagement/MailboxesPage.tsx
index cfecd0c..b5cbd84 100644
--- a/frontend/src/pages/engagement/MailboxesPage.tsx
+++ b/frontend/src/pages/engagement/MailboxesPage.tsx
@@ -17,4 +17,5 @@ import {
 import type { BadgeTone } from "@/components/ui";
 import { DataState } from "@/components/DataState";
+import { MailboxHealth } from "@/components/engagement/MailboxHealth";
 import { useToast } from "@/components/ui/Toast";
 import { useApi } from "@/hooks/useApi";
@@ -254,4 +255,5 @@ function MailboxCard({
           </p>
         )}
+        <MailboxHealth mailbox={mailbox} />
 
         {mailbox.mine && mailbox.status !== "revoked" && (
```

`ReplyDeskPage.tsx` (the readout above the tabs follows the team toggle):

```diff
diff --git a/frontend/src/pages/engagement/ReplyDeskPage.tsx b/frontend/src/pages/engagement/ReplyDeskPage.tsx
index 375878f..681cf83 100644
--- a/frontend/src/pages/engagement/ReplyDeskPage.tsx
+++ b/frontend/src/pages/engagement/ReplyDeskPage.tsx
@@ -9,4 +9,5 @@ import type { Column } from "@/components/ui";
 import { useToast } from "@/components/ui/Toast";
 import { ConversationTimeline } from "@/components/engagement/ConversationTimeline";
+import { ResponseTimes } from "@/components/engagement/ResponseTimes";
 import { CATEGORY, CATEGORY_OPTIONS, reasonText, when, whenDay } from "@/components/engagement/labels";
 import { useApi } from "@/hooks/useApi";
@@ -90,4 +91,6 @@ export function ReplyDeskPage() {
       />
 
+      <ResponseTimes team={team} />
+
       <Tabs items={tabs} value={tab} onChange={(v) => go({ tab: v as TabKey, reply: null })} idPrefix="desk" />
 
```

- [ ] **Step 7: Run** `npm run typecheck && npm run build` — expected PASS.

- [ ] **Step 8: Commit**

```bash
git add frontend/src
git commit -m "feat(ui): campaign results, Today, response times and mailbox health"
```

---

### Task 6: The tests

- [ ] **Step 1:** `tests/test_engagement_reporting.py`:

```python
"""Reporting on the engagement engine (spec §11, §19): campaign results, per-step reply rate,
response times, Outcome rows for the dashboards, the Today plan, and mailbox health.

Campaign tests run the real flow (launch, send through the provider double, a real reply ingested);
the time-sensitive ones build rows with fixed timestamps, because business hours depend on the day.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email import message_from_bytes

import pytest

from nexus.core.config import get_settings
from tests.conftest import auth, principal_from_token, signup, tenant_session
from tests.test_engagement_replies import Mailbox, _mail, _sync
from tests.test_engagement_sequences import _enrollment, _launched, _run


@pytest.fixture
def mailbox_double():
    from nexus.engagement.mailboxes import registry

    box = Mailbox()
    registry.set_provider_factory(lambda _connection: box)
    yield box
    registry.set_provider_factory(None)


@pytest.fixture
def engine_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "engagement_campaigns_enabled", True)


async def _report(tid, campaign_id):
    from nexus.engagement.reports.service import campaign_report
    from nexus.models.engagement import EngagementCampaign

    async with tenant_session(tid) as ts:
        return await campaign_report(ts, await ts.get(EngagementCampaign, campaign_id))


async def _reply(box, our_message: bytes, body: str, *, minutes: int = 5):
    arrived = datetime.now(UTC) + timedelta(minutes=minutes)
    box.arrive(_mail(sender="jane0@acme.io", in_reply_to=message_from_bytes(our_message)["Message-ID"],
                     body=body, when=arrived), when=arrived)


# ---- pure rules ----------------------------------------------------------------------------------

def test_a_bounce_rate_warns_above_three_percent_once_there_is_enough_to_judge():
    from nexus.engagement.reports.health import assess

    assert assess(40, 2).warning, "5% of 40 sends is a real problem"
    assert not assess(40, 1).warning, "2.5% is inside the line"
    # One bounce in five sends is 20%, and means nothing yet.
    few = assess(5, 1)
    assert few.bounce_rate_7d == 0.2 and not few.warning
    assert assess(0, 0).bounce_rate_7d == 0.0


# ---- a campaign's results ------------------------------------------------------------------------

async def test_the_funnel_counts_people_and_credits_the_step_that_drew_the_reply(
    mailbox_double, monkeypatch,
):
    from nexus.engagement.desk.service import decide
    from nexus.models.engagement import ReplyClassification

    tid, campaign_id = await _launched("repfunnel", monkeypatch)
    enrollment = await _enrollment(tid)
    await _run(tid, enrollment.id, enrollment.next_action_at + timedelta(seconds=1))
    enrollment = await _enrollment(tid)
    await _run(tid, enrollment.id, enrollment.next_action_at + timedelta(seconds=1))
    assert len(mailbox_double.delivered) == 2
    # The reply comes after the SECOND email, so the second step earned it.
    await _reply(mailbox_double, mailbox_double.delivered[1], "Sounds interesting, let's talk.")
    await _reply(mailbox_double, mailbox_double.delivered[1], "Also, Thursday works.", minutes=9)
    await _sync(tid, enrollment.mailbox_connection_id)

    report = await _report(tid, campaign_id)
    assert (report.contacts, report.sent, report.replied, report.positive) == (1, 1, 1, 1)
    steps = {s.step_index: s for s in report.steps}
    assert (steps[0].sent, steps[0].replies) == (1, 0)
    assert (steps[1].sent, steps[1].replies) == (1, 1)
    assert steps[1].reply_rate == 1.0
    # Two replies from one person are one reply to the campaign, and two readings.
    assert sum(report.categories.values()) == 2

    async with tenant_session(tid) as ts:
        classification = await ts.first(ReplyClassification)
        await decide(ts, classification, "meeting", user_id="u1")
    assert (await _report(tid, campaign_id)).meetings == 1


async def test_sends_replies_and_meetings_reach_the_outcome_funnel_once_each(
    mailbox_double, monkeypatch,
):
    from nexus.engagement.desk.service import decide
    from nexus.models.engagement import ReplyClassification
    from nexus.models.outcome import Outcome

    tid, campaign_id = await _launched("repoutcome", monkeypatch)
    enrollment = await _enrollment(tid)
    await _run(tid, enrollment.id, enrollment.next_action_at + timedelta(seconds=1))
    await _reply(mailbox_double, mailbox_double.delivered[0], "Interested, let's talk.")
    await _reply(mailbox_double, mailbox_double.delivered[0], "Following up on my reply.", minutes=9)
    await _sync(tid, enrollment.mailbox_connection_id)
    async with tenant_session(tid) as ts:
        classification = await ts.first(ReplyClassification)
        await decide(ts, classification, "meeting", user_id="u1")
        outcomes = await ts.list(Outcome)
    stages = sorted(o.stage for o in outcomes)
    # One sent (one email left), one replied (however many times they wrote), one meeting.
    assert stages == ["meeting", "replied", "sent"]
    for o in outcomes:
        assert o.meta["engagement_campaign_id"] == campaign_id
        assert o.meta["enrollment_id"] == enrollment.id
        # The old campaigns table's key is left alone (spec §13).
        assert o.campaign_id is None


async def test_the_outcomes_api_takes_an_engagement_campaign(client, engine_on, monkeypatch):
    from nexus.models.engagement import EngagementCampaign, MailboxConnection

    token = await signup(client, slug="repapi", email="sam@repapi.com", company="R")
    me = principal_from_token(token)
    async with tenant_session(me.tenant_id) as ts:
        mailbox = MailboxConnection(owner_user_id=me.user_id, provider="google",
                                    email="sam@repapi.com", status="connected")
        ts.add(mailbox)
        await ts.flush()
        campaign = EngagementCampaign(name="Q4", owner_user_id=me.user_id,
                                      mailbox_connection_id=mailbox.id, status="active")
        ts.add(campaign)
        await ts.flush()
        campaign_id = campaign.id
    ok = await client.post("/api/outcomes", headers=auth(token), json={
        "stage": "won", "engagement_campaign_id": campaign_id})
    assert ok.status_code == 201, ok.text
    missing = await client.post("/api/outcomes", headers=auth(token), json={
        "stage": "won", "engagement_campaign_id": "nope"})
    assert missing.status_code == 404
    report = await client.get(f"/api/engagement/reports/campaigns/{campaign_id}",
                              headers=auth(token))
    assert report.status_code == 200 and report.json()["contacts"] == 0


# ---- how fast the team answers -------------------------------------------------------------------

async def _conversation(ts, mailbox, contact, *, received: datetime, answered: datetime | None,
                        category: str = "interested"):
    from nexus.models.engagement import EngagementMessage, EngagementThread, ReplyClassification

    thread = EngagementThread(mailbox_connection_id=mailbox.id,
                              provider_thread_id=f"t-{contact.id}-{received.isoformat()}",
                              contact_id=contact.id, base_subject="Hello")
    ts.add(thread)
    await ts.flush()
    inbound = EngagementMessage(mailbox_connection_id=mailbox.id, thread_id=thread.id,
                                contact_id=contact.id, direction="in", kind="reply",
                                status="received", inbound_kind="human", received_at=received,
                                subject="Re: Hello", body_text="Yes please")
    ts.add(inbound)
    await ts.flush()
    ts.add(ReplyClassification(message_id=inbound.id, mailbox_connection_id=mailbox.id,
                               contact_id=contact.id, account_id=contact.account_id,
                               category=category, confidence=0.9,
                               status="done" if answered else "open"))
    if answered:
        ts.add(EngagementMessage(mailbox_connection_id=mailbox.id, thread_id=thread.id,
                                 contact_id=contact.id, direction="out", kind="response",
                                 status="sent", sent_at=answered, subject="Re: Hello",
                                 body_text="Great"))
    await ts.flush()


async def test_response_time_is_measured_in_the_mailboxs_business_hours(client, engine_on):
    from nexus.models.account import Account, Contact
    from nexus.models.engagement import MailboxConnection

    token = await signup(client, slug="repspeed", email="sam@repspeed.com", company="S")
    me = principal_from_token(token)
    tuesday_9 = datetime(2026, 9, 15, 9, 0, tzinfo=UTC)          # London is UTC+1 in September
    friday_17 = datetime(2026, 9, 18, 16, 0, tzinfo=UTC)         # 17:00 London
    async with tenant_session(me.tenant_id) as ts:
        mailbox = MailboxConnection(owner_user_id=me.user_id, provider="google",
                                    email="sam@repspeed.com", status="connected",
                                    timezone="Europe/London")
        account = Account(name="Acme", domain="acme.io")
        ts.add(mailbox)
        ts.add(account)
        await ts.flush()
        people = [Contact(account_id=account.id, full_name=f"P{i}", email=f"p{i}@acme.io")
                  for i in range(4)]
        for p in people:
            ts.add(p)
        await ts.flush()
        # Answered two hours later, the same working morning.
        await _conversation(ts, mailbox, people[0], received=tuesday_9,
                            answered=tuesday_9 + timedelta(hours=2))
        # Friday 17:00 to Monday 10:00: one hour Friday, one Monday, not 65 by the clock.
        await _conversation(ts, mailbox, people[1], received=friday_17,
                            answered=friday_17 + timedelta(days=2, hours=17))
        # Still waiting, and a decline that never needed an answer.
        await _conversation(ts, mailbox, people[2], received=tuesday_9, answered=None)
        await _conversation(ts, mailbox, people[3], received=tuesday_9, answered=None,
                            category="declined")

    from nexus.engagement.reports.service import response_times

    async with tenant_session(me.tenant_id) as ts:
        rows = await response_times(ts, user_id=me.user_id, team=False,
                                    now=datetime(2026, 9, 22, tzinfo=UTC))
    assert len(rows) == 1
    row = rows[0]
    assert (row.answered, row.waiting) == (2, 1)
    assert row.median_hours == pytest.approx(2.0, abs=0.01)
    r = await client.get("/api/engagement/reports/response-times", headers=auth(token),
                         params={"days": 90})
    assert r.status_code == 200 and r.json()[0]["name"]


# ---- today ---------------------------------------------------------------------------------------

async def test_today_puts_waiting_buyers_first_and_returning_people_last(client, engine_on):
    from nexus.core.db import utcnow
    from nexus.models.account import Account, Contact
    from nexus.models.calling import CallTask
    from nexus.models.engagement import (
        EngagementCampaign,
        EngagementEnrollment,
        MailboxConnection,
    )

    token = await signup(client, slug="reptoday", email="sam@reptoday.com", company="T")
    me = principal_from_token(token)
    now = utcnow()
    async with tenant_session(me.tenant_id) as ts:
        mailbox = MailboxConnection(owner_user_id=me.user_id, provider="google",
                                    email="sam@reptoday.com", status="connected", timezone="UTC")
        account = Account(name="Acme", domain="acme.io")
        ts.add(mailbox)
        ts.add(account)
        await ts.flush()
        people = [Contact(account_id=account.id, full_name=f"P{i}", email=f"p{i}@acme.io")
                  for i in range(6)]
        for p in people:
            ts.add(p)
        await ts.flush()
        campaign = EngagementCampaign(name="Q4", owner_user_id=me.user_id,
                                      mailbox_connection_id=mailbox.id, status="active")
        ts.add(campaign)
        await ts.flush()

        def enroll(person, **fields):
            row = EngagementEnrollment(campaign_id=campaign.id, contact_id=person.id,
                                       account_id=account.id, mailbox_connection_id=mailbox.id,
                                       **fields)
            ts.add(row)
            return row

        enroll(people[0], status="snoozed", status_reason="later",
               snoozed_until=now.replace(hour=23, minute=0, second=0, microsecond=0))
        enroll(people[1], status="paused", status_reason="colleague_replied")
        enroll(people[2], status="awaiting_review")
        calling = enroll(people[3], status="active")
        await ts.flush()
        ts.add(CallTask(account_id=account.id, contact_id=people[3].id, reason="Step 3 of Q4",
                        owner_user_id=me.user_id, due_at=now,
                        engagement_enrollment_id=calling.id))
        await ts.flush()
        await _conversation(ts, mailbox, people[4], received=now - timedelta(hours=3),
                            answered=None, category="unclear")
        await _conversation(ts, mailbox, people[5], received=now - timedelta(hours=1),
                            answered=None, category="interested")

    r = await client.get("/api/engagement/today", headers=auth(token))
    assert r.status_code == 200, r.text
    kinds = [i["kind"] for i in r.json()]
    assert kinds == ["reply", "decide", "colleagues", "call", "review", "returning"]
    first = r.json()[0]
    assert first["title"] == "Answer P5 at Acme" and first["link"].startswith("/engagement/replies?reply=")


# ---- mailbox health, where the SDR already looks -------------------------------------------------

async def test_my_mailboxes_reports_this_weeks_bounce_rate(client, engine_on):
    from nexus.core.db import utcnow
    from nexus.models.account import Account, Contact
    from nexus.models.engagement import EngagementMessage, MailboxConnection

    token = await signup(client, slug="rephealth", email="sam@rephealth.com", company="H")
    me = principal_from_token(token)
    async with tenant_session(me.tenant_id) as ts:
        mailbox = MailboxConnection(owner_user_id=me.user_id, provider="google",
                                    email="sam@rephealth.com", status="connected")
        account = Account(name="Acme", domain="acme.io")
        ts.add(mailbox)
        ts.add(account)
        await ts.flush()
        contact = Contact(account_id=account.id, full_name="Jane", email="jane@acme.io")
        ts.add(contact)
        await ts.flush()
        for i in range(25):
            ts.add(EngagementMessage(
                mailbox_connection_id=mailbox.id, contact_id=contact.id, direction="out",
                kind="oneoff", status="bounced" if i < 2 else "sent",
                sent_at=utcnow() - timedelta(days=1), subject="Hi", body_text="x"))
        # Older than a week: not this week's problem.
        ts.add(EngagementMessage(mailbox_connection_id=mailbox.id, contact_id=contact.id,
                                 direction="out", kind="oneoff", status="bounced",
                                 sent_at=utcnow() - timedelta(days=9), subject="Hi", body_text="x"))
        await ts.flush()
    row = (await client.get("/api/engagement/mailboxes", headers=auth(token))).json()[0]
    assert (row["sent_7d"], row["bounced_7d"]) == (25, 2)
    assert row["bounce_rate_7d"] == 0.08 and "bounced" in row["health_warning"]


async def test_the_reports_are_dark_with_the_engine(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "engagement_campaigns_enabled", False)
    token = await signup(client, slug="repdark", email="sam@repdark.com", company="D")
    for path in ("/api/engagement/today", "/api/engagement/reports/response-times"):
        assert (await client.get(path, headers=auth(token))).status_code == 404
```

- [ ] **Step 2:** The structural checks appended to `tests/test_engagement_screens_ui.py` (the inline-style check is narrowed to allow a data-carrying custom property and nothing else):

```diff
diff --git a/tests/test_engagement_screens_ui.py b/tests/test_engagement_screens_ui.py
index 0e87588..8c4f7bf 100644
--- a/tests/test_engagement_screens_ui.py
+++ b/tests/test_engagement_screens_ui.py
@@ -84,8 +84,12 @@ def test_launch_is_held_while_the_balance_cannot_cover_the_worst_case():
 
 def test_the_engagement_pages_use_tokens_not_inline_styles():
-    offenders = [p.name for p in list(PAGES.glob("*.tsx"))
-                 + list((SRC / "components" / "engagement").glob("*.tsx"))
-                 + [SRC / "pages" / "settings" / "EngagementSettings.tsx"]
-                 if "style={{" in _read(p)]
+    """The one inline style allowed passes DATA to CSS as a custom property (a bar's length);
+    every look comes from the CSS modules and tokens."""
+    offenders = []
+    for p in (list(PAGES.glob("*.tsx")) + list((SRC / "components" / "engagement").glob("*.tsx"))
+              + [SRC / "pages" / "settings" / "EngagementSettings.tsx"]):
+        for match in re.finditer(r"style=\{\{([^}]*)\}", _read(p)):
+            if not match.group(1).strip().startswith('"--'):
+                offenders.append(p.name)
     assert not offenders, f"inline styles in {offenders}: use the CSS modules and tokens"
 
@@ -95,2 +99,28 @@ def test_the_account_page_offers_emails_only_with_the_engine_on():
     assert '...(engineOn ? [{ value: "emails", label: "Emails" }] : [])' in page
     assert "<AccountConversations accountId={id} />" in page
+
+
+# ---- reporting (phase 12) ------------------------------------------------------------------------
+
+def test_results_appear_once_a_campaign_has_launched():
+    page = _read(PAGES / "CampaignDetailPage.tsx")
+    assert '...(settingUp ? [] : [{ value: "results", label: "Results" }])' in page
+    assert "<ReportsPanel campaignId={c.id} />" in page
+
+
+def test_today_is_on_the_dashboard_only_with_the_engine_on():
+    assert "{engineOn && <TodayPlan />}" in _read(SRC / "pages" / "DashboardPage.tsx")
+
+
+def test_the_funnel_is_one_hue_with_bounces_beside_it_not_in_it():
+    """One series, so one colour and no legend; a bounce is a problem, not a funnel stage."""
+    panel = _read(PAGES / "ReportsPanel.tsx")
+    funnel = panel[panel.index("const funnel"):panel.index("const widest")]
+    assert "bounced" not in funnel.lower()
+    assert "styles.bounced" in panel and "AlertTriangleIcon" in panel
+    css = _read(PAGES / "ReportsPanel.module.css")
+    assert css.count("background: var(--accent)") == 1
+
+
+def test_my_mailboxes_shows_this_weeks_bounce_rate():
+    assert "<MailboxHealth mailbox={mailbox} />" in _read(PAGES / "MailboxesPage.tsx")
```

- [ ] **Step 3: Run** `pytest tests/test_engagement_*.py tests/test_refresh_tiering.py tests/test_outcomes.py tests/test_analytics_activity.py tests/test_rep_dashboard.py tests/test_billing_metering_coverage.py tests/test_plan_gated_nav.py -q -n 6` — expected PASS; `ruff check nexus tests` — clean.

- [ ] **Step 4: See it.** In the isolated preview (phase 11, Task 7): the dashboard's Today lists waiting buyers before opening emails to approve; a running campaign's Results shows the funnel and the reply breakdown; Replies shows the response-time line (and "none answered yet" when that is the truth); My mailboxes shows the week's bounce rate.

---

## What phase 13 depends on

Today's order within a kind is age; phase 13 re-ranks the `reply` and `review` kinds by reply likelihood from the insights store (§18.5), without changing the order of kinds. The Results tab is where the insights badges for a campaign go.
