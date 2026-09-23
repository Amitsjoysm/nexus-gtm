# Phase 11: The Engagement Screens Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An SDR can do everything the engine does from the product: build a campaign (people from a saved list, by title and seniority, or by search), read and approve every opening email, launch behind the credit gate, steer each person once it runs, work every reply on the reply desk, keep reusable sequence templates, and see every email to an account in one timeline. A manager sets how replies are handled for the team.

**Architecture:** A handful of server reads the screens need and nothing else provided (names on every row, list expansion, the timeline, the engine switch, moving a scheduled date), then the client: one context that reads the engine switch, nav items that belong to an engine, and routes that wait for it; shared components under `components/engagement/`; seven pages under `pages/engagement/` plus a reply-settings page. Every screen composes the existing primitives (`DataTable`, `Tabs`, `Field`, `Modal`, `WorkingIndicator`, `DataState` states) and the design tokens; nothing is restyled ad hoc.

**Tech Stack:** React 18 + TypeScript (strict) + Vite, CSS Modules over `styles/tokens.css`; FastAPI routers from phases 08 and 10.

**Roadmap:** [00-roadmap.md](00-roadmap.md). **Spec:** §9, §10, D18, D22, D23. **Depends on:** phases 08, 10.

**Verified:** implemented on `feat/sdr-engagement` on top of phases 01–10. In the CI image: `tests/test_engagement_screens_api.py` (8 passed: the switch answers while dark, list expansion with title/seniority filters and the do-not-contact mark, names on the review queue and enrollments, the timeline, rescheduling and cancelling, and a rep refused a colleague's contact), `tests/test_engagement_screens_ui.py` (9 passed), `tests/test_plan_gated_nav.py`, the desk, mailbox, suppression, ledger-capture, feature-switch and notification suites (133 passed together), `ruff`, `npm run typecheck` and `npm run build`. Then exercised in a browser against an isolated preview of this branch (its own container, a throwaway SQLite database, the engine switched on, the offline model): the nav swapped engines; a campaign was created from a template, people added from a saved list with the duplicate-outreach warning shown; an opening email edited and approved; the launch estimate added up (12 + 6 + 3 + 6 = 27 credits); on the reply desk a reply was suggested (the working indicator showed, the subject threaded as one "Re:"), scheduled for a date, then moved; the account page's Emails tab showed both directions across the campaign. No horizontal overflow on any page at 1024px.

---

## Decisions this phase makes

- **The screens gate on the engine switch, not on a plan, and unknown reads as OFF.** `GET /engagement/settings/status` is the one engagement route that answers while the engine is dark. Every new route 404s while it is off, so offering those links on a guess puts a rep on a page that cannot load; the old Campaigns and Cadences work either way, so they stay until the switch is confirmed on. Nav items carry `engine: "on" | "off"`, which makes the whole switch-over one flag: two sets of pages, never both.
- **Every member builds campaigns and works replies**, so the new nav items carry no `minRole`. The server scopes each read to the caller's own mailboxes; a manager sees the team through a toggle.
- **Names come with the rows.** The review queue, the enrollment list, the desk queue, the scheduled list and paused colleagues all carry contact and account names, loaded in two queries per page. A client that looked each one up would issue one request per row.
- **Candidates are expanded on the server.** A saved list holds accounts; a list item that names a person means that person, one that names only an account means everyone there. Contacts with no address are never offered; people on the do-not-contact list are shown and marked so the SDR learns why they are missing.
- **A scheduled date is editable and cancellable** (`POST /engagement/desk/scheduled/{id}/reschedule|cancel`). `move_next` refuses a snoozed contact, and the desk promised this in phase 10 without a function for it. A new date is recorded as a new `enrollment.snoozed` event carrying the date it replaced, rather than a new event type.
- **The colleague actions check ownership.** Phase 10's `colleagues/{id}/{action}` resolved the enrollment without asking whose mailbox it sends from, so any rep could resume or stop a colleague's contact. It now 404s like every other desk route, and the detail marks each paused colleague `actionable` so the screen does not offer what the server would refuse.
- **Quality-check notes are sentences, not tags.** They say what to change, so they render as a wrapping warning list; they are hidden once a person has approved the draft, because they describe the AI's text, not the approved one.
- **Before launch, approval is a message state.** Every person is still `awaiting_review` until launch whether approved or not, so the launch panel counts approvals from the review queue.
- **Reply settings are their own page** (`/engagement/replies/settings`), linked from Replies for managers, because Settings is admin-only and this is a team lead's decision. Reps can open it read-only.

---

## Files

| Action | Path | Responsibility |
|---|---|---|
| Create | `nexus/engagement/sequences/candidates.py` | candidates and the conversation timeline |
| Modify | `nexus/api/routers/engagement_campaigns.py` | names on rows; `/candidates`; `/timeline` |
| Modify | `nexus/api/routers/engagement_settings.py` | `/status` |
| Modify | `nexus/api/routers/engagement_desk.py`, `nexus/engagement/desk/service.py` | reschedule, cancel, colleague ownership and names |
| Modify | `frontend/src/lib/types.ts`, `frontend/src/lib/api.ts` | engagement types and client methods; structured error details |
| Create | `frontend/src/app/EngagementContext.tsx` | the engine switch; `RequireEngine` |
| Modify | `frontend/src/app/nav.tsx`, `components/layout/Sidebar.tsx`, `components/layout/AppShell.tsx`, `App.tsx` | engine-aware nav and routes |
| Create | `frontend/src/components/engagement/labels.ts`, `StepsEditor`, `CreditEstimate`, `VolumeWarning`, `ConversationTimeline`, `ContactPicker`, `AccountConversations` (+ CSS modules) | shared pieces |
| Create | `frontend/src/pages/engagement/CampaignsPage.tsx`, `CampaignBuilder.tsx`, `CampaignDetailPage.tsx`, `ReviewQueue.tsx`, `LaunchPanel.tsx`, `SequenceTemplatesPage.tsx`, `ReplyDeskPage.tsx`, `Engagement.module.css` | the screens |
| Create | `frontend/src/pages/settings/EngagementSettings.tsx` | reply settings |
| Modify | `frontend/src/pages/AccountDetailPage.tsx`, `frontend/src/pages/engagement/MailboxesPage.tsx` | the Emails tab; copy |
| Create | `tests/test_engagement_screens_api.py`, `tests/test_engagement_screens_ui.py` | everything above |

---

### Task 1: What the screens read from the server

**Files:**
- Create: `nexus/engagement/sequences/candidates.py`
- Modify: `nexus/api/routers/engagement_campaigns.py`, `engagement_settings.py`, `engagement_desk.py`, `nexus/engagement/desk/service.py`
- Test: `tests/test_engagement_screens_api.py`

- [ ] **Step 1: Write the failing tests** — create `tests/test_engagement_screens_api.py` with the final text in Task 6.

- [ ] **Step 2: Run** `pytest tests/test_engagement_screens_api.py -n0 -q` — expected FAIL: 404 on `/api/engagement/settings/status`, and `No module named 'nexus.engagement.sequences.candidates'`.

- [ ] **Step 3: Implement** `nexus/engagement/sequences/candidates.py`:

```python
"""Who can be added to a campaign, and what has already been said to them (spec §9).

Two reads the campaign screens need and nothing else provides:

* **Candidates.** The builder's first step picks contacts "from a list, filters or search", and a
  saved list holds ACCOUNTS, so it has to expand to the people at them, narrowed by title and
  seniority (§9: "an account list expands to contacts with title and seniority pickers"). A contact
  with no address is left out, because it could never be sent to. One on the do-not-contact list
  is returned and marked, not hidden: an SDR who cannot find a person they expected needs to see
  why, and `enroll` refuses them anyway.
* **The conversation timeline.** Every engagement message to or from a contact (or everyone at an
  account), across campaigns and one-off sends, newest last — the "cross-campaign conversation
  timeline" on the contact and account pages.

Both are bounded, and both read in a fixed number of queries whatever the page size.
"""
from __future__ import annotations

from dataclasses import dataclass

#: The most a candidate search returns. A campaign bigger than this is added in several passes,
#: which is also the size at which a person stops reading the rows they are ticking.
CANDIDATE_LIMIT = 500
TIMELINE_LIMIT = 200


@dataclass(slots=True)
class Candidate:
    contact_id: str
    full_name: str
    title: str
    seniority: str
    email: str
    email_status: str
    account_id: str
    account_name: str
    blocked: bool


def _terms(raw: str | None) -> list[str]:
    return [t.strip().lower() for t in (raw or "").split(",") if t.strip()]


async def candidates(ts, *, list_id: str | None = None, q: str | None = None,
                     title: str | None = None, seniority: str | None = None,
                     limit: int = CANDIDATE_LIMIT) -> list[Candidate]:
    """Contacts with an address, optionally from one saved list, filtered by title keywords (any of
    a comma-separated set), seniority (any of a set) and free text."""
    from sqlalchemy import func, or_, select

    from nexus.models.account import Account, Contact
    from nexus.models.engagement import DoNotContact
    from nexus.models.workflow import ListItem

    stmt = (select(Contact, Account)
            .join(Account, Account.id == Contact.account_id)
            .where(Contact.tenant_id == ts.tenant_id, Account.tenant_id == ts.tenant_id)
            .where(Contact.deleted_at.is_(None))
            .where(func.coalesce(Contact.email, "") != ""))
    if list_id:
        items = await ts.list(ListItem, ListItem.list_id == list_id)
        named = {i.contact_id for i in items if i.contact_id}
        whole = {i.account_id for i in items if not i.contact_id}
        if not named and not whole:
            return []
        # A list item that names a person means that person; one that names only an account means
        # everyone there.
        clauses = []
        if named:
            clauses.append(Contact.id.in_(named))
        if whole:
            clauses.append(Contact.account_id.in_(whole))
        stmt = stmt.where(or_(*clauses))
    if q and q.strip():
        like = f"%{q.strip().lower()}%"
        stmt = stmt.where(or_(func.lower(Contact.full_name).like(like),
                              func.lower(func.coalesce(Contact.title, "")).like(like),
                              func.lower(Contact.email).like(like),
                              func.lower(Account.name).like(like)))
    titles = _terms(title)
    if titles:
        stmt = stmt.where(or_(*[func.lower(func.coalesce(Contact.title, "")).like(f"%{t}%")
                                for t in titles]))
    levels = _terms(seniority)
    if levels:
        stmt = stmt.where(func.lower(func.coalesce(Contact.seniority, "")).in_(levels))
    stmt = stmt.order_by(Account.name.asc(), Contact.full_name.asc()) \
        .limit(max(1, min(limit, CANDIDATE_LIMIT)))
    rows = (await ts.session.execute(stmt)).all()
    if not rows:
        return []

    addresses = {(c.email or "").strip().lower() for c, _a in rows}
    blocked = {d.email for d in await ts.list(DoNotContact, DoNotContact.email.in_(addresses),
                                              DoNotContact.lifted_at.is_(None))}
    return [Candidate(
        contact_id=c.id, full_name=c.full_name or "", title=c.title or "",
        seniority=c.seniority or "", email=c.email or "", email_status=c.email_status or "",
        account_id=a.id, account_name=a.name or "",
        blocked=(c.email or "").strip().lower() in blocked,
    ) for c, a in rows]


@dataclass(slots=True)
class TimelineEntry:
    message_id: str
    direction: str
    kind: str
    status: str
    subject: str
    preview: str
    at: object
    contact_id: str | None
    contact_name: str
    campaign_id: str | None
    campaign_name: str
    category: str | None


async def timeline(ts, *, contact_id: str | None = None, account_id: str | None = None,
                   limit: int = TIMELINE_LIMIT) -> list[TimelineEntry]:
    """What was sent and received, oldest first, across every campaign and one-off send.

    Drafts and queued rows are not conversation: only what left or arrived is shown.
    """
    from nexus.models.account import Contact
    from nexus.models.engagement import (
        EngagementCampaign,
        EngagementEnrollment,
        EngagementMessage,
        ReplyClassification,
    )

    if not contact_id and not account_id:
        return []
    if contact_id:
        people = {contact_id}
    else:
        people = {c.id for c in await ts.list(Contact, Contact.account_id == account_id)}
    if not people:
        return []
    messages = await ts.list(EngagementMessage, EngagementMessage.contact_id.in_(people),
                             EngagementMessage.status.in_(("sent", "received", "bounced")))
    messages.sort(key=lambda m: m.sent_at or m.received_at or m.created_at)
    messages = messages[-max(1, min(limit, TIMELINE_LIMIT)):]
    if not messages:
        return []

    enrollment_ids = {m.enrollment_id for m in messages if m.enrollment_id}
    enrollments = {e.id: e for e in await ts.list(
        EngagementEnrollment, EngagementEnrollment.id.in_(enrollment_ids))} if enrollment_ids else {}
    campaign_ids = {e.campaign_id for e in enrollments.values()}
    campaigns = {c.id: c for c in await ts.list(
        EngagementCampaign, EngagementCampaign.id.in_(campaign_ids))} if campaign_ids else {}
    names = {c.id: c.full_name or "" for c in await ts.list(Contact, Contact.id.in_(people))}
    readings = {r.message_id: r for r in await ts.list(
        ReplyClassification, ReplyClassification.message_id.in_([m.id for m in messages]))}

    out = []
    for m in messages:
        enrollment = enrollments.get(m.enrollment_id)
        campaign = campaigns.get(getattr(enrollment, "campaign_id", None))
        reading = readings.get(m.id)
        out.append(TimelineEntry(
            message_id=m.id, direction=m.direction, kind=m.kind, status=m.status,
            subject=m.subject or "", preview=" ".join((m.body_text or "").split())[:400],
            at=m.sent_at or m.received_at or m.created_at, contact_id=m.contact_id,
            contact_name=names.get(m.contact_id, ""),
            campaign_id=getattr(campaign, "id", None), campaign_name=getattr(campaign, "name", ""),
            category=(reading.corrected_category or reading.category) if reading else None,
        ))
    return out
```

- [ ] **Step 4: Names, candidates and the timeline on the campaigns router.** Apply to `nexus/api/routers/engagement_campaigns.py`:

```diff
diff --git a/nexus/api/routers/engagement_campaigns.py b/nexus/api/routers/engagement_campaigns.py
index b4d4aa1..a9681dc 100644
--- a/nexus/api/routers/engagement_campaigns.py
+++ b/nexus/api/routers/engagement_campaigns.py
@@ -86,4 +86,8 @@ class ReviewItemOut(BaseModel):
     enrollment_id: str
     contact_id: str
+    contact_name: str = ""
+    contact_email: str = ""
+    contact_title: str = ""
+    account_name: str = ""
     message_id: str | None
     subject: str
@@ -110,4 +114,8 @@ class EnrollmentOut(BaseModel):
     contact_id: str
     account_id: str
+    contact_name: str = ""
+    contact_email: str = ""
+    contact_title: str = ""
+    account_name: str = ""
     status: str
     status_reason: str | None
@@ -158,4 +166,87 @@ def _refuse(exc: Exception) -> HTTPException:
 
 
+async def _people(ts: TenantSession, contact_ids, account_ids) -> tuple[dict, dict]:
+    """Contacts and accounts by id, in two queries for the whole page rather than two per row."""
+    from nexus.models.account import Account, Contact
+
+    contact_ids = {c for c in contact_ids if c}
+    account_ids = {a for a in account_ids if a}
+    contacts = {c.id: c for c in await ts.list(Contact, Contact.id.in_(contact_ids))} \
+        if contact_ids else {}
+    account_ids |= {c.account_id for c in contacts.values() if c.account_id}
+    accounts = {a.id: a for a in await ts.list(Account, Account.id.in_(account_ids))} \
+        if account_ids else {}
+    return contacts, accounts
+
+
+def _named(contact, account) -> dict:
+    return {"contact_name": getattr(contact, "full_name", "") or "",
+            "contact_email": getattr(contact, "email", "") or "",
+            "contact_title": getattr(contact, "title", "") or "",
+            "account_name": getattr(account, "name", "") or ""}
+
+
+class CandidateOut(BaseModel):
+    contact_id: str
+    full_name: str
+    title: str
+    seniority: str
+    email: str
+    email_status: str
+    account_id: str
+    account_name: str
+    blocked: bool
+
+
+class TimelineEntryOut(BaseModel):
+    message_id: str
+    direction: str
+    kind: str
+    status: str
+    subject: str
+    preview: str
+    at: datetime | None
+    contact_id: str | None
+    contact_name: str
+    campaign_id: str | None
+    campaign_name: str
+    category: str | None
+
+
+@router.get("/candidates", response_model=list[CandidateOut])
+async def list_candidates(
+    list_id: str | None = None, q: str | None = None, title: str | None = None,
+    seniority: str | None = None, limit: int = 500,
+    ts: TenantSession = Depends(get_tenant_session),
+    _: Principal = Depends(require(Permission.run_engagement)),
+) -> list[CandidateOut]:
+    """People who could be added to a campaign: from a saved list, by title and seniority."""
+    from dataclasses import asdict
+
+    from nexus.engagement.sequences.candidates import candidates
+
+    rows = await candidates(ts, list_id=list_id, q=q, title=title, seniority=seniority,
+                            limit=limit)
+    return [CandidateOut(**asdict(r)) for r in rows]
+
+
+@router.get("/timeline", response_model=list[TimelineEntryOut])
+async def get_timeline(
+    contact_id: str | None = None, account_id: str | None = None,
+    ts: TenantSession = Depends(get_tenant_session),
+    _: Principal = Depends(require(Permission.run_engagement)),
+) -> list[TimelineEntryOut]:
+    """Everything sent to and received from a contact, or everyone at an account."""
+    from dataclasses import asdict
+
+    from nexus.engagement.sequences.candidates import timeline
+
+    if not contact_id and not account_id:
+        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
+                            "Name a contact_id or an account_id")
+    rows = await timeline(ts, contact_id=contact_id, account_id=account_id)
+    return [TimelineEntryOut(**asdict(r)) for r in rows]
+
+
 @router.get("/campaigns", response_model=list[CampaignOut])
 async def list_campaigns(
@@ -256,9 +347,13 @@ async def get_review(
 
     campaign = await _campaign(ts, campaign_id, principal)
+    rows = await review_queue(ts, campaign)
+    contacts, accounts = await _people(ts, [e.contact_id for e, _m in rows],
+                                       [e.account_id for e, _m in rows])
     return [ReviewItemOut(
         enrollment_id=e.id, contact_id=e.contact_id, message_id=getattr(m, "id", None),
+        **_named(contacts.get(e.contact_id), accounts.get(e.account_id)),
         subject=getattr(m, "subject", "") or "", body=getattr(m, "body_text", "") or "",
         quality_problems=list(getattr(m, "quality_problems", None) or []),
-        status=getattr(m, "status", "undrafted")) for e, m in await review_queue(ts, campaign)]
+        status=getattr(m, "status", "undrafted")) for e, m in rows]
 
 
@@ -302,5 +397,8 @@ async def regenerate_message(
     except CampaignError as exc:
         raise _refuse(exc) from exc
+    contacts, accounts = await _people(ts, [message.contact_id], [])
+    contact = contacts.get(message.contact_id)
     return ReviewItemOut(enrollment_id=message.enrollment_id, contact_id=message.contact_id,
+                         **_named(contact, accounts.get(getattr(contact, "account_id", None))),
                          message_id=message.id, subject=message.subject, body=message.body_text,
                          quality_problems=list(message.quality_problems or []),
@@ -394,6 +492,9 @@ async def list_enrollments(
     campaign = await _campaign(ts, campaign_id, principal)
     rows = await ts.list(EngagementEnrollment, EngagementEnrollment.campaign_id == campaign.id)
+    contacts, accounts = await _people(ts, [e.contact_id for e in rows],
+                                       [e.account_id for e in rows])
     return [EnrollmentOut(
-        id=e.id, contact_id=e.contact_id, account_id=e.account_id, status=e.status,
+        id=e.id, contact_id=e.contact_id, account_id=e.account_id,
+        **_named(contacts.get(e.contact_id), accounts.get(e.account_id)), status=e.status,
         status_reason=e.status_reason, current_step_index=e.current_step_index,
         next_action_at=e.next_action_at, snoozed_until=e.snoozed_until,
```

`/candidates` and `/timeline` are declared before `/campaigns`; neither collides with `/campaigns/{campaign_id}/{action}`, which is POST only.

- [ ] **Step 5: The engine switch.** Apply to `nexus/api/routers/engagement_settings.py` (this router is not behind `require_campaigns_enabled`, which is the point):

```diff
diff --git a/nexus/api/routers/engagement_settings.py b/nexus/api/routers/engagement_settings.py
index 73fad23..edcb2b1 100644
--- a/nexus/api/routers/engagement_settings.py
+++ b/nexus/api/routers/engagement_settings.py
@@ -52,4 +52,11 @@ class EngagementSettingsOut(BaseModel):
 
 
+class EngagementStatusOut(BaseModel):
+    #: The engine's switch. While it is off every campaign, reply desk and template route 404s,
+    #: so the screens hide rather than offer pages that cannot load.
+    engine_on: bool
+    can_manage: bool
+
+
 class EngagementSettingsPatch(BaseModel):
     model_config = {"extra": "forbid"}
@@ -126,4 +133,18 @@ async def _tenant(ts: TenantSession):
 
 
+@router.get("/status", response_model=EngagementStatusOut)
+async def get_engagement_status(
+    principal: Principal = Depends(require(Permission.run_engagement)),
+) -> EngagementStatusOut:
+    """Answered whatever the switch says. Everything else about campaigns is dark while it is
+    off, so this is the one route the navigation can ask."""
+    from nexus.engagement import config
+
+    return EngagementStatusOut(
+        engine_on=config.campaigns_enabled(),
+        can_manage=has_permission(Role(principal.role), Permission.manage_engagement),
+    )
+
+
 @router.get("", response_model=EngagementSettingsOut)
 async def get_engagement_settings(
```

- [ ] **Step 6: Scheduled dates, and the colleague ownership fix.** Apply to `nexus/engagement/desk/service.py`:

```diff
diff --git a/nexus/engagement/desk/service.py b/nexus/engagement/desk/service.py
index 45b0392..c634456 100644
--- a/nexus/engagement/desk/service.py
+++ b/nexus/engagement/desk/service.py
@@ -335,4 +335,45 @@ async def stop_colleague(ts, enrollment, *, user_id: str) -> None:
 
 
+def _is_scheduled(enrollment) -> bool:
+    return enrollment.status == "snoozed" or (
+        enrollment.status == "paused" and enrollment.status_reason == "out_of_office")
+
+
+async def reschedule(ts, enrollment, when: datetime, *, user_id: str) -> None:
+    """Move a scheduled contact's return date: a re-engagement they asked for, or the day they are
+    back from leave. The worker wakes both on ``snoozed_until``, so that is the one field moved."""
+    from nexus.core.db import utcnow
+    from nexus.engagement.ledger.emit import emit
+
+    if not _is_scheduled(enrollment):
+        raise DeskError("Only a contact waiting for a date can be given a new one.")
+    if when.tzinfo is None:
+        when = when.replace(tzinfo=UTC)
+    if when <= utcnow():
+        raise DeskError("Choose a date in the future, or resume the contact instead.")
+    previous = enrollment.snoozed_until
+    enrollment.snoozed_until = when
+    await ts.flush()
+    # A new date is a new snooze, so it is recorded as one: same event, same shape as
+    # `set_status` writes, with the date it replaced.
+    await emit(ts, "enrollment.snoozed", actor_user_id=user_id,
+               refs={"enrollment_id": enrollment.id, "campaign_id": enrollment.campaign_id,
+                     "contact_id": enrollment.contact_id, "account_id": enrollment.account_id},
+               payload={"from": enrollment.status, "to": enrollment.status,
+                        "reason": enrollment.status_reason or "",
+                        "step_index": enrollment.current_step_index,
+                        "until": when.isoformat(),
+                        "rescheduled_from": previous.isoformat() if previous else ""})
+
+
+async def cancel_scheduled(ts, enrollment, *, user_id: str) -> None:
+    """The date is not wanted after all: stop the contact, so nothing more is sent."""
+    from nexus.engagement.sequences.service import stop_enrollment
+
+    if not _is_scheduled(enrollment):
+        raise DeskError("Only a contact waiting for a date can be cancelled here.")
+    await stop_enrollment(ts, enrollment, "manual", user_id=user_id)
+
+
 async def remind_unanswered(ts, *, now: datetime) -> int:
     """Nudge the SDR when an interested buyer has been waiting (§19 reply-speed reminder).
```

and to `nexus/api/routers/engagement_desk.py`:

```diff
diff --git a/nexus/api/routers/engagement_desk.py b/nexus/api/routers/engagement_desk.py
index 4ccb03b..ab1a2f5 100644
--- a/nexus/api/routers/engagement_desk.py
+++ b/nexus/api/routers/engagement_desk.py
@@ -64,5 +64,9 @@ class ColleagueOut(BaseModel):
     enrollment_id: str
     contact_id: str
+    contact_name: str = ""
     campaign_id: str
+    #: False when the colleague is in a campaign sending from someone else's mailbox: shown, so the
+    #: SDR knows why they went quiet, but only that mailbox's owner or a manager can resume them.
+    actionable: bool = True
 
 
@@ -217,7 +221,7 @@ async def read_item(
     classification = await _classification(ts, classification_id, principal)
     detail = await service.item(ts, classification)
-    names = _Names(
-        contacts={detail.contact.id: detail.contact} if detail.contact else {},
-        accounts={detail.account.id: detail.account} if detail.account else {})
+    names = await _names(
+        ts, [classification.contact_id, *(e.contact_id for e in detail.paused_colleagues)],
+        [classification.account_id])
     return ItemOut(
         **_row(classification, detail.message, names),
@@ -227,7 +231,10 @@ async def read_item(
             id=m.id, direction=m.direction, subject=m.subject or "", body=m.body_text or "",
             at=m.sent_at or m.received_at or m.created_at) for m in detail.conversation],
-        paused_colleagues=[ColleagueOut(enrollment_id=e.id, contact_id=e.contact_id,
-                                        campaign_id=e.campaign_id)
-                           for e in detail.paused_colleagues])
+        paused_colleagues=[ColleagueOut(
+            enrollment_id=e.id, contact_id=e.contact_id, campaign_id=e.campaign_id,
+            contact_name=getattr(names.contact(e.contact_id), "full_name", "") or "",
+            actionable=_is_manager(principal)
+            or e.mailbox_connection_id == classification.mailbox_connection_id)
+            for e in detail.paused_colleagues])
 
 
@@ -324,4 +331,54 @@ async def assign(
 
 
+class RescheduleIn(BaseModel):
+    model_config = {"extra": "forbid"}
+
+    when: datetime
+
+
+async def _owned_enrollment(ts: TenantSession, enrollment_id: str, principal: Principal):
+    """An enrollment sending from one of the caller's mailboxes (or any, for a manager). 404, not
+    403, otherwise: a 403 would confirm the id exists."""
+    from nexus.models.engagement import EngagementEnrollment, MailboxConnection
+
+    enrollment = await ts.get(EngagementEnrollment, enrollment_id)
+    mailbox = await ts.get(MailboxConnection, enrollment.mailbox_connection_id) \
+        if enrollment is not None and enrollment.mailbox_connection_id else None
+    if enrollment is None or mailbox is None or (
+            mailbox.owner_user_id != principal.user_id and not _is_manager(principal)):
+        raise HTTPException(status.HTTP_404_NOT_FOUND, "Contact not found")
+    return enrollment
+
+
+@router.post("/scheduled/{enrollment_id}/reschedule", status_code=204, response_model=None)
+async def reschedule(
+    enrollment_id: str, body: RescheduleIn,
+    ts: TenantSession = Depends(get_tenant_session),
+    principal: Principal = Depends(require(Permission.run_engagement)),
+) -> None:
+    from nexus.engagement.desk import service
+
+    enrollment = await _owned_enrollment(ts, enrollment_id, principal)
+    try:
+        await service.reschedule(ts, enrollment, body.when, user_id=principal.user_id)
+    except service.DeskError as exc:
+        raise _refuse(exc) from exc
+
+
+@router.post("/scheduled/{enrollment_id}/cancel", status_code=204, response_model=None)
+async def cancel_scheduled(
+    enrollment_id: str,
+    ts: TenantSession = Depends(get_tenant_session),
+    principal: Principal = Depends(require(Permission.run_engagement)),
+) -> None:
+    from nexus.engagement.desk import service
+
+    enrollment = await _owned_enrollment(ts, enrollment_id, principal)
+    try:
+        await service.cancel_scheduled(ts, enrollment, user_id=principal.user_id)
+    except service.DeskError as exc:
+        raise _refuse(exc) from exc
+
+
 @router.post("/colleagues/{enrollment_id}/{action}", status_code=204, response_model=None)
 async def colleague(
@@ -331,9 +388,6 @@ async def colleague(
 ) -> None:
     from nexus.engagement.desk import service
-    from nexus.models.engagement import EngagementEnrollment
 
-    enrollment = await ts.get(EngagementEnrollment, enrollment_id)
-    if enrollment is None:
-        raise HTTPException(status.HTTP_404_NOT_FOUND, "Contact not found")
+    enrollment = await _owned_enrollment(ts, enrollment_id, principal)
     try:
         if action == "resume":
```

`/scheduled/{id}/reschedule` has three path segments, so it cannot be read as `/{classification_id}/{action}`.

- [ ] **Step 7: Run** `pytest tests/test_engagement_screens_api.py tests/test_engagement_desk.py -n0 -q` — expected PASS.

- [ ] **Step 8: Commit**

```bash
git add nexus/engagement/sequences/candidates.py nexus/api/routers/engagement_*.py nexus/engagement/desk/service.py tests/test_engagement_screens_api.py
git commit -m "feat(engagement): the reads the screens need — names, candidates, timeline, status"
```

---

### Task 2: The client plumbing

**Files:**
- Modify: `frontend/src/lib/types.ts`, `frontend/src/lib/api.ts`
- Create: `frontend/src/app/EngagementContext.tsx`
- Modify: `frontend/src/app/nav.tsx`, `frontend/src/components/layout/Sidebar.tsx`, `frontend/src/components/layout/AppShell.tsx`

- [ ] **Step 1: Types.** Append to `frontend/src/lib/types.ts` (the name `EngagementEnrollmentStatus` avoids the old cadence engine's `EnrollmentStatus`):

```diff
diff --git a/frontend/src/lib/types.ts b/frontend/src/lib/types.ts
index d0e32c9..2ee6407 100644
--- a/frontend/src/lib/types.ts
+++ b/frontend/src/lib/types.ts
@@ -2270,2 +2270,224 @@ export interface LedgerStatus {
   undecided_workspaces: number;
 }
+
+// ---- engagement: campaigns, sequences and the reply desk (phase 11) ----------------------------
+
+/** GET /engagement/settings/status — the engine switch, answered even while it is off. */
+export interface EngagementStatus {
+  engine_on: boolean;
+  can_manage: boolean;
+}
+
+export type StepChannel = "email" | "call";
+export type StepTiming = "auto" | "manual";
+
+export interface EngagementStep {
+  step_index?: number;
+  channel: StepChannel;
+  angle: string;
+  timing_mode: StepTiming;
+  delay_business_days: number;
+  send_time_local: string | null;
+  allowed_weekdays: number[];
+}
+
+/** draft → reviewing (first emails drafted) → active → paused / completed; stop ends it as completed. */
+export type EngagementCampaignStatus = "draft" | "reviewing" | "active" | "paused" | "completed";
+
+export interface EngagementCampaign {
+  id: string;
+  name: string;
+  owner_user_id: string;
+  mailbox_connection_id: string | null;
+  status: EngagementCampaignStatus;
+  pause_reason: string | null;
+  review_every_touch: boolean;
+  first_send_mode: "on_approval" | "scheduled";
+  first_send_at: string | null;
+  timezone_mode: "contact" | "sdr";
+  launched_at: string | null;
+  steps: Required<EngagementStep>[];
+  /** Enrollment count by status. */
+  counts: Record<string, number>;
+}
+
+export interface EngagementCampaignInput {
+  name: string;
+  mailbox_id: string;
+  steps?: EngagementStep[];
+  template_id?: string | null;
+  review_every_touch?: boolean;
+  first_send_mode?: "on_approval" | "scheduled";
+  first_send_at?: string | null;
+  timezone_mode?: "contact" | "sdr";
+  source_list_id?: string | null;
+}
+
+export interface EngagementCandidate {
+  contact_id: string;
+  full_name: string;
+  title: string;
+  seniority: string;
+  email: string;
+  email_status: string;
+  account_id: string;
+  account_name: string;
+  /** On the do-not-contact list: shown so the SDR knows why, refused if added. */
+  blocked: boolean;
+}
+
+export interface EnrollResult {
+  added: string[];
+  /** reason: not_found | already_enrolled | no_email | do_not_contact:<why> */
+  skipped: { contact_id: string; reason: string }[];
+  /** The person is already in a colleague's (or another) live campaign. Added anyway. */
+  warnings: { contact_id: string; campaign_id: string; campaign_name: string; owner: string }[];
+}
+
+export interface ReviewItem {
+  enrollment_id: string;
+  contact_id: string;
+  contact_name: string;
+  contact_email: string;
+  contact_title: string;
+  account_name: string;
+  message_id: string | null;
+  subject: string;
+  body: string;
+  quality_problems: string[];
+  /** undrafted | draft | approved | ... */
+  status: string;
+}
+
+export type EngagementEnrollmentStatus =
+  | "awaiting_review" | "active" | "paused" | "snoozed" | "stopped" | "completed";
+
+export interface EngagementEnrollment {
+  id: string;
+  contact_id: string;
+  account_id: string;
+  contact_name: string;
+  contact_email: string;
+  contact_title: string;
+  account_name: string;
+  status: EngagementEnrollmentStatus;
+  status_reason: string | null;
+  current_step_index: number;
+  next_action_at: string | null;
+  snoozed_until: string | null;
+  contact_timezone: string;
+}
+
+export interface CapabilityLine {
+  units: number;
+  credits: number;
+  likely_units: number;
+  likely_credits: number;
+}
+
+/** GET /engagement/campaigns/{id}/estimate (spec §10, D18). */
+export interface CampaignEstimate {
+  contacts: number;
+  email_steps: number;
+  worst_credits: number;
+  likely_credits: number;
+  balance: number;
+  gate_applies: boolean;
+  covered: boolean;
+  shortfall: number;
+  per_capability: Record<string, CapabilityLine>;
+  /** Non-empty above 50 sends a day from one mailbox (D10). Never a block. */
+  volume_warning: string;
+}
+
+export interface SequenceTemplate {
+  id: string;
+  name: string;
+  description: string;
+  steps: EngagementStep[];
+  created_at: string;
+}
+
+export interface TimelineEntry {
+  message_id: string;
+  direction: "out" | "in";
+  kind: string;
+  status: string;
+  subject: string;
+  preview: string;
+  at: string | null;
+  contact_id: string | null;
+  contact_name: string;
+  campaign_id: string | null;
+  campaign_name: string;
+  category: string | null;
+}
+
+export type ReplyCategory =
+  | "interested" | "question" | "referral" | "later" | "out_of_office" | "declined"
+  | "unsubscribe" | "unclear";
+
+export type DeskDecision = "reengage" | "block" | "close" | "meeting";
+
+export interface DeskQueueItem {
+  id: string;
+  message_id: string;
+  category: ReplyCategory;
+  corrected_category: ReplyCategory | null;
+  confidence: number;
+  resolved_date: string | null;
+  status: "open" | "done";
+  decision: DeskDecision | null;
+  assigned_user_id: string | null;
+  contact_id: string | null;
+  account_id: string | null;
+  contact_name: string;
+  contact_email: string;
+  account_name: string;
+  subject: string;
+  preview: string;
+  received_at: string | null;
+  responded_at: string | null;
+}
+
+export interface DeskConversationMessage {
+  id: string;
+  direction: "out" | "in";
+  subject: string;
+  body: string;
+  at: string | null;
+}
+
+export interface DeskItemDetail extends DeskQueueItem {
+  body: string;
+  suggested_response: string | null;
+  conversation: DeskConversationMessage[];
+  paused_colleagues: {
+    enrollment_id: string;
+    contact_id: string;
+    contact_name: string;
+    campaign_id: string;
+    /** False when the colleague is in someone else's campaign: shown, not steerable. */
+    actionable: boolean;
+  }[];
+}
+
+export interface DeskScheduledItem {
+  enrollment_id: string;
+  contact_id: string;
+  contact_name: string;
+  account_name: string;
+  campaign_id: string;
+  status: string;
+  status_reason: string | null;
+  due_at: string | null;
+}
+
+export interface WorkspaceEngagementSettings {
+  reply_confidence_default: number;
+  reply_confidence_min: number;
+  reply_confidence_max: number;
+  reply_reminder_business_hours: number;
+  ooo_default_days: number;
+  can_edit: boolean;
+}
```

- [ ] **Step 2: Client methods and structured error details.** Apply to `frontend/src/lib/api.ts`. `errorDetail` learns to read `{detail}` / `{message}` objects: the launch gate's 402 and an unconfigured provider's 409 both send one, and without this the screen showed "Payment Required" for "not enough credits to launch".

```diff
diff --git a/frontend/src/lib/api.ts b/frontend/src/lib/api.ts
index c1c270f..2deedf2 100644
--- a/frontend/src/lib/api.ts
+++ b/frontend/src/lib/api.ts
@@ -76,4 +76,21 @@ import type {
   ConnectedMailbox,
   MailboxProviderState,
+  EngagementStatus,
+  EngagementStep,
+  EngagementCampaign,
+  EngagementCampaignInput,
+  EngagementCandidate,
+  EnrollResult,
+  ReviewItem,
+  EngagementEnrollment,
+  CampaignEstimate,
+  SequenceTemplate,
+  TimelineEntry,
+  DeskQueueItem,
+  DeskItemDetail,
+  DeskScheduledItem,
+  DeskDecision,
+  ReplyCategory,
+  WorkspaceEngagementSettings,
   DoNotContactEntry,
   LedgerStatus,
@@ -1230,4 +1247,172 @@ export class ApiClient {
     return this.request<EngagementSetup>("/admin/engagement/setup", { signal });
   }
+
+  // ---- engagement: the engine switch and workspace reply settings ----
+  engagementStatus(signal?: AbortSignal) {
+    return this.request<EngagementStatus>("/engagement/settings/status", { signal });
+  }
+  engagementSettings(signal?: AbortSignal) {
+    return this.request<WorkspaceEngagementSettings>("/engagement/settings", { signal });
+  }
+  updateEngagementSettings(body: Partial<Omit<WorkspaceEngagementSettings, "can_edit">>) {
+    return this.request<WorkspaceEngagementSettings>("/engagement/settings", {
+      method: "PUT", body,
+    });
+  }
+
+  // ---- engagement: campaigns ----
+  listEngagementCampaigns(team = false, signal?: AbortSignal) {
+    return this.request<EngagementCampaign[]>("/engagement/campaigns", { query: { team }, signal });
+  }
+  createEngagementCampaign(body: EngagementCampaignInput) {
+    return this.request<EngagementCampaign>("/engagement/campaigns", { method: "POST", body });
+  }
+  getEngagementCampaign(id: string, signal?: AbortSignal) {
+    return this.request<EngagementCampaign>(`/engagement/campaigns/${id}`, { signal });
+  }
+  replaceCampaignSteps(id: string, steps: EngagementStep[]) {
+    return this.request<EngagementCampaign>(`/engagement/campaigns/${id}/steps`, {
+      method: "PUT", body: steps,
+    });
+  }
+  engagementCandidates(
+    params: { list_id?: string; q?: string; title?: string; seniority?: string },
+    signal?: AbortSignal,
+  ) {
+    return this.request<EngagementCandidate[]>("/engagement/candidates", { query: params, signal });
+  }
+  addCampaignContacts(id: string, contactIds: string[]) {
+    return this.request<EnrollResult>(`/engagement/campaigns/${id}/contacts`, {
+      method: "POST", body: { contact_ids: contactIds },
+    });
+  }
+  draftCampaign(id: string, limit = 25) {
+    return this.request<{
+      drafted: number; failed: number; errors: { contact_id: string; error: string }[];
+    }>(`/engagement/campaigns/${id}/draft`, { method: "POST", query: { limit } });
+  }
+  campaignReview(id: string, signal?: AbortSignal) {
+    return this.request<ReviewItem[]>(`/engagement/campaigns/${id}/review`, { signal });
+  }
+  approveCampaignMessage(messageId: string, body: { subject?: string; body?: string } = {}) {
+    return this.request<null>(`/engagement/messages/${messageId}/approve`, {
+      method: "POST", body,
+    });
+  }
+  regenerateCampaignMessage(messageId: string) {
+    return this.request<ReviewItem>(`/engagement/messages/${messageId}/regenerate`, {
+      method: "POST",
+    });
+  }
+  approveAllPassing(id: string) {
+    return this.request<{ approved: number }>(`/engagement/campaigns/${id}/approve-all`, {
+      method: "POST",
+    });
+  }
+  campaignEstimate(id: string, signal?: AbortSignal) {
+    return this.request<CampaignEstimate>(`/engagement/campaigns/${id}/estimate`, { signal });
+  }
+  launchCampaign(id: string) {
+    return this.request<{ status: string; estimate: CampaignEstimate }>(
+      `/engagement/campaigns/${id}/launch`, { method: "POST" },
+    );
+  }
+  campaignAction(id: string, action: "pause" | "resume" | "stop") {
+    return this.request<null>(`/engagement/campaigns/${id}/${action}`, { method: "POST" });
+  }
+  campaignEnrollments(id: string, signal?: AbortSignal) {
+    return this.request<EngagementEnrollment[]>(`/engagement/campaigns/${id}/enrollments`, {
+      signal,
+    });
+  }
+  moveEnrollment(enrollmentId: string, when: string) {
+    return this.request<null>(`/engagement/enrollments/${enrollmentId}/move`, {
+      method: "POST", body: { when },
+    });
+  }
+  enrollmentAction(
+    enrollmentId: string, action: "pause" | "resume" | "stop" | "send-now" | "remove",
+  ) {
+    return this.request<null>(`/engagement/enrollments/${enrollmentId}/${action}`, {
+      method: "POST",
+    });
+  }
+  engagementTimeline(
+    params: { contact_id?: string; account_id?: string }, signal?: AbortSignal,
+  ) {
+    return this.request<TimelineEntry[]>("/engagement/timeline", { query: params, signal });
+  }
+
+  // ---- engagement: sequence templates ----
+  listSequenceTemplates(signal?: AbortSignal) {
+    return this.request<SequenceTemplate[]>("/engagement/templates", { signal });
+  }
+  createSequenceTemplate(body: { name: string; description?: string; steps: EngagementStep[] }) {
+    return this.request<SequenceTemplate>("/engagement/templates", { method: "POST", body });
+  }
+  updateSequenceTemplate(
+    id: string, body: { name: string; description?: string; steps: EngagementStep[] },
+  ) {
+    return this.request<SequenceTemplate>(`/engagement/templates/${id}`, { method: "PUT", body });
+  }
+  deleteSequenceTemplate(id: string) {
+    return this.request<null>(`/engagement/templates/${id}`, { method: "DELETE" });
+  }
+
+  // ---- engagement: the reply desk ----
+  deskQueue(tab: "needs_action" | "handled", team = false, signal?: AbortSignal) {
+    return this.request<DeskQueueItem[]>("/engagement/desk", { query: { tab, team }, signal });
+  }
+  deskScheduled(team = false, signal?: AbortSignal) {
+    return this.request<DeskScheduledItem[]>("/engagement/desk/scheduled", {
+      query: { team }, signal,
+    });
+  }
+  deskItem(id: string, signal?: AbortSignal) {
+    return this.request<DeskItemDetail>(`/engagement/desk/${id}`, { signal });
+  }
+  deskDraft(id: string) {
+    return this.request<{ subject: string; body: string; quality_problems: string[] }>(
+      `/engagement/desk/${id}/draft`, { method: "POST" },
+    );
+  }
+  deskSend(id: string, body: { subject: string; body: string }) {
+    return this.request<{ outcome: string; reason: string; message_id: string }>(
+      `/engagement/desk/${id}/send`, { method: "POST", body },
+    );
+  }
+  deskSaveDraft(id: string, body: { subject: string; body: string }) {
+    return this.request<{ provider_draft_id: string }>(`/engagement/desk/${id}/save-draft`, {
+      method: "POST", body,
+    });
+  }
+  deskDecide(id: string, body: { decision: DeskDecision; reengage_on?: string; note?: string }) {
+    return this.request<null>(`/engagement/desk/${id}/decide`, { method: "POST", body });
+  }
+  deskCorrect(id: string, category: ReplyCategory) {
+    return this.request<null>(`/engagement/desk/${id}/correct`, {
+      method: "POST", body: { category },
+    });
+  }
+  deskAssign(id: string, userId: string) {
+    return this.request<null>(`/engagement/desk/${id}/assign`, {
+      method: "POST", query: { user_id: userId },
+    });
+  }
+  rescheduleScheduled(enrollmentId: string, when: string) {
+    return this.request<null>(`/engagement/desk/scheduled/${enrollmentId}/reschedule`, {
+      method: "POST", body: { when },
+    });
+  }
+  cancelScheduled(enrollmentId: string) {
+    return this.request<null>(`/engagement/desk/scheduled/${enrollmentId}/cancel`, {
+      method: "POST",
+    });
+  }
+  deskColleague(enrollmentId: string, action: "resume" | "stop") {
+    return this.request<null>(`/engagement/desk/colleagues/${enrollmentId}/${action}`, {
+      method: "POST",
+    });
+  }
   supportedProviders(signal?: AbortSignal) {
     return this.request<SupportedProvider[]>("/admin/provider-keys/providers", { signal });
@@ -2023,4 +2208,12 @@ export function errorDetail(data: unknown, statusText = ""): string {
   const detail = (data as { detail?: unknown }).detail;
   if (typeof detail === "string" && detail.trim()) return detail;
+  // A structured refusal: the launch gate sends `{detail, estimate}` and an unconfigured mailbox
+  // provider sends `{message, missing}`. Read the sentence out of it rather than falling back to
+  // the status text, which would show "Payment Required" for "not enough credits to launch".
+  if (detail && typeof detail === "object" && !Array.isArray(detail)) {
+    const inner = detail as { detail?: unknown; message?: unknown };
+    const sentence = typeof inner.detail === "string" ? inner.detail : inner.message;
+    if (typeof sentence === "string" && sentence.trim()) return sentence;
+  }
   if (Array.isArray(detail)) {
     const parts = detail
```

- [ ] **Step 3: The engine switch.** Create `frontend/src/app/EngagementContext.tsx`:

```tsx
import { createContext, useContext, type ReactNode } from "react";
import { Navigate } from "react-router-dom";
import { Skeleton } from "@/components/ui";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import type { EngagementStatus } from "@/lib/types";

/**
 * Whether the engagement engine is switched on, fetched once at the shell.
 *
 * `null` while unknown. This gate fails CLOSED for the new screens and OPEN for the old ones, the
 * opposite of `EntitlementsContext`, because the question is different: entitlements decide whether
 * to advertise a feature the server will police anyway, while this decides which of two engines
 * the workspace is on. Every new campaign, reply and template route answers 404 while the engine is
 * dark, so offering those links on a guess would put a rep on a page that cannot load; the old
 * Campaigns and Cadences keep working until the switch is confirmed on.
 */
const EngagementContext = createContext<EngagementStatus | null>(null);

export function useEngagementStatus(): EngagementStatus | null {
  return useContext(EngagementContext);
}

/** `true` / `false` once known (an unreadable status is `false`), `null` while loading. */
export function useEngineOn(): boolean | null {
  const status = useContext(EngagementContext);
  return status ? status.engine_on : null;
}

export function EngagementProvider({ children }: { children: ReactNode }) {
  const api = useApiClient();
  const state = useApi<EngagementStatus>((signal) => api.engagementStatus(signal), []);
  // An unreadable status reads as OFF: the old screens work either way, the new ones only when on.
  const value = state.data ?? (state.error ? { engine_on: false, can_manage: false } : null);
  return <EngagementContext.Provider value={value}>{children}</EngagementContext.Provider>;
}

/**
 * A new-engine page renders only once the engine is confirmed on. Off sends the reader to the
 * screen that works today (the old Campaigns), with `replace` so Back does not bounce them here.
 */
export function RequireEngine({ children, fallback = "/campaigns" }: {
  children: ReactNode;
  fallback?: string;
}) {
  const on = useEngineOn();
  if (on === null) return <Skeleton width="100%" height={240} />;
  if (!on) return <Navigate to={fallback} replace />;
  return <>{children}</>;
}
```

- [ ] **Step 4: Engine-aware navigation.** Apply to `frontend/src/app/nav.tsx`:

```diff
diff --git a/frontend/src/app/nav.tsx b/frontend/src/app/nav.tsx
index 2b224d8..5a1aa72 100644
--- a/frontend/src/app/nav.tsx
+++ b/frontend/src/app/nav.tsx
@@ -8,4 +8,5 @@ import {
   CreditCardIcon,
   DashboardIcon,
+  FileTextIcon,
   InboxIcon,
   ListIcon,
@@ -51,4 +52,11 @@ export interface NavItem {
    */
   capability?: string;
+  /**
+   * Which engagement engine the item belongs to. `"on"` items (the new Campaigns, Replies and
+   * Sequence templates) appear only once the engine is confirmed switched on; `"off"` items (the
+   * old Campaigns and Cadences) disappear at that moment. Before phase 15 removes the old engine,
+   * this is the whole of the switch-over in the navigation: one flag, two sets of pages, never both.
+   */
+  engine?: "on" | "off";
 }
 
@@ -66,4 +74,19 @@ export const NAV_ITEMS: NavItem[] = [
   { to: "/contacts", label: "Contacts", icon: <UsersIcon /> },
   { to: "/mailboxes", label: "My mailboxes", icon: <MailIcon />, capability: "module.outreach" },
+  // The engagement engine (spec §9). An SDR builds and runs their own campaigns and works their own
+  // replies, so none of these carries a `minRole`; the server scopes every read to the caller's
+  // mailboxes, and a manager sees the team through a toggle on the page.
+  {
+    to: "/engagement/campaigns", label: "Campaigns", icon: <SendIcon />,
+    capability: "module.campaigns", engine: "on",
+  },
+  {
+    to: "/engagement/replies", label: "Replies", icon: <MessageIcon />,
+    capability: "module.campaigns", engine: "on",
+  },
+  {
+    to: "/engagement/templates", label: "Sequence templates", icon: <FileTextIcon />,
+    capability: "module.cadences", engine: "on",
+  },
   { to: "/network", label: "Network", icon: <NetworkIcon />, capability: "module.network" },
   { to: "/lists", label: "Lists", icon: <ListIcon />, capability: "module.lists" },
@@ -87,9 +110,9 @@ export const NAV_ITEMS: NavItem[] = [
   {
     to: "/campaigns", label: "Campaigns", icon: <SendIcon />, minRole: "manager",
-    capability: "module.campaigns",
+    capability: "module.campaigns", engine: "off",
   },
   {
     to: "/cadences", label: "Cadences", icon: <MessageIcon />, minRole: "manager",
-    capability: "module.cadences",
+    capability: "module.cadences", engine: "off",
   },
   {
@@ -124,8 +147,17 @@ export const NAV_ITEMS: NavItem[] = [
 ];
 
-export function canSee(item: NavItem, role: Role | undefined, isPlatformAdmin = false): boolean {
+export function canSee(
+  item: NavItem,
+  role: Role | undefined,
+  isPlatformAdmin = false,
+  engineOn: boolean | null = null,
+): boolean {
   // A platform-only item is invisible to every workspace member, including an owner. The server
   // enforces this regardless — the nav entry only decides whether the link is offered.
   if (item.platformOnly) return isPlatformAdmin;
+  // New-engine pages only once the switch is CONFIRMED on; old-engine pages until it is. Unknown
+  // (still loading) keeps the old ones, which work either way.
+  if (item.engine === "on" && engineOn !== true) return false;
+  if (item.engine === "off" && engineOn === true) return false;
   if (!item.minRole) return true;
   if (!role) return false;
```

`components/layout/Sidebar.tsx`:

```diff
diff --git a/frontend/src/components/layout/Sidebar.tsx b/frontend/src/components/layout/Sidebar.tsx
index 51b80d7..1c411d0 100644
--- a/frontend/src/components/layout/Sidebar.tsx
+++ b/frontend/src/components/layout/Sidebar.tsx
@@ -5,4 +5,5 @@ import { usePlatformIdentity } from "@/app/RequirePlatformAdmin";
 import { useEntitlements, isLocked, switchNotice } from "@/app/EntitlementsContext";
 import { useAuth } from "@/app/AuthContext";
+import { useEngineOn } from "@/app/EngagementContext";
 import { Icons } from "@/components/ui";
 import styles from "./Sidebar.module.css";
@@ -26,4 +27,7 @@ export function Sidebar({ open, collapsed = false, onNavigate, onToggleCollapse
   // slow or failing billing endpoint never deletes the customer's navigation.
   const entitlements = useEntitlements();
+  // Which engagement engine the workspace is on: the new Campaigns, Replies and Sequence templates
+  // replace the old Campaigns and Cadences only once the switch is confirmed on.
+  const engineOn = useEngineOn();
 
   return (
@@ -56,5 +60,5 @@ export function Sidebar({ open, collapsed = false, onNavigate, onToggleCollapse
 
       <nav className={styles.nav} aria-label="Main navigation">
-        {NAV_ITEMS.filter((item) => canSee(item, role, isPlatformAdmin)).map((item) => {
+        {NAV_ITEMS.filter((item) => canSee(item, role, isPlatformAdmin, engineOn)).map((item) => {
           const notice = switchNotice(entitlements, item.capability);
           const state = navState(role, isLocked(entitlements, item.capability), notice !== null);
```

and `components/layout/AppShell.tsx` (wrap the shell in `EngagementProvider`; the rest of the diff is re-indentation):

```diff
diff --git a/frontend/src/components/layout/AppShell.tsx b/frontend/src/components/layout/AppShell.tsx
index 885dfaa..3c34259 100644
--- a/frontend/src/components/layout/AppShell.tsx
+++ b/frontend/src/components/layout/AppShell.tsx
@@ -5,4 +5,5 @@ import { useAuth } from "@/app/AuthContext";
 import { NAV_ITEMS } from "@/app/nav";
 import { EntitlementsProvider } from "@/app/EntitlementsContext";
+import { EngagementProvider } from "@/app/EngagementContext";
 import { ImpersonationBanner } from "./ImpersonationBanner";
 import { Sidebar } from "./Sidebar";
@@ -71,36 +72,38 @@ export function AppShell() {
     // same "is this in our plan?" question, and one fetch answers all of them.
     <EntitlementsProvider>
-      <ImpersonationBanner />
-      {/* Asks an owner or admin once, for workspaces created before the ledger existed. */}
-      <ConsentPrompt />
-      <div className={styles.shell}>
-        <a className="skip-link" href="#main">
-          Skip to content
-        </a>
+      <EngagementProvider>
+        <ImpersonationBanner />
+        {/* Asks an owner or admin once, for workspaces created before the ledger existed. */}
+        <ConsentPrompt />
+        <div className={styles.shell}>
+          <a className="skip-link" href="#main">
+            Skip to content
+          </a>
 
-        <Sidebar
-          open={drawerOpen}
-          collapsed={collapsed}
-          onNavigate={() => setDrawerOpen(false)}
-          onToggleCollapse={toggleCollapse}
-        />
-
-        {drawerOpen && (
-          <button
-            className={styles.scrim}
-            aria-label="Close menu"
-            onClick={() => setDrawerOpen(false)}
+          <Sidebar
+            open={drawerOpen}
+            collapsed={collapsed}
+            onNavigate={() => setDrawerOpen(false)}
+            onToggleCollapse={toggleCollapse}
           />
-        )}
 
-        <div className={styles.body}>
-          <Topbar title={title} onMenuClick={() => setDrawerOpen(true)} />
-          <main id="main" className={styles.main} tabIndex={-1}>
-            <div className={styles.container} key={tenantEpoch}>
-              <Outlet />
-            </div>
-          </main>
+          {drawerOpen && (
+            <button
+              className={styles.scrim}
+              aria-label="Close menu"
+              onClick={() => setDrawerOpen(false)}
+            />
+          )}
+
+          <div className={styles.body}>
+            <Topbar title={title} onMenuClick={() => setDrawerOpen(true)} />
+            <main id="main" className={styles.main} tabIndex={-1}>
+              <div className={styles.container} key={tenantEpoch}>
+                <Outlet />
+              </div>
+            </main>
+          </div>
         </div>
-      </div>
+      </EngagementProvider>
     </EntitlementsProvider>
   );
```

Routes are `/engagement/campaigns`, `/engagement/replies` and `/engagement/templates`, with no bare `/engagement` item, so no nav link is a prefix of another (a `NavLink` for `/engagement` would light up on every page beneath it) and `titleFor`'s longest-prefix match names each page.

- [ ] **Step 5: Run** `npm run typecheck` — expected PASS.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/lib frontend/src/app frontend/src/components/layout
git commit -m "feat(ui): engagement types, client, and navigation that follows the engine switch"
```

---

### Task 3: Shared components

**Files:** Create everything under `frontend/src/components/engagement/` listed below.

- [ ] **Step 1: The vocabulary** — `labels.ts`:

```ts
import type { BadgeTone } from "@/components/ui";
import type {
  EngagementCampaignStatus,
  EngagementEnrollmentStatus,
  ReplyCategory,
} from "@/lib/types";

/**
 * The words the engagement screens use for states the server names in snake_case. One map per
 * vocabulary, so the campaign list, the detail page and the reply desk cannot drift into three
 * spellings of "waiting for review".
 */

export const CAMPAIGN_STATUS: Record<EngagementCampaignStatus, { label: string; tone: BadgeTone }> = {
  draft: { label: "Setting up", tone: "neutral" },
  reviewing: { label: "In review", tone: "info" },
  active: { label: "Running", tone: "success" },
  paused: { label: "Paused", tone: "warning" },
  completed: { label: "Finished", tone: "neutral" },
};

export const ENROLLMENT_STATUS: Record<EngagementEnrollmentStatus, { label: string; tone: BadgeTone }> = {
  awaiting_review: { label: "Awaiting review", tone: "info" },
  active: { label: "In sequence", tone: "success" },
  paused: { label: "Paused", tone: "warning" },
  snoozed: { label: "Scheduled", tone: "accent" },
  stopped: { label: "Stopped", tone: "neutral" },
  completed: { label: "Finished", tone: "neutral" },
};

/** Why an enrollment is in its state, as a sentence fragment. Unknown reasons fall back to "". */
const REASONS: Record<string, string> = {
  replied: "they replied",
  colleague_replied: "a colleague replied",
  needs_decision: "their reply needs your decision",
  out_of_office: "out of office",
  later: "asked to hear back later",
  manual: "by hand",
  bounced: "the address bounced",
  unsubscribed: "they unsubscribed",
  declined: "they declined",
  out_of_credits: "the workspace ran out of credits",
  mailbox_disconnected: "the sending mailbox was disconnected",
};

export function reasonText(reason: string | null | undefined): string {
  if (!reason) return "";
  if (reason.startsWith("do_not_contact")) return "on the do-not-contact list";
  return REASONS[reason] ?? reason.replace(/_/g, " ");
}

export const CATEGORY: Record<ReplyCategory, { label: string; tone: BadgeTone }> = {
  interested: { label: "Interested", tone: "success" },
  question: { label: "Question", tone: "info" },
  referral: { label: "Referral", tone: "accent" },
  later: { label: "Later", tone: "neutral" },
  out_of_office: { label: "Out of office", tone: "neutral" },
  declined: { label: "Declined", tone: "warning" },
  unsubscribe: { label: "Unsubscribe", tone: "danger" },
  unclear: { label: "Needs a decision", tone: "warning" },
};

export const CATEGORY_OPTIONS = (Object.keys(CATEGORY) as ReplyCategory[]).map((value) => ({
  value,
  label: CATEGORY[value].label,
}));

/** Capability ids on the launch estimate, as the line a customer recognises on their bill. */
export const CAPABILITY_LINE: Record<string, string> = {
  "ai.email_draft": "Writing each email",
  "outreach.email_send": "Sending each email",
  "ai.reply_classify": "Reading each reply",
  "ai.reply_draft": "Suggesting each answer",
};

export const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"] as const;

/** A timestamp as the reader's own short date and time, or an em-less placeholder. */
export function when(iso: string | null | undefined): string {
  if (!iso) return "Not scheduled";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "Not scheduled";
  return d.toLocaleString([], {
    month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
  });
}

export function whenDay(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
}
```

- [ ] **Step 2: The steps editor** — `StepsEditor.tsx`. Its limits mirror `validate_steps`, which stays the authority: at most 12 steps, the first is always an email with no wait, later steps wait 0–60 business days, a set time is required only for "at a set time".

```tsx
import { Button, Field, IconButton, Icons, Input, Select, Textarea } from "@/components/ui";
import type { EngagementStep } from "@/lib/types";
import { WEEKDAYS } from "./labels";
import styles from "./StepsEditor.module.css";

/**
 * The touches of a sequence, in order (spec §8). Used by the campaign builder and by sequence
 * templates, so a template and a campaign are edited with the same words and the same limits.
 *
 * The limits mirror `validate_steps` on the server, which stays the authority: at most 12 steps, the
 * first is always an email and goes out when approved (so it has no delay), later steps wait 0–60
 * business days, and a set send time is required only when "at a set time" is chosen.
 */

export const MAX_STEPS = 12;

export function blankStep(first = false): EngagementStep {
  return {
    channel: "email",
    angle: "",
    timing_mode: "auto",
    delay_business_days: first ? 0 : 2,
    send_time_local: null,
    allowed_weekdays: [0, 1, 2, 3, 4],
  };
}

/** Why the steps cannot be saved as they are, or `null`. Mirrors the server's checks. */
export function stepsProblem(steps: EngagementStep[]): string | null {
  if (steps.length === 0) return "Add at least one step.";
  if (steps.length > MAX_STEPS) return `A sequence can have at most ${MAX_STEPS} steps.`;
  if (steps[0].channel !== "email") return "The first step must be an email.";
  for (const [i, s] of steps.entries()) {
    if (i > 0 && (s.delay_business_days < 0 || s.delay_business_days > 60)) {
      return `Step ${i + 1}: wait between 0 and 60 business days.`;
    }
    if (s.timing_mode === "manual" && !/^([01]\d|2[0-3]):[0-5]\d$/.test(s.send_time_local ?? "")) {
      return `Step ${i + 1}: choose the time it should go out.`;
    }
    if (s.allowed_weekdays.length === 0) return `Step ${i + 1}: allow at least one day.`;
  }
  return null;
}

export interface StepsEditorProps {
  steps: EngagementStep[];
  onChange: (steps: EngagementStep[]) => void;
  disabled?: boolean;
}

export function StepsEditor({ steps, onChange, disabled = false }: StepsEditorProps) {
  function update(index: number, patch: Partial<EngagementStep>) {
    onChange(steps.map((s, i) => (i === index ? { ...s, ...patch } : s)));
  }
  function move(index: number, by: -1 | 1) {
    const target = index + by;
    if (target < 0 || target >= steps.length) return;
    const next = [...steps];
    [next[index], next[target]] = [next[target], next[index]];
    // Whatever lands first becomes the opening email: it goes out on approval, with no wait.
    next[0] = { ...next[0], channel: "email", delay_business_days: 0 };
    onChange(next);
  }
  function remove(index: number) {
    const next = steps.filter((_, i) => i !== index);
    if (next.length) next[0] = { ...next[0], channel: "email", delay_business_days: 0 };
    onChange(next);
  }
  function toggleDay(index: number, day: number) {
    const current = steps[index].allowed_weekdays;
    const next = current.includes(day) ? current.filter((d) => d !== day) : [...current, day];
    update(index, { allowed_weekdays: next.sort((a, b) => a - b) });
  }

  return (
    <div className={styles.editor}>
      <ol className={styles.steps}>
        {steps.map((step, index) => {
          const first = index === 0;
          const label = `Step ${index + 1}`;
          return (
            <li key={index} className={styles.step}>
              <fieldset className={styles.fieldset} disabled={disabled}>
                <legend className={styles.legend}>
                  <span className={styles.number} aria-hidden="true">{index + 1}</span>
                  <span className={styles.srOnly}>{label}: </span>
                  {first ? "Opening email" : step.channel === "call" ? "Call" : "Follow-up email"}
                </legend>
                <div className={styles.reorder}>
                  <IconButton
                    label={`Move ${label} up`} size="sm" variant="ghost"
                    onClick={() => move(index, -1)} disabled={disabled || first}
                    icon={<Icons.ChevronLeftIcon className={styles.up} />}
                  />
                  <IconButton
                    label={`Move ${label} down`} size="sm" variant="ghost"
                    onClick={() => move(index, 1)} disabled={disabled || index === steps.length - 1}
                    icon={<Icons.ChevronRightIcon className={styles.down} />}
                  />
                  <IconButton
                    label={`Remove ${label}`} size="sm" variant="ghost"
                    onClick={() => remove(index)} disabled={disabled || steps.length === 1}
                    icon={<Icons.TrashIcon />}
                  />
                </div>

                <div className={styles.grid}>
                  {!first && (
                    <Field label="Channel">
                      <Select
                        value={step.channel}
                        onChange={(e) => update(index, { channel: e.target.value as EngagementStep["channel"] })}
                        options={[
                          { value: "email", label: "Email" },
                          { value: "call", label: "Call (a task for you)" },
                        ]}
                      />
                    </Field>
                  )}
                  {!first && (
                    <Field label="Wait" hint="Business days after the previous step.">
                      <Input
                        type="number" inputMode="numeric" min={0} max={60}
                        value={step.delay_business_days}
                        onChange={(e) => update(index, {
                          delay_business_days: Math.max(0, Math.min(60, Number(e.target.value) || 0)),
                        })}
                      />
                    </Field>
                  )}
                  <Field
                    label="When"
                    hint={first
                      ? "Opening emails go when you approve them, or at the time you schedule."
                      : "The best time is the contact's working morning."}
                  >
                    <Select
                      value={step.timing_mode}
                      onChange={(e) => {
                        const mode = e.target.value as EngagementStep["timing_mode"];
                        update(index, {
                          timing_mode: mode,
                          send_time_local: mode === "manual" ? step.send_time_local ?? "09:30" : null,
                        });
                      }}
                      options={[
                        { value: "auto", label: "At the best time" },
                        { value: "manual", label: "At a set time" },
                      ]}
                    />
                  </Field>
                  {step.timing_mode === "manual" && (
                    <Field label="Send at" hint="In the contact's own timezone.">
                      <Input
                        type="time" value={step.send_time_local ?? ""}
                        onChange={(e) => update(index, { send_time_local: e.target.value || null })}
                      />
                    </Field>
                  )}
                </div>

                {step.channel === "email" && (
                  <Field
                    label="What this email is about"
                    hint="The AI writes from this and from what it knows about the person. Leave it blank to let it choose the angle."
                  >
                    <Textarea
                      rows={2} maxLength={500} value={step.angle}
                      placeholder={first ? "Open on their recent funding round" : "A different reason to talk"}
                      onChange={(e) => update(index, { angle: e.target.value })}
                    />
                  </Field>
                )}

                <div className={styles.days} role="group" aria-label={`${label}: days it may go out`}>
                  <span className={styles.daysLabel}>Only on</span>
                  {WEEKDAYS.map((day, d) => {
                    const on = step.allowed_weekdays.includes(d);
                    return (
                      <button
                        key={day} type="button" className={styles.day} aria-pressed={on}
                        onClick={() => toggleDay(index, d)} disabled={disabled}
                      >
                        {day}
                      </button>
                    );
                  })}
                </div>
              </fieldset>
            </li>
          );
        })}
      </ol>
      <Button
        variant="secondary" iconLeft={<Icons.PlusIcon />}
        onClick={() => onChange([...steps, blankStep(steps.length === 0)])}
        disabled={disabled || steps.length >= MAX_STEPS}
      >
        Add a step
      </Button>
    </div>
  );
}
```

`StepsEditor.module.css`:

```css
.editor {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: var(--space-4);
}

.steps {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  width: 100%;
  margin: 0;
  padding: 0;
  list-style: none;
}

.step {
  position: relative;
}

.fieldset {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
  margin: 0;
  padding: var(--space-4) var(--space-5) var(--space-5);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background: var(--surface);
  min-width: 0;
}

.legend {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  padding: 0;
  font-size: var(--text-base);
  font-weight: var(--weight-semibold);
  color: var(--text);
  float: left;
}

.number {
  display: inline-grid;
  place-items: center;
  width: 24px;
  height: 24px;
  border-radius: var(--radius-full);
  background: var(--accent-quiet);
  color: var(--accent);
  font-size: var(--text-xs);
  font-variant-numeric: tabular-nums;
}

.reorder {
  position: absolute;
  top: var(--space-2);
  right: var(--space-3);
  display: flex;
  gap: var(--space-1);
}

.up,
.down {
  transform: rotate(90deg);
}

.grid {
  clear: both;
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(11rem, 1fr));
  gap: var(--space-3) var(--space-4);
}

.days {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-1);
}

.daysLabel {
  margin-right: var(--space-2);
  font-size: var(--text-sm);
  color: var(--text-muted);
}

.day {
  min-width: 44px;
  min-height: 36px;
  padding: 0 var(--space-2);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface-2);
  color: var(--text-muted);
  font: inherit;
  font-size: var(--text-sm);
  cursor: pointer;
  transition: background var(--dur-fast) var(--ease-out), color var(--dur-fast) var(--ease-out),
    border-color var(--dur-fast) var(--ease-out);
}

.day:hover:not(:disabled) {
  border-color: var(--border-strong);
  color: var(--text);
}

.day[aria-pressed="true"] {
  border-color: var(--accent);
  background: var(--accent-quiet);
  color: var(--text);
  font-weight: var(--weight-medium);
}

.day:focus-visible {
  outline: 2px solid var(--ring);
  outline-offset: 2px;
}

.day:disabled {
  cursor: not-allowed;
  opacity: 0.6;
}

.srOnly {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip: rect(0 0 0 0);
  white-space: nowrap;
}

@media (max-width: 640px) {
  .fieldset {
    padding: var(--space-4);
  }

  .reorder {
    position: static;
    justify-content: flex-end;
  }
}
```

- [ ] **Step 3: The cost and the volume warning** — `CreditEstimate.tsx`:

```tsx
import type { CampaignEstimate } from "@/lib/types";
import { CAPABILITY_LINE } from "./labels";
import styles from "./CreditEstimate.module.css";

/**
 * What a campaign can cost before it launches (spec §10, D18).
 *
 * The WORST case is the gate and is shown first: every contact receives every email and replies
 * once. The likely cost is information only. Prices come from the live rate cards through the same
 * code the meter charges with, so the number here is the number spent.
 */

function credits(n: number): string {
  return n.toLocaleString(undefined, { maximumFractionDigits: 1 });
}

export function CreditEstimate({ estimate }: { estimate: CampaignEstimate }) {
  const lines = Object.entries(estimate.per_capability).filter(([, l]) => l.units > 0);
  return (
    <section className={styles.estimate} aria-labelledby="estimate-title">
      <h3 id="estimate-title" className={styles.title}>What this campaign can cost</h3>
      <dl className={styles.figures}>
        <div className={styles.figure}>
          <dt>Most it can cost</dt>
          <dd className={styles.worst}>{credits(estimate.worst_credits)} credits</dd>
          <dd className={styles.note}>
            {estimate.contacts} {estimate.contacts === 1 ? "contact" : "contacts"} ×{" "}
            {estimate.email_steps} {estimate.email_steps === 1 ? "email" : "emails"}, each
            replying once
          </dd>
        </div>
        <div className={styles.figure}>
          <dt>Likely</dt>
          <dd className={styles.likely}>{credits(estimate.likely_credits)} credits</dd>
          <dd className={styles.note}>If 15% reply and follow-ups stop when they do</dd>
        </div>
        <div className={styles.figure}>
          <dt>Your balance</dt>
          <dd className={styles.likely}>{credits(estimate.balance)} credits</dd>
        </div>
      </dl>

      {!estimate.gate_applies ? (
        <p className={styles.status} role="status">
          Credits are not checked before launch on this workspace's plan.
        </p>
      ) : estimate.covered ? (
        <p className={`${styles.status} ${styles.ok}`} role="status">
          Your balance covers the most this campaign can cost.
        </p>
      ) : (
        <p className={`${styles.status} ${styles.short}`} role="alert">
          Short by {credits(estimate.shortfall)} credits. Launching waits until your balance covers
          the most it can cost, so no one is left half-way through a sequence.
        </p>
      )}

      {lines.length > 0 && (
        <table className={styles.table}>
          <caption className={styles.caption}>Where the most it can cost comes from</caption>
          <thead>
            <tr>
              <th scope="col">Action</th>
              <th scope="col" className={styles.num}>Times</th>
              <th scope="col" className={styles.num}>Credits</th>
            </tr>
          </thead>
          <tbody>
            {lines.map(([id, line]) => (
              <tr key={id}>
                <th scope="row">{CAPABILITY_LINE[id] ?? id}</th>
                <td className={styles.num}>{line.units.toLocaleString()}</td>
                <td className={styles.num}>{credits(line.credits)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
```

`CreditEstimate.module.css`:

```css
.estimate {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.title {
  margin: 0;
  font-size: var(--text-md);
  font-weight: var(--weight-semibold);
  color: var(--text);
}

.figures {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(12rem, 1fr));
  gap: var(--space-4);
  margin: 0;
}

.figure {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.figure dt {
  font-size: var(--text-sm);
  color: var(--text-muted);
}

.figure dd {
  margin: 0;
}

.worst {
  font-size: var(--text-xl);
  font-weight: var(--weight-semibold);
  color: var(--text);
  font-variant-numeric: tabular-nums;
}

.likely {
  font-size: var(--text-lg);
  color: var(--text);
  font-variant-numeric: tabular-nums;
}

.note {
  font-size: var(--text-sm);
  color: var(--text-muted);
  line-height: var(--leading);
}

.status {
  margin: 0;
  padding: var(--space-3) var(--space-4);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface-2);
  color: var(--text);
  font-size: var(--text-sm);
  line-height: var(--leading);
}

.ok {
  border-color: var(--success);
  background: var(--success-quiet);
}

.short {
  border-color: var(--danger);
  background: var(--danger-quiet);
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
```

`VolumeWarning.tsx` and its CSS module:

```tsx
import styles from "./VolumeWarning.module.css";

/**
 * More than 50 sends a day from one mailbox is where providers start to notice (D10). A warning the
 * SDR reads before launching, never a block: the server keeps sending at the paced rate either way.
 */
export function VolumeWarning({ message }: { message: string }) {
  if (!message) return null;
  return (
    <p className={styles.volume} role="status">
      {message}
    </p>
  );
}
```

```css
.volume {
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

- [ ] **Step 4: The conversation** — `ConversationTimeline.tsx`. Direction is carried by alignment, a label and the surface, never colour alone.

```tsx
import type { ReactNode } from "react";
import { Badge } from "@/components/ui";
import type { ReplyCategory } from "@/lib/types";
import { CATEGORY, when } from "./labels";
import styles from "./ConversationTimeline.module.css";

/**
 * A conversation, oldest first: what the SDR sent and what came back.
 *
 * One component for two readers. The reply desk passes whole bodies for the thread it is answering;
 * the contact and account pages pass previews across every campaign. Direction is carried by
 * alignment, a label and the surface, never by colour alone.
 */

export interface TimelineMessage {
  id: string;
  direction: "out" | "in";
  subject: string;
  body: string;
  at: string | null;
  /** Who, when the timeline spans several people (an account). */
  who?: string;
  /** Where it came from: a campaign name, or "One-off email". */
  context?: string;
  category?: ReplyCategory | null;
}

export interface ConversationTimelineProps {
  messages: TimelineMessage[];
  /** Shown when there is nothing yet. */
  empty?: ReactNode;
  /** Clamp long bodies to a few lines (previews) instead of showing them whole. */
  clamp?: boolean;
}

export function ConversationTimeline({ messages, empty, clamp = false }: ConversationTimelineProps) {
  if (messages.length === 0) {
    return <p className={styles.empty}>{empty ?? "Nothing has been sent or received yet."}</p>;
  }
  return (
    <ol className={styles.timeline}>
      {messages.map((m) => {
        const outbound = m.direction === "out";
        const category = m.category ? CATEGORY[m.category] : null;
        return (
          <li key={m.id} className={outbound ? styles.out : styles.in}>
            <article className={styles.message} aria-label={outbound ? "Sent" : "Received"}>
              <header className={styles.meta}>
                <span className={styles.direction}>{outbound ? "You sent" : m.who ? `${m.who} replied` : "They replied"}</span>
                <time dateTime={m.at ?? undefined} className={styles.time}>{when(m.at)}</time>
                {category && <Badge tone={category.tone}>{category.label}</Badge>}
              </header>
              {m.subject && <p className={styles.subject}>{m.subject}</p>}
              <p className={clamp ? `${styles.body} ${styles.clamped}` : styles.body}>{m.body}</p>
              {m.context && <p className={styles.context}>{m.context}</p>}
            </article>
          </li>
        );
      })}
    </ol>
  );
}
```

```css
.timeline {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  margin: 0;
  padding: 0;
  list-style: none;
}

.out,
.in {
  display: flex;
}

.out {
  justify-content: flex-end;
}

.in {
  justify-content: flex-start;
}

.message {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  max-width: min(100%, 38rem);
  padding: var(--space-3) var(--space-4);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
}

.out .message {
  background: var(--accent-quiet);
  border-color: transparent;
}

.in .message {
  background: var(--surface);
}

.meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
}

.direction {
  font-size: var(--text-sm);
  font-weight: var(--weight-semibold);
  color: var(--text);
}

.time {
  font-size: var(--text-xs);
  color: var(--text-muted);
  font-variant-numeric: tabular-nums;
}

.subject {
  margin: 0;
  font-size: var(--text-sm);
  font-weight: var(--weight-medium);
  color: var(--text);
}

.body {
  margin: 0;
  font-size: var(--text-base);
  line-height: var(--leading);
  color: var(--text);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.clamped {
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.context {
  margin: 0;
  font-size: var(--text-xs);
  color: var(--text-muted);
}

.empty {
  margin: 0;
  font-size: var(--text-sm);
  color: var(--text-muted);
}
```

`AccountConversations.tsx`, the account page's reader of it:

```tsx
import { Card, ErrorState, Skeleton } from "@/components/ui";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import type { ReplyCategory, TimelineEntry } from "@/lib/types";
import { ConversationTimeline } from "./ConversationTimeline";

/**
 * Every email to and from the people at an account (or one contact), across campaigns and one-off
 * sends (spec §9, "a cross-campaign conversation timeline"). Previews, oldest first; the full thread
 * of a reply is on the reply desk.
 */
export function AccountConversations({ accountId, contactId }: { accountId?: string; contactId?: string }) {
  const api = useApiClient();
  const timeline = useApi<TimelineEntry[]>(
    (s) => api.engagementTimeline({ account_id: accountId, contact_id: contactId }, s),
    [accountId, contactId],
  );

  if (timeline.error) {
    return <ErrorState title="Couldn't load the emails" message={timeline.error.detail} onRetry={timeline.refetch} />;
  }
  if (!timeline.data) return <Skeleton width="100%" height={240} />;
  return (
    <Card padding="lg">
      <ConversationTimeline
        clamp
        empty="No emails have been sent to anyone here from NEXUS yet."
        messages={timeline.data.map((e) => ({
          id: e.message_id,
          direction: e.direction,
          subject: e.subject,
          body: e.preview,
          at: e.at,
          who: e.contact_name || undefined,
          context: e.direction === "out"
            ? `${e.contact_name ? `To ${e.contact_name} · ` : ""}${e.campaign_name || "One-off email"}`
            : e.campaign_name || undefined,
          category: (e.category as ReplyCategory | null) ?? null,
        }))}
      />
    </Card>
  );
}
```

- [ ] **Step 5: Choosing people** — `ContactPicker.tsx`. It debounces a STRING of the filters: an object literal is a new value every render, which would re-arm the timer forever and refetch every 300ms.

```tsx
import { useEffect, useMemo, useState } from "react";
import {
  Badge, Button, DataTable, EmptyState, ErrorState, Field, Icons, Input, Select,
} from "@/components/ui";
import type { Column } from "@/components/ui";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import type { EngagementCandidate, ProspectList } from "@/lib/types";
import styles from "./ContactPicker.module.css";

/**
 * Choose who a campaign emails (spec §9, step 1): from a saved list, by title and seniority, or by
 * search. A saved list holds accounts, so the server expands it to the people there; this screen
 * only chooses among them.
 *
 * People on the do-not-contact list are shown and cannot be ticked, so an SDR who expected to see
 * someone learns why they are missing rather than wondering.
 */

const SENIORITY = [
  { value: "", label: "Any seniority" },
  { value: "c_level", label: "C-level" },
  { value: "vp", label: "VP" },
  { value: "director", label: "Director" },
  { value: "manager", label: "Manager" },
  { value: "ic", label: "Individual contributor" },
];

function useDebounced<T>(value: T, ms = 300): T {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const id = window.setTimeout(() => setSettled(value), ms);
    return () => window.clearTimeout(id);
  }, [value, ms]);
  return settled;
}

export interface ContactPickerProps {
  /** Contacts already in the campaign: shown as added, not offered again. */
  enrolledIds: ReadonlySet<string>;
  onAdd: (contactIds: string[]) => Promise<void>;
}

export function ContactPicker({ enrolledIds, onAdd }: ContactPickerProps) {
  const api = useApiClient();
  const [listId, setListId] = useState("");
  const [q, setQ] = useState("");
  const [title, setTitle] = useState("");
  const [seniority, setSeniority] = useState("");
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [adding, setAdding] = useState(false);
  // Debounce a STRING: an object literal is a new value every render, so the timer would re-arm
  // forever and refetch every 300ms. Equal strings let React skip the update.
  const settled = useDebounced(JSON.stringify({ listId, q, title, seniority }));
  const query = useMemo(
    () => JSON.parse(settled) as { listId: string; q: string; title: string; seniority: string },
    [settled],
  );

  const lists = useApi<ProspectList[]>((s) => api.listSavedLists(s), []);
  const found = useApi<EngagementCandidate[]>(
    (s) => api.engagementCandidates({
      list_id: query.listId || undefined, q: query.q || undefined,
      title: query.title || undefined, seniority: query.seniority || undefined,
    }, s),
    [query.listId, query.q, query.title, query.seniority],
  );

  const rows = found.data ?? [];
  const selectable = useMemo(
    () => rows.filter((r) => !r.blocked && !enrolledIds.has(r.contact_id)),
    [rows, enrolledIds],
  );
  const allPicked = selectable.length > 0 && selectable.every((r) => picked.has(r.contact_id));

  function toggle(id: string) {
    setPicked((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }
  function toggleAll() {
    setPicked((prev) => {
      const next = new Set(prev);
      for (const r of selectable) {
        if (allPicked) next.delete(r.contact_id);
        else next.add(r.contact_id);
      }
      return next;
    });
  }
  async function add() {
    setAdding(true);
    try {
      await onAdd([...picked]);
      setPicked(new Set());
    } finally {
      setAdding(false);
    }
  }

  const columns: Column<EngagementCandidate>[] = [
    {
      key: "pick",
      width: "44px",
      header: (
        <input
          type="checkbox" className={styles.check} aria-label="Select everyone shown"
          checked={allPicked} onChange={toggleAll} disabled={selectable.length === 0}
        />
      ),
      render: (r) => {
        const added = enrolledIds.has(r.contact_id);
        return (
          <input
            type="checkbox" className={styles.check}
            aria-label={`Select ${r.full_name}`}
            checked={added || picked.has(r.contact_id)}
            disabled={r.blocked || added}
            onChange={() => toggle(r.contact_id)}
          />
        );
      },
    },
    {
      key: "person",
      header: "Person",
      render: (r) => (
        <div className={styles.person}>
          <span className={styles.name}>{r.full_name}</span>
          {r.title && <span className={styles.sub}>{r.title}</span>}
        </div>
      ),
    },
    { key: "account", header: "Account", render: (r) => r.account_name, hideOnMobile: true },
    {
      key: "email",
      header: "Email",
      hideOnMobile: true,
      render: (r) => (
        <div className={styles.person}>
          <span className={styles.sub}>{r.email}</span>
          {r.blocked ? (
            <Badge tone="danger">Do not contact</Badge>
          ) : enrolledIds.has(r.contact_id) ? (
            <Badge tone="accent">Added</Badge>
          ) : r.email_status === "invalid" ? (
            <Badge tone="warning">Address invalid</Badge>
          ) : null}
        </div>
      ),
    },
  ];

  const listOptions = [
    { value: "", label: "All contacts" },
    ...(lists.data ?? []).map((l) => ({ value: l.id, label: `${l.name} (${l.accounts} accounts)` })),
  ];

  return (
    <div className={styles.picker}>
      <div className={styles.filters}>
        <Field label="From a saved list">
          <Select value={listId} onChange={(e) => setListId(e.target.value)} options={listOptions} />
        </Field>
        <Field label="Title contains" hint="Separate several with commas.">
          <Input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="VP, Head of" />
        </Field>
        <Field label="Seniority">
          <Select value={seniority} onChange={(e) => setSeniority(e.target.value)} options={SENIORITY} />
        </Field>
        <Field label="Search">
          <Input
            type="search" value={q} onChange={(e) => setQ(e.target.value)}
            placeholder="Name, email or company"
          />
        </Field>
      </div>

      {found.error ? (
        <ErrorState title="Couldn't load contacts" message={found.error.detail} onRetry={found.refetch} />
      ) : (
        <DataTable
          columns={columns}
          rows={rows}
          getRowKey={(r) => r.contact_id}
          loading={found.loading && !found.data}
          density="compact"
          caption="People who can be added to this campaign"
          empty={
            <EmptyState
              compact
              icon={<Icons.UsersIcon />}
              title="No one matches"
              description="Only contacts with an email address can be added. Try a wider title or another list."
            />
          }
        />
      )}

      <div className={styles.footer}>
        <span className={styles.count} aria-live="polite">
          {picked.size === 0 ? "Nobody selected" : `${picked.size} selected`}
        </span>
        <Button onClick={add} disabled={picked.size === 0} loading={adding} iconLeft={<Icons.PlusIcon />}>
          Add {picked.size || ""} to the campaign
        </Button>
      </div>
    </div>
  );
}
```

```css
.picker {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.filters {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(12rem, 1fr));
  gap: var(--space-3) var(--space-4);
}

.person {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 2px;
  min-width: 0;
}

.name {
  font-weight: var(--weight-medium);
  color: var(--text);
}

.sub {
  font-size: var(--text-sm);
  color: var(--text-muted);
  overflow-wrap: anywhere;
}

.check {
  width: 18px;
  height: 18px;
  accent-color: var(--accent);
  cursor: pointer;
}

.check:disabled {
  cursor: not-allowed;
}

.footer {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
}

.count {
  font-size: var(--text-sm);
  color: var(--text-muted);
}
```

- [ ] **Step 6: Run** `npm run typecheck` — expected PASS.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/components/engagement
git commit -m "feat(ui): engagement components — steps, cost, conversation, contact picker"
```

---

### Task 4: The screens

**Files:** Create the pages below; modify `frontend/src/App.tsx`.

- [ ] **Step 1: The shared page stylesheet** — `pages/engagement/Engagement.module.css`. At 1024px and below the reply desk shows one pane at a time: the list, or the reply that was opened, with a back button.

```css
/* Shared by the engagement pages: campaigns, the builder, a campaign, templates and replies. */

.page {
  display: flex;
  flex-direction: column;
  gap: var(--space-5);
  min-width: 0;
}

.stack {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
  min-width: 0;
}

/* ---- text ---- */

.muted {
  margin: 0;
  font-size: var(--text-sm);
  color: var(--text-muted);
  line-height: var(--leading);
}

.num {
  font-variant-numeric: tabular-nums;
}

.time {
  font-size: var(--text-xs);
  color: var(--text-muted);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.srOnly {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip: rect(0 0 0 0);
  white-space: nowrap;
}

.bodyText {
  margin: 0;
  max-width: 72ch;
  font-size: var(--text-base);
  line-height: var(--leading);
  color: var(--text);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.subjectLine {
  margin: 0;
  font-weight: var(--weight-semibold);
  color: var(--text);
}

.subhead {
  margin: 0;
  font-size: var(--text-sm);
  font-weight: var(--weight-semibold);
  color: var(--text);
}

.plainList {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  margin: 0;
  padding-left: var(--space-5);
  font-size: var(--text-sm);
  color: var(--text);
  line-height: var(--leading);
}

/* ---- links that act as navigation ---- */

.rowLink {
  font-weight: var(--weight-medium);
  color: var(--text);
  text-decoration: none;
}

.rowLink:hover {
  color: var(--accent);
  text-decoration: underline;
}

.inlineLink {
  color: var(--accent);
  text-decoration: underline;
  text-underline-offset: 2px;
}

.buttonLink {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  height: 38px;
  padding: 0 var(--space-4);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-sm);
  background: var(--surface-3);
  color: var(--text);
  font-size: var(--text-base);
  font-weight: var(--weight-semibold);
  text-decoration: none;
  white-space: nowrap;
  transition: border-color var(--dur-fast) var(--ease-out);
}

.buttonLink:hover {
  border-color: var(--accent);
}

.buttonLink:focus-visible,
.rowLink:focus-visible,
.inlineLink:focus-visible,
.back:focus-visible,
.backButton:focus-visible {
  outline: 2px solid var(--ring);
  outline-offset: 2px;
}

.back {
  display: inline-flex;
  align-items: center;
  gap: var(--space-1);
  align-self: flex-start;
  min-height: 32px;
  font-size: var(--text-sm);
  color: var(--text-muted);
  text-decoration: none;
}

.back:hover {
  color: var(--text);
}

.back :global(svg) {
  width: 16px;
  height: 16px;
}

/* ---- notices and form chrome ---- */

.notice {
  margin: 0;
  padding: var(--space-3) var(--space-4);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface-2);
  color: var(--text);
  font-size: var(--text-sm);
  line-height: var(--leading);
}

.formError {
  margin: 0;
  padding: var(--space-3) var(--space-4);
  border: 1px solid var(--danger);
  border-radius: var(--radius);
  background: var(--danger-quiet);
  color: var(--text);
  font-size: var(--text-sm);
}

.form {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.section {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.sectionHead {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
}

.sectionTitle {
  margin: 0;
  font-size: var(--text-md);
  font-weight: var(--weight-semibold);
  color: var(--text);
}

.fields {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(16rem, 1fr));
  gap: var(--space-3) var(--space-4);
}

/* A fieldset used only to disable a group of controls at once. */
.bare {
  min-width: 0;
  margin: 0;
  padding: 0;
  border: 0;
}

.checkRow {
  display: flex;
  align-items: flex-start;
  gap: var(--space-3);
  font-size: var(--text-base);
  color: var(--text);
  cursor: pointer;
}

.checkRow input {
  width: 18px;
  height: 18px;
  margin-top: 2px;
  accent-color: var(--accent);
}

.formActions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: flex-end;
  gap: var(--space-2);
}

.rowActions {
  display: inline-flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: var(--space-1);
}

/* ---- tables and people ---- */

.statusCell {
  display: inline-flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
}

.person {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.personName {
  margin: 0;
  font-weight: var(--weight-medium);
  color: var(--text);
  overflow-wrap: anywhere;
}

.headerMeta {
  display: inline-flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-3);
  font-size: var(--text-sm);
  color: var(--text-muted);
}

.addResult {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-3) var(--space-4);
  border: 1px solid var(--warning);
  border-radius: var(--radius);
  background: var(--warning-quiet);
}

/* ---- steps, read-only ---- */

.stepList {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  margin: 0;
  padding: 0;
  list-style: none;
}

.stepRow {
  display: flex;
  align-items: flex-start;
  gap: var(--space-3);
  font-size: var(--text-sm);
  color: var(--text);
  line-height: var(--leading);
}

.stepNumber {
  display: inline-grid;
  flex-shrink: 0;
  place-items: center;
  width: 24px;
  height: 24px;
  border-radius: var(--radius-full);
  background: var(--accent-quiet);
  color: var(--accent);
  font-size: var(--text-xs);
  font-weight: var(--weight-semibold);
  font-variant-numeric: tabular-nums;
}

/* ---- review queue ---- */

.toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
}

.summary {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-4);
  margin: 0;
  font-size: var(--text-sm);
  color: var(--text-muted);
}

.summary strong {
  color: var(--text);
  font-variant-numeric: tabular-nums;
}

.toolbarActions {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
}

.reviewList {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  margin: 0;
  padding: 0;
  list-style: none;
}

.reviewCard {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  padding: var(--space-5);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background: var(--surface);
}

.reviewHead {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-3);
}

.problems {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  margin: 0;
  padding: var(--space-3) var(--space-4);
  border: 1px solid var(--warning);
  border-radius: var(--radius);
  background: var(--warning-quiet);
  list-style: none;
}

.problem {
  display: flex;
  align-items: flex-start;
  gap: var(--space-2);
  max-width: 72ch;
  font-size: var(--text-sm);
  line-height: var(--leading);
  color: var(--text);
}

.problemIcon {
  flex-shrink: 0;
  width: 16px;
  height: 16px;
  margin-top: 2px;
  color: var(--warning);
}

.draft {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.draftRead {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-3) var(--space-4);
  border-radius: var(--radius);
  background: var(--surface-2);
}

.cardActions {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
}

/* ---- templates ---- */

.templateList {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  margin: 0;
  padding: 0;
  list-style: none;
}

.templateCard {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.templateHead {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-3);
}

.templateName {
  margin: 0 0 var(--space-1);
  font-size: var(--text-md);
  font-weight: var(--weight-semibold);
  color: var(--text);
}

/* ---- the reply desk ---- */

.deskGrid {
  display: grid;
  grid-template-columns: minmax(16rem, 22rem) minmax(0, 1fr);
  gap: var(--space-5);
  align-items: start;
}

.deskList {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  margin: 0;
  padding: 0;
  list-style: none;
  position: sticky;
  top: calc(var(--topbar-h) + var(--space-4));
  max-height: calc(100vh - var(--topbar-h) - var(--space-8));
  overflow-y: auto;
}

.deskItem {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  width: 100%;
  padding: var(--space-3) var(--space-4);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
  color: inherit;
  font: inherit;
  text-align: left;
  cursor: pointer;
  transition: border-color var(--dur-fast) var(--ease-out), background var(--dur-fast) var(--ease-out);
}

.deskItem:hover {
  border-color: var(--border-strong);
}

.deskItem[aria-current="true"] {
  border-color: var(--accent);
  background: var(--accent-quiet);
}

.deskItem:focus-visible {
  outline: 2px solid var(--ring);
  outline-offset: 2px;
}

.deskItemHead {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--space-2);
}

.deskItemFoot {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-1);
}

.preview {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  font-size: var(--text-sm);
  color: var(--text-muted);
  line-height: var(--leading);
}

.deskDetail {
  min-width: 0;
}

.backButton {
  display: none;
  align-items: center;
  gap: var(--space-1);
  min-height: 44px;
  margin-bottom: var(--space-2);
  padding: 0;
  border: 0;
  background: none;
  color: var(--text-muted);
  font: inherit;
  font-size: var(--text-sm);
  cursor: pointer;
}

.backButton :global(svg) {
  width: 16px;
  height: 16px;
}

.detail {
  display: flex;
  flex-direction: column;
  gap: var(--space-5);
  padding: var(--space-5);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background: var(--surface);
}

.detailHead {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-3);
}

.detailTitle {
  margin: 0;
  font-size: var(--text-lg);
  font-weight: var(--weight-semibold);
  color: var(--text);
}

.readAs {
  display: inline-flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
}

.correctRow {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(14rem, 18rem));
  gap: var(--space-3) var(--space-4);
}

.conversation {
  padding: var(--space-4);
  border-radius: var(--radius);
  background: var(--surface-2);
}

.answer,
.decisions,
.colleagues {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  padding-top: var(--space-4);
  border-top: 1px solid var(--border);
}

.colleagueRow {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
}

.laterRow {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-end;
  gap: var(--space-3);
}

.decisionButtons {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
}

/* One pane at a time below the large breakpoint: the list, or the reply that was opened. */
@media (max-width: 1024px) {
  .deskGrid {
    grid-template-columns: minmax(0, 1fr);
  }

  .deskList {
    position: static;
    max-height: none;
  }

  .deskGrid[data-open="detail"] .deskList,
  .deskGrid[data-open="list"] .deskDetail {
    display: none;
  }

  .backButton {
    display: inline-flex;
  }
}

@media (max-width: 640px) {
  .detail,
  .reviewCard {
    padding: var(--space-4);
  }
}
```

- [ ] **Step 2: Campaigns** — `CampaignsPage.tsx`:

```tsx
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { PageHeader } from "@/components/layout/PageHeader";
import { Badge, Button, DataTable, EmptyState, ErrorState, Icons } from "@/components/ui";
import type { Column } from "@/components/ui";
import { CAMPAIGN_STATUS, reasonText, whenDay } from "@/components/engagement/labels";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { useEngagementStatus } from "@/app/EngagementContext";
import type { ConnectedMailbox, EngagementCampaign } from "@/lib/types";
import styles from "./Engagement.module.css";

/**
 * Campaigns on the engagement engine (spec §9): sequences an SDR sends from their own mailbox.
 *
 * Every member builds their own; a manager can look across the team. The list is the way into a
 * campaign, and the campaign page is where it is built, reviewed, launched and steered.
 */

function total(counts: Record<string, number>): number {
  return Object.values(counts).reduce((a, b) => a + b, 0);
}

export function EngagementCampaignsPage() {
  const api = useApiClient();
  const navigate = useNavigate();
  const status = useEngagementStatus();
  const [team, setTeam] = useState(false);
  const campaigns = useApi<EngagementCampaign[]>((s) => api.listEngagementCampaigns(team, s), [team]);
  const mailboxes = useApi<ConnectedMailbox[]>((s) => api.listConnectedMailboxes(false, s), []);
  const hasMailbox = (mailboxes.data ?? []).some((m) => m.mine && m.status === "connected");

  const columns: Column<EngagementCampaign>[] = [
    {
      key: "name",
      header: "Campaign",
      sortable: true,
      sortValue: (c) => c.name.toLowerCase(),
      render: (c) => (
        <Link to={`/engagement/campaigns/${c.id}`} className={styles.rowLink}>{c.name}</Link>
      ),
    },
    {
      key: "status",
      header: "Status",
      render: (c) => {
        const s = CAMPAIGN_STATUS[c.status] ?? { label: c.status, tone: "neutral" as const };
        return (
          <span className={styles.statusCell}>
            <Badge tone={s.tone} dot>{s.label}</Badge>
            {c.status === "paused" && c.pause_reason && c.pause_reason !== "manual" && (
              <span className={styles.muted}>{reasonText(c.pause_reason)}</span>
            )}
          </span>
        );
      },
    },
    {
      key: "people",
      header: "People",
      align: "right",
      sortable: true,
      sortValue: (c) => total(c.counts),
      render: (c) => <span className={styles.num}>{total(c.counts)}</span>,
    },
    {
      key: "active",
      header: "In sequence",
      align: "right",
      hideOnMobile: true,
      render: (c) => <span className={styles.num}>{c.counts.active ?? 0}</span>,
    },
    {
      key: "steps",
      header: "Steps",
      align: "right",
      hideOnMobile: true,
      render: (c) => <span className={styles.num}>{c.steps.length}</span>,
    },
    {
      key: "launched",
      header: "Launched",
      hideOnMobile: true,
      sortable: true,
      sortValue: (c) => c.launched_at ?? "",
      render: (c) => (c.launched_at ? whenDay(c.launched_at) : <span className={styles.muted}>Not yet</span>),
    },
  ];

  return (
    <div className={styles.page}>
      <PageHeader
        title="Campaigns"
        description="Sequences sent from your own mailbox. Every first email waits for your approval, and replies arrive in Replies."
        actions={
          <>
            {status?.can_manage && (
              <Button variant="secondary" size="sm" aria-pressed={team} onClick={() => setTeam((t) => !t)}>
                {team ? "Show mine" : "Show team"}
              </Button>
            )}
            <Button
              iconLeft={<Icons.PlusIcon />}
              onClick={() => navigate("/engagement/campaigns/new")}
              disabled={mailboxes.data !== null && !hasMailbox}
            >
              New campaign
            </Button>
          </>
        }
      />

      {mailboxes.data !== null && !hasMailbox && (
        <p className={styles.notice} role="status">
          Campaigns send from your own Gmail or Outlook mailbox.{" "}
          <Link to="/mailboxes" className={styles.inlineLink}>Connect it on My mailboxes</Link>{" "}
          to build one.
        </p>
      )}

      {campaigns.error ? (
        <ErrorState title="Couldn't load campaigns" message={campaigns.error.detail} onRetry={campaigns.refetch} />
      ) : (
        <DataTable
          columns={columns}
          rows={campaigns.data ?? []}
          getRowKey={(c) => c.id}
          loading={campaigns.loading && !campaigns.data}
          onRowClick={(c) => navigate(`/engagement/campaigns/${c.id}`)}
          caption="Campaigns"
          empty={
            <EmptyState
              icon={<Icons.SendIcon />}
              title={team ? "Nobody on the team has a campaign yet" : "No campaigns yet"}
              description="Pick the people, choose the steps, read every first email, then launch. Nothing sends until you approve it."
              action={hasMailbox ? (
                <Button iconLeft={<Icons.PlusIcon />} onClick={() => navigate("/engagement/campaigns/new")}>
                  New campaign
                </Button>
              ) : undefined}
            />
          }
        />
      )}
    </div>
  );
}

export default EngagementCampaignsPage;
```

- [ ] **Step 3: Starting a campaign** — `CampaignBuilder.tsx`. Creating a campaign adds nobody and sends nothing; it lands on the campaign page, where the work continues in order.

```tsx
import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { PageHeader } from "@/components/layout/PageHeader";
import { Button, Card, EmptyState, Field, Icons, Input, Select, Skeleton } from "@/components/ui";
import { useToast } from "@/components/ui/Toast";
import { StepsEditor, blankStep, stepsProblem } from "@/components/engagement/StepsEditor";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import type { ConnectedMailbox, EngagementStep, SequenceTemplate } from "@/lib/types";
import styles from "./Engagement.module.css";

/**
 * Start a campaign: its name, the mailbox it sends from, and its steps (spec §9, step 2).
 *
 * Creating it adds nobody and sends nothing. The campaign page that follows is where people are
 * added, first emails drafted and read, and the campaign launched, in that order.
 */

const CUSTOM = "";

function localInputValue(date: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

export function CampaignBuilder() {
  const api = useApiClient();
  const toast = useToast();
  const navigate = useNavigate();
  const mailboxes = useApi<ConnectedMailbox[]>((s) => api.listConnectedMailboxes(false, s), []);
  const templates = useApi<SequenceTemplate[]>((s) => api.listSequenceTemplates(s), []);

  const mine = useMemo(
    () => (mailboxes.data ?? []).filter((m) => m.mine && m.status === "connected"),
    [mailboxes.data],
  );
  const [name, setName] = useState("");
  const [mailboxId, setMailboxId] = useState("");
  const [templateId, setTemplateId] = useState(CUSTOM);
  const [steps, setSteps] = useState<EngagementStep[]>([blankStep(true), blankStep()]);
  const [firstSend, setFirstSend] = useState<"on_approval" | "scheduled">("on_approval");
  const [firstSendAt, setFirstSendAt] = useState(() => {
    const d = new Date();
    d.setDate(d.getDate() + 1);
    d.setHours(9, 30, 0, 0);
    return localInputValue(d);
  });
  const [timezoneMode, setTimezoneMode] = useState<"contact" | "sdr">("contact");
  const [reviewEvery, setReviewEvery] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!mailboxId && mine.length) setMailboxId(mine[0].id);
  }, [mine, mailboxId]);

  function applyTemplate(id: string) {
    setTemplateId(id);
    const t = (templates.data ?? []).find((x) => x.id === id);
    if (t) setSteps(t.steps.map((s) => ({ ...s })));
  }

  const problem = !name.trim()
    ? "Give the campaign a name."
    : !mailboxId
      ? "Choose the mailbox it sends from."
      : stepsProblem(steps);

  async function create() {
    if (problem) {
      setError(problem);
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const campaign = await api.createEngagementCampaign({
        name: name.trim(),
        mailbox_id: mailboxId,
        steps,
        template_id: templateId || null,
        review_every_touch: reviewEvery,
        first_send_mode: firstSend,
        first_send_at: firstSend === "scheduled" ? new Date(firstSendAt).toISOString() : null,
        timezone_mode: timezoneMode,
      });
      toast.success("Campaign created", "Now add the people it goes to.");
      navigate(`/engagement/campaigns/${campaign.id}`, { replace: true });
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "The campaign could not be created.");
    } finally {
      setSaving(false);
    }
  }

  if (mailboxes.loading && !mailboxes.data) {
    return <Skeleton width="100%" height={360} />;
  }
  if (mailboxes.data && mine.length === 0) {
    return (
      <div className={styles.page}>
        <PageHeader title="New campaign" />
        <EmptyState
          icon={<Icons.MailIcon />}
          title="Connect your mailbox first"
          description="A campaign sends from your own Gmail or Outlook mailbox, so replies come back to you."
          action={<Link to="/mailboxes" className={styles.buttonLink}>Go to My mailboxes</Link>}
        />
      </div>
    );
  }

  const templateOptions = [
    { value: CUSTOM, label: "Write the steps here" },
    ...(templates.data ?? []).map((t) => ({ value: t.id, label: t.name })),
  ];

  return (
    <div className={styles.page}>
      <PageHeader
        title="New campaign"
        description="Name it, choose where it sends from, and set its steps. People are added next, and nothing sends until you approve it."
      />

      <form
        className={styles.form}
        onSubmit={(e) => {
          e.preventDefault();
          void create();
        }}
        noValidate
      >
        <Card padding="lg" className={styles.section}>
          <h2 className={styles.sectionTitle}>The campaign</h2>
          <div className={styles.fields}>
            <Field label="Name" required>
              <Input value={name} onChange={(e) => setName(e.target.value)} maxLength={200}
                placeholder="Q4 fintech CFOs" autoFocus />
            </Field>
            <Field label="Sends from" hint="Your own mailbox. Replies come back to it.">
              <Select
                value={mailboxId}
                onChange={(e) => setMailboxId(e.target.value)}
                options={mine.map((m) => ({ value: m.id, label: m.email }))}
              />
            </Field>
          </div>
        </Card>

        <Card padding="lg" className={styles.section}>
          <div className={styles.sectionHead}>
            <h2 className={styles.sectionTitle}>Steps</h2>
            {(templates.data?.length ?? 0) > 0 && (
              <Field label="Start from" hideLabel>
                <Select value={templateId} onChange={(e) => applyTemplate(e.target.value)} options={templateOptions} />
              </Field>
            )}
          </div>
          <StepsEditor steps={steps} onChange={(next) => { setSteps(next); setTemplateId(CUSTOM); }} />
        </Card>

        <Card padding="lg" className={styles.section}>
          <h2 className={styles.sectionTitle}>Timing</h2>
          <div className={styles.fields}>
            <Field label="Opening emails go out" hint="Every opening email is read and approved first either way.">
              <Select
                value={firstSend}
                onChange={(e) => setFirstSend(e.target.value as typeof firstSend)}
                options={[
                  { value: "on_approval", label: "As soon as I approve each one" },
                  { value: "scheduled", label: "At a time I choose" },
                ]}
              />
            </Field>
            {firstSend === "scheduled" && (
              <Field label="Starting at" hint="Your local time.">
                <Input type="datetime-local" value={firstSendAt} onChange={(e) => setFirstSendAt(e.target.value)} />
              </Field>
            )}
            <Field label="Follow the timezone of" hint="Used for every follow-up and set send time.">
              <Select
                value={timezoneMode}
                onChange={(e) => setTimezoneMode(e.target.value as typeof timezoneMode)}
                options={[
                  { value: "contact", label: "Each contact (their working morning)" },
                  { value: "sdr", label: "Me" },
                ]}
              />
            </Field>
          </div>
          <label className={styles.checkRow}>
            <input type="checkbox" checked={reviewEvery} onChange={(e) => setReviewEvery(e.target.checked)} />
            <span>
              Review every follow-up too, not only the opening email.
              <span className={styles.muted}> Each one waits for you before it sends.</span>
            </span>
          </label>
        </Card>

        {error && <p className={styles.formError} role="alert">{error}</p>}
        <div className={styles.formActions}>
          <Button type="button" variant="ghost" onClick={() => navigate("/engagement/campaigns")}>Cancel</Button>
          <Button type="submit" loading={saving} iconRight={<Icons.ChevronRightIcon />}>
            Create and add people
          </Button>
        </div>
      </form>
    </div>
  );
}

export default CampaignBuilder;
```

- [ ] **Step 4: Reading every opening email** — `ReviewQueue.tsx`. Drafting runs in passes of ten and stops on a pass that writes nothing, or on `out_of_credits`, so a failing draft cannot loop.

```tsx
import { useState } from "react";
import {
  Badge, Button, EmptyState, ErrorState, Field, Icons, Input, Skeleton, Textarea, WorkingIndicator,
} from "@/components/ui";
import { useToast } from "@/components/ui/Toast";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import type { ReviewItem } from "@/lib/types";
import styles from "./Engagement.module.css";

/**
 * Read every opening email before it goes (spec §9 step 4, D22).
 *
 * Each person's draft is shown with the quality check's complaints beside it. The SDR edits in
 * place and approves, regenerates, or takes the person out. "Approve all that pass" approves only
 * drafts the check found nothing wrong with: a flagged draft always needs a person to look.
 *
 * Approving on a running campaign sends that person's email at once (or at the scheduled start),
 * so the queue is useful after launch too: people added later are reviewed here the same way.
 */

const STATUS: Record<string, { label: string; tone: "neutral" | "info" | "success" }> = {
  undrafted: { label: "Not written yet", tone: "neutral" },
  draft: { label: "Draft", tone: "info" },
  approved: { label: "Approved", tone: "success" },
};

export interface ReviewQueueProps {
  campaignId: string;
  /** Something changed that the rest of the page counts (approvals, removals, new drafts). */
  onChanged: () => void;
}

export function ReviewQueue({ campaignId, onChanged }: ReviewQueueProps) {
  const api = useApiClient();
  const toast = useToast();
  const review = useApi<ReviewItem[]>((s) => api.campaignReview(campaignId, s), [campaignId]);
  const [drafting, setDrafting] = useState<{ done: number; of: number } | null>(null);
  const [approvingAll, setApprovingAll] = useState(false);

  const items = review.data ?? [];
  const undrafted = items.filter((i) => i.status === "undrafted").length;
  const passing = items.filter((i) => i.status === "draft" && i.quality_problems.length === 0).length;
  const flagged = items.filter((i) => i.status === "draft" && i.quality_problems.length > 0).length;
  const approved = items.filter((i) => i.status === "approved").length;

  async function draftAll() {
    const of = undrafted;
    let done = 0;
    setDrafting({ done, of });
    try {
      // The server drafts in passes so a long list is not one request. A pass that writes nothing
      // is the end: either everyone has a draft, or what is left keeps failing and needs a look.
      for (;;) {
        const pass = await api.draftCampaign(campaignId, 10);
        done += pass.drafted;
        setDrafting({ done, of });
        review.refetch();
        if (pass.errors.some((e) => e.error === "out_of_credits")) {
          toast.error("Out of credits", "Drafting stopped. Top up the workspace's credits to write the rest.");
          break;
        }
        if (pass.drafted === 0) {
          if (pass.failed > 0) {
            toast.error(`${pass.failed} could not be written`, "Try Regenerate on each, or take them out.");
          }
          break;
        }
      }
      if (done > 0) toast.success(`${done} opening ${done === 1 ? "email" : "emails"} written`, "Read each one, then approve.");
    } catch (err) {
      toast.error("Drafting stopped", err instanceof ApiError ? err.detail : "Please try again.");
    } finally {
      setDrafting(null);
      review.refetch();
      onChanged();
    }
  }

  async function approveAll() {
    setApprovingAll(true);
    try {
      const { approved: n } = await api.approveAllPassing(campaignId);
      toast.success(`${n} approved`, flagged ? `${flagged} flagged ${flagged === 1 ? "draft still needs" : "drafts still need"} a look.` : "");
      review.refetch();
      onChanged();
    } catch (err) {
      toast.error("Couldn't approve", err instanceof ApiError ? err.detail : "Please try again.");
    } finally {
      setApprovingAll(false);
    }
  }

  if (review.error) {
    return <ErrorState title="Couldn't load the review queue" message={review.error.detail} onRetry={review.refetch} />;
  }
  if (review.loading && !review.data) {
    return (
      <div className={styles.stack}>
        <Skeleton width="100%" height={56} />
        <Skeleton width="100%" height={220} />
        <Skeleton width="100%" height={220} />
      </div>
    );
  }
  if (items.length === 0) {
    return (
      <EmptyState
        icon={<Icons.CheckIcon />}
        title="Nobody is waiting for review"
        description="Everyone added so far has been approved or has already started. People you add next appear here."
      />
    );
  }

  return (
    <div className={styles.stack}>
      <div className={styles.toolbar}>
        <p className={styles.summary} aria-live="polite">
          <span><strong>{approved}</strong> approved</span>
          <span><strong>{passing}</strong> ready</span>
          {flagged > 0 && <span><strong>{flagged}</strong> flagged</span>}
          {undrafted > 0 && <span><strong>{undrafted}</strong> not written</span>}
        </p>
        <div className={styles.toolbarActions}>
          {undrafted > 0 && (
            <Button variant="secondary" iconLeft={<Icons.SparklesIcon />} onClick={draftAll} disabled={drafting !== null}>
              Write {undrafted} opening {undrafted === 1 ? "email" : "emails"}
            </Button>
          )}
          <Button onClick={approveAll} loading={approvingAll} disabled={passing === 0 || drafting !== null}
            iconLeft={<Icons.CheckIcon />}>
            Approve all that pass
          </Button>
        </div>
      </div>

      {drafting && (
        <WorkingIndicator
          label={`Writing opening emails: ${drafting.done} of ${drafting.of}`}
          hint="Each one is written from what the person and their company are doing now."
          slowAfter={30}
        />
      )}

      <ol className={styles.reviewList}>
        {items.map((item) => (
          <ReviewCard key={item.enrollment_id} item={item}
            onChanged={() => { review.refetch(); onChanged(); }} />
        ))}
      </ol>
    </div>
  );
}

function ReviewCard({ item, onChanged }: { item: ReviewItem; onChanged: () => void }) {
  const api = useApiClient();
  const toast = useToast();
  const [subject, setSubject] = useState(item.subject);
  const [body, setBody] = useState(item.body);
  const [busy, setBusy] = useState<"approve" | "regenerate" | "remove" | null>(null);
  const status = STATUS[item.status] ?? { label: item.status, tone: "neutral" as const };
  const editable = item.status === "draft" && item.message_id !== null;
  const edited = subject !== item.subject || body !== item.body;

  async function run(kind: NonNullable<typeof busy>, action: () => Promise<unknown>, done?: string) {
    setBusy(kind);
    try {
      await action();
      if (done) toast.success(done, "");
      onChanged();
    } catch (err) {
      toast.error("That didn't work", err instanceof ApiError ? err.detail : "Please try again.");
    } finally {
      setBusy(null);
    }
  }

  return (
    <li className={styles.reviewCard}>
      <header className={styles.reviewHead}>
        <div className={styles.person}>
          <span className={styles.personName}>{item.contact_name || item.contact_email}</span>
          <span className={styles.muted}>
            {[item.contact_title, item.account_name].filter(Boolean).join(" · ")}
          </span>
          <span className={styles.muted}>{item.contact_email}</span>
        </div>
        <Badge tone={status.tone} dot>{status.label}</Badge>
      </header>

      {/* The check read the AI's text; once a person has approved it, those notes are history. */}
      {item.status === "draft" && item.quality_problems.length > 0 && (
        // Sentences, not tags: each one says what to change, so it has to wrap and be read.
        <ul className={styles.problems} aria-label="What the check found">
          {item.quality_problems.map((p) => (
            <li key={p} className={styles.problem}>
              <Icons.AlertTriangleIcon aria-hidden className={styles.problemIcon} />
              <span>{p}</span>
            </li>
          ))}
        </ul>
      )}

      {busy === "regenerate" ? (
        <WorkingIndicator label={`Rewriting the email to ${item.contact_name || "this person"}`} hint="Usually under 15 seconds." slowAfter={20} />
      ) : item.status === "undrafted" ? (
        <p className={styles.muted}>No draft yet. Use "Write opening emails" above.</p>
      ) : editable ? (
        <div className={styles.draft}>
          <Field label="Subject">
            <Input value={subject} onChange={(e) => setSubject(e.target.value)} />
          </Field>
          <Field label="Email">
            <Textarea rows={9} value={body} onChange={(e) => setBody(e.target.value)} />
          </Field>
        </div>
      ) : (
        <div className={styles.draftRead}>
          <p className={styles.subjectLine}>{item.subject}</p>
          <p className={styles.bodyText}>{item.body}</p>
        </div>
      )}

      <div className={styles.cardActions}>
        {editable && (
          <Button
            loading={busy === "approve"} disabled={busy !== null || !body.trim()}
            iconLeft={<Icons.CheckIcon />}
            onClick={() => run("approve", () => api.approveCampaignMessage(
              item.message_id as string, edited ? { subject, body } : {}), edited ? "Approved with your edits" : "Approved")}
          >
            {edited ? "Approve with edits" : "Approve"}
          </Button>
        )}
        {item.message_id && item.status !== "undrafted" && (
          <Button
            variant="secondary" disabled={busy !== null} iconLeft={<Icons.RefreshIcon />}
            onClick={() => run("regenerate", async () => {
              const fresh = await api.regenerateCampaignMessage(item.message_id as string);
              setSubject(fresh.subject);
              setBody(fresh.body);
            })}
          >
            Regenerate
          </Button>
        )}
        <Button
          variant="ghost" loading={busy === "remove"} disabled={busy !== null}
          onClick={() => run("remove", () => api.enrollmentAction(item.enrollment_id, "remove"), "Taken out of the campaign")}
        >
          Take out
        </Button>
      </div>
    </li>
  );
}
```

- [ ] **Step 5: Launching behind the gate** — `LaunchPanel.tsx`:

```tsx
import { useState } from "react";
import { Link } from "react-router-dom";
import { Button, ErrorState, Icons, Skeleton } from "@/components/ui";
import { useToast } from "@/components/ui/Toast";
import { CreditEstimate } from "@/components/engagement/CreditEstimate";
import { VolumeWarning } from "@/components/engagement/VolumeWarning";
import { useApi } from "@/hooks/useApi";
import { useApiClient, useAuth } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import type { CampaignEstimate, EngagementCampaign, ReviewItem } from "@/lib/types";
import styles from "./Engagement.module.css";

/**
 * Launch, or resume, a campaign behind the credit gate (spec §9 step 5, §10, D18).
 *
 * The button is held while the balance cannot cover the most the campaign can cost; the server
 * refuses the same way, so this only saves a round trip. Launching with nobody approved yet is
 * allowed and said plainly: the campaign starts, and each person goes out as they are approved.
 */

export interface LaunchPanelProps {
  campaign: EngagementCampaign;
  onLaunched: () => void;
}

export function LaunchPanel({ campaign, onLaunched }: LaunchPanelProps) {
  const api = useApiClient();
  const toast = useToast();
  const { session } = useAuth();
  const [launching, setLaunching] = useState(false);
  const estimate = useApi<CampaignEstimate>((s) => api.campaignEstimate(campaign.id, s), [campaign.id]);
  // Approved-and-waiting is a MESSAGE state: before launch every person is still awaiting review,
  // approved or not, so the count comes from the review queue rather than the enrollment counts.
  const review = useApi<ReviewItem[]>((s) => api.campaignReview(campaign.id, s), [campaign.id]);
  const approved = (review.data ?? []).filter((i) => i.status === "approved").length;
  const resuming = campaign.status === "paused";
  const canTopUp = session?.role === "admin" || session?.role === "owner";

  async function launch() {
    setLaunching(true);
    try {
      if (resuming) await api.campaignAction(campaign.id, "resume");
      else await api.launchCampaign(campaign.id);
      toast.success(resuming ? "Campaign resumed" : "Campaign launched",
        approved ? `${approved} ${approved === 1 ? "email goes" : "emails go"} out on schedule.` : "Each person goes out as you approve them.");
      onLaunched();
    } catch (err) {
      toast.error(resuming ? "Not resumed" : "Not launched", err instanceof ApiError ? err.detail : "Please try again.");
      estimate.refetch();
    } finally {
      setLaunching(false);
    }
  }

  if (estimate.error) {
    return <ErrorState title="Couldn't work out the cost" message={estimate.error.detail} onRetry={estimate.refetch} />;
  }
  if (!estimate.data) {
    return <Skeleton width="100%" height={260} />;
  }
  const e = estimate.data;
  const blocked = e.gate_applies && !e.covered;

  return (
    <div className={styles.stack}>
      <CreditEstimate estimate={e} />
      <VolumeWarning message={e.volume_warning} />
      {e.contacts === 0 ? (
        <p className={styles.notice} role="status">Add people before launching.</p>
      ) : approved === 0 ? (
        <p className={styles.notice} role="status">
          Nobody is approved yet. Launching starts the campaign, and each person goes out as you
          approve their email.
        </p>
      ) : null}
      <div className={styles.formActions}>
        {blocked && canTopUp && (
          <Link to="/settings/billing" className={styles.buttonLink}>Add credits</Link>
        )}
        <Button onClick={launch} loading={launching} disabled={blocked || e.contacts === 0}
          iconLeft={<Icons.SendIcon />}>
          {resuming ? "Resume campaign" : "Launch campaign"}
        </Button>
      </div>
      {blocked && !canTopUp && (
        <p className={styles.muted}>Ask an owner or admin to add credits to the workspace.</p>
      )}
    </div>
  );
}
```

- [ ] **Step 6: One campaign** — `CampaignDetailPage.tsx`. It opens where the work is: People while there are none, then Review, then Launch.

```tsx
import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { PageHeader } from "@/components/layout/PageHeader";
import {
  Badge, Button, Card, DataTable, EmptyState, ErrorState, Field, Icons, Input, Modal, Skeleton,
  TabPanel, Tabs,
} from "@/components/ui";
import type { Column } from "@/components/ui";
import { useToast } from "@/components/ui/Toast";
import { ContactPicker } from "@/components/engagement/ContactPicker";
import { StepsEditor, stepsProblem } from "@/components/engagement/StepsEditor";
import {
  CAMPAIGN_STATUS, ENROLLMENT_STATUS, WEEKDAYS, reasonText, when,
} from "@/components/engagement/labels";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import type {
  ConnectedMailbox, EngagementCampaign, EngagementEnrollment, EngagementStep, EnrollResult,
} from "@/lib/types";
import { LaunchPanel } from "./LaunchPanel";
import { ReviewQueue } from "./ReviewQueue";
import styles from "./Engagement.module.css";

/**
 * One campaign, from setting up to steering (spec §9).
 *
 * Before launch the tabs are the order the work happens in: add People, Review their opening
 * emails, Launch. After launch, People becomes the live view (each person's step, next send and
 * why they stopped) with per-person pause, resume, stop, send now and move. Review stays, because
 * anyone added later is read and approved the same way.
 */

type TabKey = "people" | "review" | "launch" | "steps";
const SETTING_UP = new Set(["draft", "reviewing"]);

function toLocalInput(iso: string | null): string {
  const d = iso ? new Date(iso) : new Date(Date.now() + 60 * 60 * 1000);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function CampaignDetailPage() {
  const { campaignId = "" } = useParams();
  const api = useApiClient();
  const toast = useToast();
  const campaign = useApi<EngagementCampaign>((s) => api.getEngagementCampaign(campaignId, s), [campaignId]);
  const enrollments = useApi<EngagementEnrollment[]>(
    (s) => api.campaignEnrollments(campaignId, s), [campaignId],
  );
  const mailboxes = useApi<ConnectedMailbox[]>((s) => api.listConnectedMailboxes(false, s), []);
  const [tab, setTab] = useState<TabKey | null>(null);
  const [acting, setActing] = useState<string | null>(null);
  const [lastAdd, setLastAdd] = useState<EnrollResult | null>(null);

  const c = campaign.data;
  const people = enrollments.data ?? [];
  const enrolledIds = useMemo(() => new Set(people.map((p) => p.contact_id)), [people]);
  const awaiting = c?.counts.awaiting_review ?? 0;
  const settingUp = c ? SETTING_UP.has(c.status) : false;
  const canLaunch = c ? settingUp || c.status === "paused" : false;

  // Open where the work is: people first while there are none, then review, then launch.
  useEffect(() => {
    if (!c || tab !== null) return;
    const count = Object.values(c.counts).reduce((a, b) => a + b, 0);
    setTab(settingUp ? (count === 0 ? "people" : awaiting > 0 ? "review" : "launch") : "people");
  }, [c, tab, settingUp, awaiting]);

  function refresh() {
    campaign.refetch();
    enrollments.refetch();
  }

  async function act(action: "pause" | "stop") {
    setActing(action);
    try {
      await api.campaignAction(campaignId, action);
      toast.success(action === "pause" ? "Campaign paused" : "Campaign stopped",
        action === "pause" ? "Nothing more sends until you resume it." : "Nobody else will be emailed from it.");
      refresh();
    } catch (err) {
      toast.error("That didn't work", err instanceof ApiError ? err.detail : "Please try again.");
    } finally {
      setActing(null);
    }
  }

  async function addPeople(ids: string[]) {
    try {
      const result = await api.addCampaignContacts(campaignId, ids);
      setLastAdd(result);
      toast.success(`${result.added.length} added`,
        result.skipped.length ? `${result.skipped.length} could not be added.` : "Next, write and review their opening emails.");
      refresh();
    } catch (err) {
      toast.error("Couldn't add them", err instanceof ApiError ? err.detail : "Please try again.");
    }
  }

  if (campaign.error) {
    return (
      <ErrorState
        title={campaign.error.status === 404 ? "Campaign not found" : "Couldn't load the campaign"}
        message={campaign.error.status === 404 ? "It may belong to someone else, or it was removed." : campaign.error.detail}
        onRetry={campaign.error.status === 404 ? undefined : campaign.refetch}
      />
    );
  }
  if (!c) return <Skeleton width="100%" height={420} />;

  const status = CAMPAIGN_STATUS[c.status] ?? { label: c.status, tone: "neutral" as const };
  const mailbox = (mailboxes.data ?? []).find((m) => m.id === c.mailbox_connection_id);
  const total = Object.values(c.counts).reduce((a, b) => a + b, 0);
  const tabs = [
    { value: "people", label: "People", count: total },
    { value: "review", label: "Review", count: awaiting },
    ...(canLaunch ? [{ value: "launch", label: c.status === "paused" ? "Resume" : "Launch" }] : []),
    { value: "steps", label: "Steps", count: c.steps.length },
  ];

  return (
    <div className={styles.page}>
      <Link to="/engagement/campaigns" className={styles.back}>
        <Icons.ChevronLeftIcon aria-hidden /> Campaigns
      </Link>
      <PageHeader
        title={c.name}
        description={
          <span className={styles.headerMeta}>
            <Badge tone={status.tone} dot>{status.label}</Badge>
            {c.status === "paused" && c.pause_reason && c.pause_reason !== "manual" && (
              <span>Paused because {reasonText(c.pause_reason)}</span>
            )}
            <span>Sends from {mailbox?.email ?? "your mailbox"}</span>
          </span>
        }
        actions={
          <>
            {c.status === "active" && (
              <Button variant="secondary" loading={acting === "pause"} disabled={acting !== null}
                onClick={() => act("pause")}>Pause</Button>
            )}
            {(c.status === "active" || c.status === "paused") && (
              <Button variant="ghost" loading={acting === "stop"} disabled={acting !== null}
                onClick={() => act("stop")}>Stop</Button>
            )}
          </>
        }
      />

      {c.status === "paused" && c.pause_reason === "out_of_credits" && (
        <p className={styles.notice} role="status">
          It paused when a charge could not be covered. Once credits are added, resume it from the
          Resume tab and it continues from where it stopped.
        </p>
      )}
      {mailbox && mailbox.status !== "connected" && (
        <p className={styles.notice} role="alert">
          {mailbox.email} needs reconnecting before this campaign can send.{" "}
          <Link to="/mailboxes" className={styles.inlineLink}>Reconnect it on My mailboxes</Link>.
        </p>
      )}

      <Tabs items={tabs} value={tab ?? "people"} onChange={(v) => setTab(v as TabKey)} idPrefix="campaign" />

      <TabPanel id="campaign-panel-people" active={(tab ?? "people") === "people"}>
        <div className={styles.stack}>
          {c.status !== "completed" && (
            <Card padding="lg" className={styles.section}>
              <h2 className={styles.sectionTitle}>Add people</h2>
              <ContactPicker enrolledIds={enrolledIds} onAdd={addPeople} />
              {lastAdd && <AddResult result={lastAdd} people={people} />}
            </Card>
          )}
          <PeopleTable
            rows={people} loading={enrollments.loading && !enrollments.data}
            error={enrollments.error?.detail ?? null} onRetry={enrollments.refetch}
            steps={c.steps.length} live={!settingUp} onChanged={refresh}
          />
        </div>
      </TabPanel>

      <TabPanel id="campaign-panel-review" active={tab === "review"}>
        <ReviewQueue campaignId={c.id} onChanged={refresh} />
      </TabPanel>

      {canLaunch && (
        <TabPanel id="campaign-panel-launch" active={tab === "launch"}>
          <Card padding="lg">
            <LaunchPanel campaign={c} onLaunched={() => { refresh(); setTab("people"); }} />
          </Card>
        </TabPanel>
      )}

      <TabPanel id="campaign-panel-steps" active={tab === "steps"}>
        <StepsPanel campaign={c} editable={settingUp} onSaved={refresh} />
      </TabPanel>
    </div>
  );
}

function AddResult({ result, people }: { result: EnrollResult; people: EngagementEnrollment[] }) {
  const name = (id: string) => people.find((p) => p.contact_id === id)?.contact_name || "Someone";
  if (!result.skipped.length && !result.warnings.length) return null;
  const why: Record<string, string> = {
    not_found: "no longer exists",
    already_enrolled: "is already in this campaign",
    no_email: "has no email address",
  };
  return (
    <div className={styles.addResult} role="status">
      {result.warnings.length > 0 && (
        <>
          <p className={styles.subhead}>Already being emailed elsewhere</p>
          <ul className={styles.plainList}>
            {result.warnings.map((w) => (
              <li key={`${w.contact_id}-${w.campaign_id}`}>
                {name(w.contact_id)} is also in {w.campaign_name || "another campaign"}
                {w.owner ? ` (${w.owner})` : ""}. Added anyway; check before approving.
              </li>
            ))}
          </ul>
        </>
      )}
      {result.skipped.length > 0 && (
        <>
          <p className={styles.subhead}>Not added</p>
          <ul className={styles.plainList}>
            {result.skipped.map((s) => (
              <li key={s.contact_id}>
                {name(s.contact_id)} {s.reason.startsWith("do_not_contact") ? "is on the do-not-contact list" : why[s.reason] ?? s.reason}.
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}

function PeopleTable({
  rows, loading, error, onRetry, steps, live, onChanged,
}: {
  rows: EngagementEnrollment[];
  loading: boolean;
  error: string | null;
  onRetry: () => void;
  steps: number;
  live: boolean;
  onChanged: () => void;
}) {
  const api = useApiClient();
  const toast = useToast();
  const [busy, setBusy] = useState<string | null>(null);
  const [moving, setMoving] = useState<EngagementEnrollment | null>(null);
  const [moveTo, setMoveTo] = useState("");

  async function act(row: EngagementEnrollment, action: "pause" | "resume" | "stop" | "send-now") {
    setBusy(`${row.id}:${action}`);
    try {
      await api.enrollmentAction(row.id, action);
      const said = { pause: "Paused", resume: "Resumed", stop: "Stopped", "send-now": "Sending now" }[action];
      toast.success(`${said}: ${row.contact_name}`, "");
      onChanged();
    } catch (err) {
      toast.error("That didn't work", err instanceof ApiError ? err.detail : "Please try again.");
    } finally {
      setBusy(null);
    }
  }

  async function move() {
    if (!moving) return;
    setBusy(`${moving.id}:move`);
    try {
      await api.moveEnrollment(moving.id, new Date(moveTo).toISOString());
      toast.success(`Moved: ${moving.contact_name}`, `Next step ${when(new Date(moveTo).toISOString())}.`);
      setMoving(null);
      onChanged();
    } catch (err) {
      toast.error("Couldn't move it", err instanceof ApiError ? err.detail : "Please try again.");
    } finally {
      setBusy(null);
    }
  }

  const columns: Column<EngagementEnrollment>[] = [
    {
      key: "person",
      header: "Person",
      sortable: true,
      sortValue: (r) => r.contact_name.toLowerCase(),
      render: (r) => (
        <div className={styles.person}>
          <span className={styles.personName}>{r.contact_name || r.contact_email}</span>
          <span className={styles.muted}>{[r.contact_title, r.account_name].filter(Boolean).join(" · ")}</span>
        </div>
      ),
    },
    {
      key: "status",
      header: "Status",
      render: (r) => {
        const s = ENROLLMENT_STATUS[r.status];
        const why = reasonText(r.status_reason);
        return (
          <span className={styles.statusCell}>
            <Badge tone={s.tone} dot>{s.label}</Badge>
            {why && <span className={styles.muted}>{why}</span>}
          </span>
        );
      },
    },
    {
      key: "step",
      header: "Step",
      align: "right",
      hideOnMobile: true,
      render: (r) => <span className={styles.num}>{Math.min(r.current_step_index + 1, steps)} of {steps}</span>,
    },
    {
      key: "next",
      header: "Next",
      hideOnMobile: true,
      sortable: true,
      sortValue: (r) => r.snoozed_until ?? r.next_action_at ?? "",
      render: (r) => {
        if (r.status === "stopped" || r.status === "completed") return <span className={styles.muted}>Nothing more</span>;
        if (r.status === "snoozed" || r.status_reason === "out_of_office") return `Back ${when(r.snoozed_until)}`;
        if (r.status === "awaiting_review") return <span className={styles.muted}>After approval</span>;
        return when(r.next_action_at);
      },
    },
  ];
  if (live) {
    columns.push({
      key: "actions",
      header: <span className={styles.srOnly}>Actions</span>,
      align: "right",
      render: (r) => {
        const b = (a: string) => busy === `${r.id}:${a}`;
        const disabled = busy !== null;
        return (
          <div className={styles.rowActions}>
            {r.status === "active" && (
              <>
                <Button size="sm" variant="ghost" loading={b("send-now")} disabled={disabled}
                  onClick={() => act(r, "send-now")}>Send now</Button>
                <Button size="sm" variant="ghost" disabled={disabled}
                  onClick={() => { setMoving(r); setMoveTo(toLocalInput(r.next_action_at)); }}>Move</Button>
                <Button size="sm" variant="ghost" loading={b("pause")} disabled={disabled}
                  onClick={() => act(r, "pause")}>Pause</Button>
              </>
            )}
            {(r.status === "paused" || r.status === "snoozed") && (
              <Button size="sm" variant="ghost" loading={b("resume")} disabled={disabled}
                onClick={() => act(r, "resume")}>Resume</Button>
            )}
            {!["stopped", "completed", "awaiting_review"].includes(r.status) && (
              <Button size="sm" variant="ghost" loading={b("stop")} disabled={disabled}
                onClick={() => act(r, "stop")}>Stop</Button>
            )}
          </div>
        );
      },
    });
  }

  if (error) return <ErrorState title="Couldn't load the people in this campaign" message={error} onRetry={onRetry} />;
  return (
    <>
      <DataTable
        columns={columns}
        rows={rows}
        getRowKey={(r) => r.id}
        loading={loading}
        caption="People in this campaign"
        empty={
          <EmptyState compact icon={<Icons.UsersIcon />} title="Nobody added yet"
            description="Choose people above: from a saved list, by title and seniority, or by search." />
        }
      />
      <Modal
        open={moving !== null}
        onClose={() => setMoving(null)}
        title={`Move ${moving?.contact_name ?? ""}'s next step`}
        description="The next step goes at this time instead of when its timing says. Later steps follow from it."
        footer={
          <>
            <Button variant="ghost" onClick={() => setMoving(null)}>Cancel</Button>
            <Button onClick={move} loading={moving !== null && busy === `${moving.id}:move`} disabled={!moveTo}>
              Move it
            </Button>
          </>
        }
      >
        <Field label="Send the next step at" hint="Your local time.">
          <Input type="datetime-local" value={moveTo} onChange={(e) => setMoveTo(e.target.value)} />
        </Field>
      </Modal>
    </>
  );
}

function StepsPanel({ campaign, editable, onSaved }: {
  campaign: EngagementCampaign;
  editable: boolean;
  onSaved: () => void;
}) {
  const api = useApiClient();
  const toast = useToast();
  const [steps, setSteps] = useState<EngagementStep[]>(() => campaign.steps.map((s) => ({ ...s })));
  const [saving, setSaving] = useState(false);
  const problem = stepsProblem(steps);
  const dirty = JSON.stringify(steps) !== JSON.stringify(campaign.steps);

  async function save() {
    setSaving(true);
    try {
      await api.replaceCampaignSteps(campaign.id, steps);
      toast.success("Steps saved", "Drafts already written keep their text; regenerate any you want rewritten.");
      onSaved();
    } catch (err) {
      toast.error("Steps not saved", err instanceof ApiError ? err.detail : "Please try again.");
    } finally {
      setSaving(false);
    }
  }

  if (!editable) {
    return (
      <Card padding="lg">
        <ol className={styles.stepList}>
          {campaign.steps.map((s, i) => (
            <li key={i} className={styles.stepRow}>
              <span className={styles.stepNumber} aria-hidden="true">{i + 1}</span>
              <div>
                <p className={styles.personName}>
                  {i === 0 ? "Opening email" : s.channel === "call" ? "Call" : "Follow-up email"}
                  {i > 0 && <span className={styles.muted}> · after {s.delay_business_days} business {s.delay_business_days === 1 ? "day" : "days"}</span>}
                  {s.timing_mode === "manual" && s.send_time_local && <span className={styles.muted}> · at {s.send_time_local}</span>}
                </p>
                {s.angle && <p className={styles.muted}>{s.angle}</p>}
                <p className={styles.muted}>{s.allowed_weekdays.map((d) => WEEKDAYS[d]).join(", ")}</p>
              </div>
            </li>
          ))}
        </ol>
        <p className={styles.muted}>Steps are fixed once a campaign has launched.</p>
      </Card>
    );
  }
  return (
    <Card padding="lg" className={styles.section}>
      <StepsEditor steps={steps} onChange={setSteps} />
      {problem && dirty && <p className={styles.formError} role="alert">{problem}</p>}
      <div className={styles.formActions}>
        <Button onClick={save} loading={saving} disabled={!dirty || Boolean(problem)}>Save steps</Button>
      </div>
    </Card>
  );
}

export default CampaignDetailPage;
```

- [ ] **Step 7: Templates** — `SequenceTemplatesPage.tsx`:

```tsx
import { useState } from "react";
import { PageHeader } from "@/components/layout/PageHeader";
import {
  Button, Card, EmptyState, ErrorState, Field, Icons, Input, Modal, Skeleton, Textarea,
} from "@/components/ui";
import { useToast } from "@/components/ui/Toast";
import { StepsEditor, blankStep, stepsProblem } from "@/components/engagement/StepsEditor";
import { WEEKDAYS } from "@/components/engagement/labels";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import type { EngagementStep, SequenceTemplate } from "@/lib/types";
import styles from "./Engagement.module.css";

/**
 * Sequence templates (spec §9: Cadences becomes Sequence templates): a named set of steps a
 * campaign can start from. A campaign copies the steps when it is created, so editing a template
 * later never changes a campaign that is already running.
 */

interface Draft {
  id: string | null;
  name: string;
  description: string;
  steps: EngagementStep[];
}

function summary(steps: EngagementStep[]): string {
  const emails = steps.filter((s) => s.channel === "email").length;
  const calls = steps.length - emails;
  const days = steps.slice(1).reduce((n, s) => n + (s.delay_business_days || 0), 0);
  const parts = [`${emails} ${emails === 1 ? "email" : "emails"}`];
  if (calls) parts.push(`${calls} ${calls === 1 ? "call" : "calls"}`);
  parts.push(`over ${days} business ${days === 1 ? "day" : "days"}`);
  return parts.join(", ");
}

export function SequenceTemplatesPage() {
  const api = useApiClient();
  const toast = useToast();
  const templates = useApi<SequenceTemplate[]>((s) => api.listSequenceTemplates(s), []);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState<SequenceTemplate | null>(null);
  const [busyDelete, setBusyDelete] = useState(false);

  const problem = draft ? (!draft.name.trim() ? "Give the template a name." : stepsProblem(draft.steps)) : null;

  async function save() {
    if (!draft || problem) return;
    setSaving(true);
    try {
      const body = { name: draft.name.trim(), description: draft.description.trim(), steps: draft.steps };
      if (draft.id) await api.updateSequenceTemplate(draft.id, body);
      else await api.createSequenceTemplate(body);
      toast.success(draft.id ? "Template saved" : "Template created",
        "New campaigns can start from it. Running campaigns keep their own steps.");
      setDraft(null);
      templates.refetch();
    } catch (err) {
      toast.error("Template not saved", err instanceof ApiError ? err.detail : "Please try again.");
    } finally {
      setSaving(false);
    }
  }

  async function remove() {
    if (!deleting) return;
    setBusyDelete(true);
    try {
      await api.deleteSequenceTemplate(deleting.id);
      toast.success("Template deleted", "Campaigns created from it are unchanged.");
      setDeleting(null);
      templates.refetch();
    } catch (err) {
      toast.error("Couldn't delete it", err instanceof ApiError ? err.detail : "Please try again.");
    } finally {
      setBusyDelete(false);
    }
  }

  const newTemplate = () => setDraft({ id: null, name: "", description: "", steps: [blankStep(true), blankStep(), blankStep()] });

  return (
    <div className={styles.page}>
      <PageHeader
        title="Sequence templates"
        description="Reusable steps a campaign can start from. A campaign copies them when it is created, so changing a template never changes one that is running."
        actions={!draft && (
          <Button iconLeft={<Icons.PlusIcon />} onClick={newTemplate}>New template</Button>
        )}
      />

      {draft && (
        <Card padding="lg" className={styles.section}>
          <h2 className={styles.sectionTitle}>{draft.id ? "Edit template" : "New template"}</h2>
          <div className={styles.fields}>
            <Field label="Name" required>
              <Input value={draft.name} maxLength={200} autoFocus
                onChange={(e) => setDraft({ ...draft, name: e.target.value })} placeholder="Three touches, two weeks" />
            </Field>
            <Field label="When to use it">
              <Textarea rows={2} value={draft.description}
                onChange={(e) => setDraft({ ...draft, description: e.target.value })}
                placeholder="Cold outreach to engineering leaders after a funding round" />
            </Field>
          </div>
          <StepsEditor steps={draft.steps} onChange={(steps) => setDraft({ ...draft, steps })} />
          {problem && <p className={styles.muted}>{problem}</p>}
          <div className={styles.formActions}>
            <Button variant="ghost" onClick={() => setDraft(null)}>Cancel</Button>
            <Button onClick={save} loading={saving} disabled={Boolean(problem)}>
              {draft.id ? "Save template" : "Create template"}
            </Button>
          </div>
        </Card>
      )}

      {templates.error ? (
        <ErrorState title="Couldn't load templates" message={templates.error.detail} onRetry={templates.refetch} />
      ) : !templates.data ? (
        <div className={styles.stack}>
          <Skeleton width="100%" height={96} />
          <Skeleton width="100%" height={96} />
        </div>
      ) : templates.data.length === 0 ? (
        !draft && (
          <EmptyState
            icon={<Icons.FileTextIcon />}
            title="No templates yet"
            description="Save the steps that work for your team once, and start every campaign from them."
            action={<Button iconLeft={<Icons.PlusIcon />} onClick={newTemplate}>New template</Button>}
          />
        )
      ) : (
        <ul className={styles.templateList}>
          {templates.data.map((t) => (
            <li key={t.id}>
              <Card padding="lg" className={styles.templateCard}>
                <div className={styles.templateHead}>
                  <div>
                    <h3 className={styles.templateName}>{t.name}</h3>
                    <p className={styles.muted}>{summary(t.steps)}</p>
                  </div>
                  <div className={styles.rowActions}>
                    <Button size="sm" variant="secondary" disabled={draft !== null}
                      onClick={() => setDraft({ id: t.id, name: t.name, description: t.description, steps: t.steps.map((s) => ({ ...s })) })}>
                      Edit
                    </Button>
                    <Button size="sm" variant="ghost" onClick={() => setDeleting(t)}>Delete</Button>
                  </div>
                </div>
                {t.description && <p className={styles.bodyText}>{t.description}</p>}
                <ol className={styles.stepList}>
                  {t.steps.map((s, i) => (
                    <li key={i} className={styles.stepRow}>
                      <span className={styles.stepNumber} aria-hidden="true">{i + 1}</span>
                      <span>
                        {i === 0 ? "Opening email" : s.channel === "call" ? "Call" : "Follow-up email"}
                        {i > 0 && <span className={styles.muted}> after {s.delay_business_days} business {s.delay_business_days === 1 ? "day" : "days"}</span>}
                        {s.angle && <span className={styles.muted}>: {s.angle}</span>}
                        {s.allowed_weekdays.length < 5 || s.allowed_weekdays.some((d) => d > 4) ? (
                          <span className={styles.muted}> ({s.allowed_weekdays.map((d) => WEEKDAYS[d]).join(", ")})</span>
                        ) : null}
                      </span>
                    </li>
                  ))}
                </ol>
              </Card>
            </li>
          ))}
        </ul>
      )}

      <Modal
        open={deleting !== null}
        onClose={() => setDeleting(null)}
        title={`Delete ${deleting?.name ?? "this template"}?`}
        description="Campaigns already created from it keep their steps."
        footer={
          <>
            <Button variant="ghost" onClick={() => setDeleting(null)}>Cancel</Button>
            <Button variant="danger" loading={busyDelete} onClick={remove}>Delete template</Button>
          </>
        }
      >
        <p className={styles.muted}>It will no longer be offered when starting a campaign.</p>
      </Modal>
    </div>
  );
}

export default SequenceTemplatesPage;
```

- [ ] **Step 8: The reply desk** — `ReplyDeskPage.tsx`. The selected reply and tab live in the URL (`?tab=&reply=`), so a reminder or alert can link straight to one and Back behaves. `api.deskSend` has one call site, inside the Send button's handler.

```tsx
import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { PageHeader } from "@/components/layout/PageHeader";
import {
  Badge, Button, DataTable, EmptyState, ErrorState, Field, Icons, Input, Modal, Select, Skeleton,
  TabPanel, Tabs, Textarea, WorkingIndicator,
} from "@/components/ui";
import type { Column } from "@/components/ui";
import { useToast } from "@/components/ui/Toast";
import { ConversationTimeline } from "@/components/engagement/ConversationTimeline";
import { CATEGORY, CATEGORY_OPTIONS, reasonText, when, whenDay } from "@/components/engagement/labels";
import { useApi } from "@/hooks/useApi";
import type { AsyncState } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { useEngagementStatus } from "@/app/EngagementContext";
import { ApiError } from "@/lib/api";
import type {
  DeskDecision, DeskItemDetail, DeskQueueItem, DeskScheduledItem, Member, ReplyCategory,
} from "@/lib/types";
import styles from "./Engagement.module.css";

/**
 * The reply desk (spec §9, D22): every reply to a campaign, read and sorted, and nothing sent
 * without a person pressing Send.
 *
 * Needs action holds what a person must answer or decide. Scheduled holds people who asked to hear
 * back on a date or are out of office; their date can be moved or cancelled. Handled is the record
 * of who declined, unsubscribed or was closed, and why.
 */

type TabKey = "needs_action" | "scheduled" | "handled";

function replySubject(subject: string): string {
  const s = subject.trim();
  return /^re:/i.test(s) ? s : `Re: ${s}`;
}

/** The stored suggestion is "subject\n\nbody"; split it back. */
function splitSuggestion(text: string | null, fallbackSubject: string): { subject: string; body: string } {
  if (!text) return { subject: replySubject(fallbackSubject), body: "" };
  const [first, ...rest] = text.split("\n\n");
  return rest.length ? { subject: first, body: rest.join("\n\n") } : { subject: replySubject(fallbackSubject), body: first };
}

export function ReplyDeskPage() {
  const api = useApiClient();
  const status = useEngagementStatus();
  const [params, setParams] = useSearchParams();
  const tab = (params.get("tab") as TabKey) || "needs_action";
  const selected = params.get("reply");
  const [team, setTeam] = useState(false);

  const open = useApi<DeskQueueItem[]>((s) => api.deskQueue("needs_action", team, s), [team]);
  const handled = useApi<DeskQueueItem[]>(
    (s) => (tab === "handled" ? api.deskQueue("handled", team, s) : Promise.resolve([])), [tab, team],
  );
  const scheduled = useApi<DeskScheduledItem[]>((s) => api.deskScheduled(team, s), [team]);

  function go(next: Partial<{ tab: TabKey; reply: string | null }>) {
    const p = new URLSearchParams(params);
    if (next.tab) p.set("tab", next.tab);
    if (next.reply === null) p.delete("reply");
    else if (next.reply) p.set("reply", next.reply);
    setParams(p, { replace: true });
  }

  const tabs = [
    { value: "needs_action", label: "Needs action", count: open.data?.length },
    { value: "scheduled", label: "Scheduled", count: scheduled.data?.length },
    { value: "handled", label: "Handled" },
  ];

  return (
    <div className={styles.page}>
      <PageHeader
        title="Replies"
        description="Answers to your campaigns, read and sorted for you. Nothing is sent until you press Send."
        actions={
          <>
            {status?.can_manage && (
              <>
                <Button variant="secondary" size="sm" aria-pressed={team} onClick={() => setTeam((t) => !t)}>
                  {team ? "Show mine" : "Show team"}
                </Button>
                <Link to="/engagement/replies/settings" className={styles.buttonLink}>Reply settings</Link>
              </>
            )}
          </>
        }
      />

      <Tabs items={tabs} value={tab} onChange={(v) => go({ tab: v as TabKey, reply: null })} idPrefix="desk" />

      <TabPanel id="desk-panel-needs_action" active={tab === "needs_action"}>
        <Queue
          state={open} selected={selected} onSelect={(id) => go({ reply: id })}
          onBack={() => go({ reply: null })}
          onDone={() => { open.refetch(); scheduled.refetch(); go({ reply: null }); }}
          canAssign={Boolean(status?.can_manage)}
          emptyTitle="Nothing needs you right now"
          emptyText="When someone replies with interest, a question or a referral, or a reply needs your decision, it waits here."
        />
      </TabPanel>

      <TabPanel id="desk-panel-scheduled" active={tab === "scheduled"}>
        <Scheduled state={scheduled} onChanged={scheduled.refetch} />
      </TabPanel>

      <TabPanel id="desk-panel-handled" active={tab === "handled"}>
        <Queue
          state={handled} selected={selected} onSelect={(id) => go({ reply: id })}
          onBack={() => go({ reply: null })} onDone={handled.refetch} canAssign={false}
          emptyTitle="Nothing handled yet"
          emptyText="Replies you have closed, and people who declined or unsubscribed, are kept here as the record of why."
        />
      </TabPanel>
    </div>
  );
}

/* ---- the queue and one reply --------------------------------------------------------------- */

function Queue({
  state, selected, onSelect, onBack, onDone, canAssign, emptyTitle, emptyText,
}: {
  state: AsyncState<DeskQueueItem[]>;
  selected: string | null;
  onSelect: (id: string) => void;
  onBack: () => void;
  onDone: () => void;
  canAssign: boolean;
  emptyTitle: string;
  emptyText: string;
}) {
  if (state.error) {
    return <ErrorState title="Couldn't load replies" message={state.error.detail} onRetry={state.refetch} />;
  }
  if (!state.data) {
    return (
      <div className={styles.deskGrid}>
        <div className={styles.stack}>
          {[0, 1, 2, 3].map((i) => <Skeleton key={i} width="100%" height={76} />)}
        </div>
        <Skeleton width="100%" height={420} />
      </div>
    );
  }
  if (state.data.length === 0) {
    return <EmptyState icon={<Icons.InboxIcon />} title={emptyTitle} description={emptyText} />;
  }
  const current = selected ?? state.data[0].id;
  return (
    <div className={styles.deskGrid} data-open={selected ? "detail" : "list"}>
      <ul className={styles.deskList} aria-label="Replies">
        {state.data.map((r) => {
          const cat = CATEGORY[(r.corrected_category ?? r.category) as ReplyCategory];
          return (
            <li key={r.id}>
              <button
                type="button"
                className={styles.deskItem}
                aria-current={r.id === current ? "true" : undefined}
                onClick={() => onSelect(r.id)}
              >
                <span className={styles.deskItemHead}>
                  <span className={styles.personName}>{r.contact_name || r.contact_email}</span>
                  <time className={styles.time} dateTime={r.received_at ?? undefined}>{whenDay(r.received_at)}</time>
                </span>
                <span className={styles.muted}>{r.account_name}</span>
                <span className={styles.deskItemFoot}>
                  {cat && <Badge tone={cat.tone}>{cat.label}</Badge>}
                  {r.decision && <Badge tone="neutral">{decisionLabel(r.decision)}</Badge>}
                  {r.responded_at && !r.decision && <Badge tone="neutral">Answered</Badge>}
                </span>
                <span className={styles.preview}>{r.preview}</span>
              </button>
            </li>
          );
        })}
      </ul>
      <div className={styles.deskDetail}>
        <button type="button" className={styles.backButton} onClick={onBack}>
          <Icons.ChevronLeftIcon aria-hidden /> All replies
        </button>
        <ReplyDetail key={current} id={current} onDone={onDone} canAssign={canAssign} />
      </div>
    </div>
  );
}

function decisionLabel(d: DeskDecision): string {
  return { reengage: "Coming back later", block: "Blocked", close: "Closed", meeting: "Meeting booked" }[d];
}

function ReplyDetail({ id, onDone, canAssign }: { id: string; onDone: () => void; canAssign: boolean }) {
  const api = useApiClient();
  const toast = useToast();
  const item = useApi<DeskItemDetail>((s) => api.deskItem(id, s), [id]);
  const members = useApi<Member[]>(
    (s) => (canAssign ? api.memberDirectory(s) : Promise.resolve([])), [canAssign],
  );
  const [subject, setSubject] = useState("");
  const [body, setBody] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [laterOpen, setLaterOpen] = useState(false);
  const [laterOn, setLaterOn] = useState("");
  const [note, setNote] = useState("");
  const [confirmBlock, setConfirmBlock] = useState(false);

  const d = item.data;
  useEffect(() => {
    if (!d) return;
    const s = splitSuggestion(d.suggested_response, d.subject);
    setSubject(s.subject);
    setBody(s.body);
    setLaterOn(d.resolved_date ?? "");
  }, [d]);

  const conversation = useMemo(() => (d?.conversation ?? []).map((m) => ({
    id: m.id, direction: m.direction, subject: m.subject, body: m.body, at: m.at,
    category: m.id === d?.message_id ? ((d?.corrected_category ?? d?.category) as ReplyCategory) : null,
  })), [d]);

  async function run(label: string, action: () => Promise<unknown>, done?: [string, string?], closes = false) {
    setBusy(label);
    try {
      await action();
      if (done) toast.success(done[0], done[1]);
      if (closes) onDone();
      else item.refetch();
    } catch (err) {
      toast.error("That didn't work", err instanceof ApiError ? err.detail : "Please try again.");
    } finally {
      setBusy(null);
    }
  }

  if (item.error) {
    return <ErrorState title="Couldn't open this reply" message={item.error.detail} onRetry={item.refetch} />;
  }
  if (!d) return <Skeleton width="100%" height={420} />;

  const category = (d.corrected_category ?? d.category) as ReplyCategory;
  const cat = CATEGORY[category];
  const openItem = d.status === "open";

  async function suggest() {
    await run("draft", async () => {
      const fresh = await api.deskDraft(id);
      setSubject(fresh.subject);
      setBody(fresh.body);
      if (fresh.quality_problems.length) {
        toast.error("Check the suggestion", fresh.quality_problems.join("; "));
      }
    });
  }

  async function send() {
    await run("send", async () => {
      const result = await api.deskSend(id, { subject, body });
      if (result.outcome !== "sent") throw new ApiError(409, `Not sent: ${result.reason || result.outcome}.`);
    }, ["Sent", `In the same thread as their reply, from your mailbox.`], category !== "unclear");
  }

  return (
    <article className={styles.detail} aria-labelledby="reply-title">
      <header className={styles.detailHead}>
        <div className={styles.person}>
          <h2 id="reply-title" className={styles.detailTitle}>{d.contact_name || d.contact_email}</h2>
          <span className={styles.muted}>
            {[d.contact_email, d.account_name].filter(Boolean).join(" · ")}
          </span>
        </div>
        <div className={styles.readAs}>
          <span className={styles.muted}>Read as</span>
          <Badge tone={cat.tone}>{cat.label}</Badge>
          {!d.corrected_category && (
            <span className={styles.muted}>{Math.round(d.confidence * 100)}% sure</span>
          )}
        </div>
      </header>

      <div className={styles.correctRow}>
        <Field label="Read it differently?" hint="Your correction is what the reading learns from.">
          <Select
            value={category}
            onChange={(e) => {
              const next = e.target.value as ReplyCategory;
              void run("correct", () => api.deskCorrect(id, next), ["Correction saved"]);
            }}
            options={CATEGORY_OPTIONS}
            disabled={busy !== null}
          />
        </Field>
        {canAssign && (members.data?.length ?? 0) > 0 && (
          <Field label="Assigned to">
            <Select
              value={d.assigned_user_id ?? ""}
              onChange={(e) => void run("assign", () => api.deskAssign(id, e.target.value), ["Reassigned"])}
              options={[
                { value: "", label: "The mailbox owner" },
                ...(members.data ?? []).map((m) => ({ value: m.user_id, label: m.full_name || m.email })),
              ]}
              disabled={busy !== null}
            />
          </Field>
        )}
      </div>

      {d.resolved_date && (
        <p className={styles.notice} role="status">They asked to hear back on {whenDay(d.resolved_date)}.</p>
      )}

      <section aria-label="Conversation" className={styles.conversation}>
        <ConversationTimeline messages={conversation} />
      </section>

      {openItem && (
        <section className={styles.answer} aria-labelledby="answer-title">
          <div className={styles.sectionHead}>
            <h3 id="answer-title" className={styles.sectionTitle}>Your answer</h3>
            <Button size="sm" variant="secondary" iconLeft={<Icons.SparklesIcon />}
              onClick={suggest} disabled={busy !== null}>
              {body ? "Suggest again" : "Suggest a reply"}
            </Button>
          </div>
          {busy === "draft" ? (
            <WorkingIndicator label="Reading the conversation and writing a reply" hint="Usually under 15 seconds." slowAfter={20} />
          ) : (
            <>
              <Field label="Subject">
                <Input value={subject} onChange={(e) => setSubject(e.target.value)} />
              </Field>
              <Field label="Reply" hint="Your signature is added from your mailbox when it sends.">
                <Textarea rows={8} value={body} onChange={(e) => setBody(e.target.value)}
                  placeholder="Write your answer, or ask for a suggestion." />
              </Field>
            </>
          )}
          <div className={styles.formActions}>
            <Button variant="secondary" disabled={busy !== null || !body.trim()} loading={busy === "save"}
              onClick={() => run("save", () => api.deskSaveDraft(id, { subject, body }),
                ["Saved to Drafts", "It is in your mailbox's Drafts folder, in the same thread."])}>
              Save to Drafts
            </Button>
            <Button iconLeft={<Icons.SendIcon />} disabled={busy !== null || !body.trim()} loading={busy === "send"}
              onClick={send}>
              Send
            </Button>
          </div>
        </section>
      )}

      {d.paused_colleagues.length > 0 && (
        <section className={styles.colleagues} aria-labelledby="colleagues-title">
          <h3 id="colleagues-title" className={styles.sectionTitle}>Colleagues paused by this reply</h3>
          <p className={styles.muted}>They stay paused until someone decides. Nothing resumes on its own.</p>
          <ul className={styles.plainList}>
            {d.paused_colleagues.map((c) => (
              <li key={c.enrollment_id} className={styles.colleagueRow}>
                <span>{c.contact_name || "A colleague"}</span>
                {c.actionable ? (
                  <span className={styles.rowActions}>
                    <Button size="sm" variant="ghost" disabled={busy !== null} loading={busy === `resume:${c.enrollment_id}`}
                      onClick={() => run(`resume:${c.enrollment_id}`, () => api.deskColleague(c.enrollment_id, "resume"), ["Resumed"])}>
                      Resume
                    </Button>
                    <Button size="sm" variant="ghost" disabled={busy !== null} loading={busy === `stop:${c.enrollment_id}`}
                      onClick={() => run(`stop:${c.enrollment_id}`, () => api.deskColleague(c.enrollment_id, "stop"), ["Stopped"])}>
                      Stop
                    </Button>
                  </span>
                ) : (
                  <span className={styles.muted}>In a colleague's campaign</span>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}

      {openItem && (
        <section className={styles.decisions} aria-labelledby="decide-title">
          <h3 id="decide-title" className={styles.sectionTitle}>Decide what happens next</h3>
          <Field label="Note (optional)" hint="Kept with the decision.">
            <Input value={note} onChange={(e) => setNote(e.target.value)} maxLength={500} />
          </Field>
          {laterOpen && (
            <div className={styles.laterRow}>
              <Field label="Come back on" hint="Their sequence resumes that morning, in their timezone.">
                <Input type="date" value={laterOn} onChange={(e) => setLaterOn(e.target.value)} />
              </Field>
              <Button disabled={!laterOn || busy !== null} loading={busy === "reengage"}
                onClick={() => run("reengage", () => api.deskDecide(id, { decision: "reengage", reengage_on: laterOn, note }),
                  ["Scheduled", `They come back on ${whenDay(laterOn)}.`], true)}>
                Schedule it
              </Button>
            </div>
          )}
          <div className={styles.decisionButtons}>
            <Button variant="secondary" disabled={busy !== null} loading={busy === "meeting"}
              iconLeft={<Icons.CheckIcon />}
              onClick={() => run("meeting", () => api.deskDecide(id, { decision: "meeting", note }),
                ["Meeting booked", "Recorded on the account; their sequence has stopped."], true)}>
              Meeting booked
            </Button>
            <Button variant="secondary" disabled={busy !== null} aria-expanded={laterOpen}
              onClick={() => setLaterOpen((v) => !v)}>
              Come back later
            </Button>
            <Button variant="ghost" disabled={busy !== null} loading={busy === "close"}
              onClick={() => run("close", () => api.deskDecide(id, { decision: "close", note }), ["Closed"], true)}>
              Close
            </Button>
            <Button variant="ghost" disabled={busy !== null} onClick={() => setConfirmBlock(true)}>
              Do not contact
            </Button>
          </div>
        </section>
      )}

      <Modal
        open={confirmBlock}
        onClose={() => setConfirmBlock(false)}
        title={`Stop contacting ${d.contact_name || d.contact_email}?`}
        description="Their address goes on the do-not-contact list and every sequence they are in stops. A manager can lift it later."
        footer={
          <>
            <Button variant="ghost" onClick={() => setConfirmBlock(false)}>Cancel</Button>
            <Button variant="danger" loading={busy === "block"}
              onClick={() => run("block", async () => {
                await api.deskDecide(id, { decision: "block", note });
                setConfirmBlock(false);
              }, ["Added to do-not-contact"], true)}>
              Do not contact
            </Button>
          </>
        }
      >
        <p className={styles.muted}>{d.contact_email}</p>
      </Modal>
    </article>
  );
}

/* ---- scheduled ------------------------------------------------------------------------------ */

function toLocalInput(iso: string | null): string {
  const d = iso ? new Date(iso) : new Date(Date.now() + 7 * 24 * 60 * 60 * 1000);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function Scheduled({ state, onChanged }: {
  state: AsyncState<DeskScheduledItem[]>;
  onChanged: () => void;
}) {
  const api = useApiClient();
  const toast = useToast();
  const [editing, setEditing] = useState<DeskScheduledItem | null>(null);
  const [date, setDate] = useState("");
  const [busy, setBusy] = useState<string | null>(null);

  async function run(label: string, action: () => Promise<unknown>, done: string) {
    setBusy(label);
    try {
      await action();
      toast.success(done);
      setEditing(null);
      onChanged();
    } catch (err) {
      toast.error("That didn't work", err instanceof ApiError ? err.detail : "Please try again.");
    } finally {
      setBusy(null);
    }
  }

  const columns: Column<DeskScheduledItem>[] = [
    {
      key: "person",
      header: "Person",
      render: (r) => (
        <div className={styles.person}>
          <span className={styles.personName}>{r.contact_name || "Unknown"}</span>
          <span className={styles.muted}>{r.account_name}</span>
        </div>
      ),
    },
    {
      key: "why",
      header: "Why",
      render: (r) => {
        if (r.status_reason === "out_of_office") return "Out of office";
        const why = reasonText(r.status_reason) || "asked to hear back later";
        return why.charAt(0).toUpperCase() + why.slice(1);
      },
    },
    {
      key: "due",
      header: "Back on",
      sortable: true,
      sortValue: (r) => r.due_at ?? "",
      render: (r) => when(r.due_at),
    },
    {
      key: "actions",
      header: <span className={styles.srOnly}>Actions</span>,
      align: "right",
      render: (r) => (
        <div className={styles.rowActions}>
          <Button size="sm" variant="ghost" disabled={busy !== null}
            onClick={() => { setEditing(r); setDate(toLocalInput(r.due_at)); }}>
            Change date
          </Button>
          <Button size="sm" variant="ghost" disabled={busy !== null} loading={busy === `cancel:${r.enrollment_id}`}
            onClick={() => run(`cancel:${r.enrollment_id}`, () => api.cancelScheduled(r.enrollment_id), `Cancelled: ${r.contact_name}`)}>
            Cancel
          </Button>
        </div>
      ),
    },
  ];

  if (state.error) {
    return <ErrorState title="Couldn't load scheduled contacts" message={state.error.detail} onRetry={state.refetch} />;
  }
  return (
    <>
      <DataTable
        columns={columns}
        rows={state.data ?? []}
        getRowKey={(r) => r.enrollment_id}
        loading={!state.data}
        caption="People waiting for a date"
        empty={
          <EmptyState compact icon={<Icons.InboxIcon />} title="Nobody is scheduled"
            description="People who ask to hear back later, or who are out of office, wait here until their date." />
        }
      />
      <Modal
        open={editing !== null}
        onClose={() => setEditing(null)}
        title={`New date for ${editing?.contact_name ?? "this contact"}`}
        description="Their sequence resumes at this time instead."
        footer={
          <>
            <Button variant="ghost" onClick={() => setEditing(null)}>Cancel</Button>
            <Button disabled={!date} loading={editing !== null && busy === `move:${editing.enrollment_id}`}
              onClick={() => editing && run(`move:${editing.enrollment_id}`,
                () => api.rescheduleScheduled(editing.enrollment_id, new Date(date).toISOString()),
                `New date: ${when(new Date(date).toISOString())}`)}>
              Save date
            </Button>
          </>
        }
      >
        <Field label="Resume on" hint="Your local time.">
          <Input type="datetime-local" value={date} onChange={(e) => setDate(e.target.value)} />
        </Field>
      </Modal>
    </>
  );
}

export default ReplyDeskPage;
```

- [ ] **Step 9: Reply settings** — `pages/settings/EngagementSettings.tsx`:

```tsx
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { PageHeader } from "@/components/layout/PageHeader";
import { Button, Card, ErrorState, Field, Icons, Input, Skeleton } from "@/components/ui";
import { useToast } from "@/components/ui/Toast";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import type { WorkspaceEngagementSettings } from "@/lib/types";
import { TrainingConsentCard } from "./TrainingConsentCard";
import styles from "@/pages/engagement/Engagement.module.css";

/**
 * How replies are handled for the whole workspace (spec §6, D23): the confidence bar, the range an
 * SDR may choose inside, how long an interested buyer may wait before a reminder, and how long an
 * out-of-office with no return date pauses a sequence.
 *
 * Managers and up change these; everyone can read them. It is its own page rather than a card in
 * Settings because Settings is admin-only and this is a team lead's decision. The limits are the
 * server's (`engagement/settings.validate_update`), repeated here only so the form can say so
 * before saving.
 */

const HARD_MIN = 0.5;
const HARD_MAX = 0.99;

type Form = Record<keyof Omit<WorkspaceEngagementSettings, "can_edit">, string>;

function toForm(s: WorkspaceEngagementSettings): Form {
  return {
    reply_confidence_default: s.reply_confidence_default.toFixed(2),
    reply_confidence_min: s.reply_confidence_min.toFixed(2),
    reply_confidence_max: s.reply_confidence_max.toFixed(2),
    reply_reminder_business_hours: String(s.reply_reminder_business_hours),
    ooo_default_days: String(s.ooo_default_days),
  };
}

function problemWith(f: Form): string | null {
  const [def, min, max] = [f.reply_confidence_default, f.reply_confidence_min, f.reply_confidence_max].map(Number);
  if ([def, min, max].some((n) => Number.isNaN(n) || n < HARD_MIN || n > HARD_MAX)) {
    return `Confidence values run from ${HARD_MIN.toFixed(2)} to ${HARD_MAX.toFixed(2)}.`;
  }
  if (min > max) return "The lowest value SDRs may choose cannot be above the highest.";
  if (def < min || def > max) return "The workspace default must sit inside the range SDRs may choose from.";
  const hours = Number(f.reply_reminder_business_hours);
  if (!Number.isInteger(hours) || hours < 1 || hours > 72) return "The reminder is between 1 and 72 business hours.";
  const days = Number(f.ooo_default_days);
  if (!Number.isInteger(days) || days < 1 || days > 60) return "Out of office pauses for 1 to 60 days.";
  return null;
}

export function EngagementSettingsPage() {
  const api = useApiClient();
  const toast = useToast();
  const settings = useApi<WorkspaceEngagementSettings>((s) => api.engagementSettings(s), []);
  const [form, setForm] = useState<Form | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (settings.data) setForm(toForm(settings.data));
  }, [settings.data]);

  if (settings.error) {
    return <ErrorState title="Couldn't load reply settings" message={settings.error.detail} onRetry={settings.refetch} />;
  }
  if (!settings.data || !form) return <Skeleton width="100%" height={360} />;

  const editable = settings.data.can_edit;
  const problem = problemWith(form);
  const dirty = JSON.stringify(form) !== JSON.stringify(toForm(settings.data));
  const set = (key: keyof Form) => (e: { target: { value: string } }) => setForm({ ...form, [key]: e.target.value });

  async function save() {
    if (!form || problem) return;
    setSaving(true);
    try {
      const next = await api.updateEngagementSettings({
        reply_confidence_default: Number(form.reply_confidence_default),
        reply_confidence_min: Number(form.reply_confidence_min),
        reply_confidence_max: Number(form.reply_confidence_max),
        reply_reminder_business_hours: Number(form.reply_reminder_business_hours),
        ooo_default_days: Number(form.ooo_default_days),
      });
      settings.setData(next);
      toast.success("Reply settings saved", "They apply to the next reply that arrives.");
    } catch (err) {
      toast.error("Not saved", err instanceof ApiError ? err.detail : "Please try again.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className={styles.page}>
      <Link to="/engagement/replies" className={styles.back}>
        <Icons.ChevronLeftIcon aria-hidden /> Replies
      </Link>
      <PageHeader
        title="Reply settings"
        description="How sure the AI must be before it acts on a reply without anyone, and when to remind the team."
      />

      <Card padding="lg" className={styles.section}>
        <h2 className={styles.sectionTitle}>Acting on replies without you</h2>
        <p className={styles.muted}>
          A clear "not now", "no thanks" or "unsubscribe" read at or above the bar is acted on
          straight away: the sequence snoozes or stops. Below it, the reply waits in Replies for a
          person. Each SDR can set their own bar for their mailbox, inside the range below.
        </p>
        <fieldset className={`${styles.fields} ${styles.bare}`} disabled={!editable}>
          <Field label="Workspace default" hint="Used when an SDR has not set their own.">
            <Input type="number" inputMode="decimal" step="0.01" min={HARD_MIN} max={HARD_MAX}
              value={form.reply_confidence_default} onChange={set("reply_confidence_default")} />
          </Field>
          <Field label="Lowest an SDR may choose" hint={`Never below ${HARD_MIN.toFixed(2)}.`}>
            <Input type="number" inputMode="decimal" step="0.01" min={HARD_MIN} max={HARD_MAX}
              value={form.reply_confidence_min} onChange={set("reply_confidence_min")} />
          </Field>
          <Field label="Highest an SDR may choose" hint={`Never above ${HARD_MAX.toFixed(2)}.`}>
            <Input type="number" inputMode="decimal" step="0.01" min={HARD_MIN} max={HARD_MAX}
              value={form.reply_confidence_max} onChange={set("reply_confidence_max")} />
          </Field>
        </fieldset>
      </Card>

      <Card padding="lg" className={styles.section}>
        <h2 className={styles.sectionTitle}>Timing</h2>
        <fieldset className={`${styles.fields} ${styles.bare}`} disabled={!editable}>
          <Field label="Remind after" hint="Business hours an interested buyer or a question may wait for an answer.">
            <Input type="number" inputMode="numeric" min={1} max={72}
              value={form.reply_reminder_business_hours} onChange={set("reply_reminder_business_hours")} />
          </Field>
          <Field label="Out of office with no return date" hint="Days the sequence pauses before it resumes.">
            <Input type="number" inputMode="numeric" min={1} max={60}
              value={form.ooo_default_days} onChange={set("ooo_default_days")} />
          </Field>
        </fieldset>
      </Card>

      {editable ? (
        <>
          {problem && dirty && <p className={styles.formError} role="alert">{problem}</p>}
          <div className={styles.formActions}>
            <Button variant="ghost" disabled={!dirty || saving} onClick={() => setForm(toForm(settings.data as WorkspaceEngagementSettings))}>
              Discard changes
            </Button>
            <Button onClick={save} loading={saving} disabled={!dirty || Boolean(problem)}>Save settings</Button>
          </div>
        </>
      ) : (
        <p className={styles.muted}>Only managers and above can change these.</p>
      )}

      <TrainingConsentCard />
    </div>
  );
}

export default EngagementSettingsPage;
```

- [ ] **Step 10: Routes.** Apply to `frontend/src/App.tsx`. Every `/engagement/*` route is inside `RequireEngine` and its module gate; `module.cadences` gates templates, as spec §10 says, and its fallback is the old Cadences page.

```diff
diff --git a/frontend/src/App.tsx b/frontend/src/App.tsx
index 51eab90..a005609 100644
--- a/frontend/src/App.tsx
+++ b/frontend/src/App.tsx
@@ -6,4 +6,5 @@ import { SignalWindowProvider } from "@/app/SignalWindowContext";
 import { AuthProvider, useAuth } from "@/app/AuthContext";
 import { isLocked, switchNotice, useEntitlements } from "@/app/EntitlementsContext";
+import { RequireEngine } from "@/app/EngagementContext";
 import { FeatureUnavailable } from "@/components/FeatureUnavailable";
 import { RequirePlatformAdmin } from "@/app/RequirePlatformAdmin";
@@ -67,4 +68,22 @@ const CadencesPage = lazyPage(() => import("@/pages/CadencesPage"), "CadencesPag
 const MailboxesPage = lazyPage(() => import("@/pages/engagement/MailboxesPage"), "MailboxesPage");
 const DataUsePage = lazyPage(() => import("@/pages/DataUsePage"), "DataUsePage");
+// The engagement engine's screens (spec §9). Each route is behind `RequireEngine` as well as its
+// module gate: while the engine is dark every API behind them answers 404.
+const EngagementCampaignsPage = lazyPage(
+  () => import("@/pages/engagement/CampaignsPage"), "EngagementCampaignsPage",
+);
+const CampaignBuilder = lazyPage(
+  () => import("@/pages/engagement/CampaignBuilder"), "CampaignBuilder",
+);
+const CampaignDetailPage = lazyPage(
+  () => import("@/pages/engagement/CampaignDetailPage"), "CampaignDetailPage",
+);
+const ReplyDeskPage = lazyPage(() => import("@/pages/engagement/ReplyDeskPage"), "ReplyDeskPage");
+const SequenceTemplatesPage = lazyPage(
+  () => import("@/pages/engagement/SequenceTemplatesPage"), "SequenceTemplatesPage",
+);
+const EngagementSettingsPage = lazyPage(
+  () => import("@/pages/settings/EngagementSettings"), "EngagementSettingsPage",
+);
 const DoNotContactPage = lazyPage(
   () => import("@/pages/engagement/DoNotContactPage"), "DoNotContactPage",
@@ -328,4 +347,66 @@ export function App() {
                   }
                 />
+                {/* The engagement engine. Every member works their own campaigns and replies; the
+                    server scopes each read to the caller's mailboxes. */}
+                <Route
+                  path="/engagement/campaigns"
+                  element={
+                    <RequireEngine>
+                      <RequireCapability capability="module.campaigns" name="Campaigns">
+                        <EngagementCampaignsPage />
+                      </RequireCapability>
+                    </RequireEngine>
+                  }
+                />
+                <Route
+                  path="/engagement/campaigns/new"
+                  element={
+                    <RequireEngine>
+                      <RequireCapability capability="module.campaigns" name="Campaigns">
+                        <CampaignBuilder />
+                      </RequireCapability>
+                    </RequireEngine>
+                  }
+                />
+                <Route
+                  path="/engagement/campaigns/:campaignId"
+                  element={
+                    <RequireEngine>
+                      <RequireCapability capability="module.campaigns" name="Campaigns">
+                        <CampaignDetailPage />
+                      </RequireCapability>
+                    </RequireEngine>
+                  }
+                />
+                <Route
+                  path="/engagement/replies"
+                  element={
+                    <RequireEngine>
+                      <RequireCapability capability="module.campaigns" name="Replies">
+                        <ReplyDeskPage />
+                      </RequireCapability>
+                    </RequireEngine>
+                  }
+                />
+                <Route
+                  path="/engagement/replies/settings"
+                  element={
+                    <RequireEngine>
+                      <RequireCapability capability="module.campaigns" name="Reply settings">
+                        <EngagementSettingsPage />
+                      </RequireCapability>
+                    </RequireEngine>
+                  }
+                />
+                <Route
+                  path="/engagement/templates"
+                  element={
+                    <RequireEngine fallback="/cadences">
+                      <RequireCapability capability="module.cadences" name="Sequence templates">
+                        <SequenceTemplatesPage />
+                      </RequireCapability>
+                    </RequireEngine>
+                  }
+                />
                 <Route
                   path="/cadences"
```

- [ ] **Step 11: Run** `npm run typecheck && npm run build` — expected PASS.

- [ ] **Step 12: Commit**

```bash
git add frontend/src/pages/engagement frontend/src/pages/settings/EngagementSettings.tsx frontend/src/App.tsx
git commit -m "feat(ui): campaigns, review, launch, templates, the reply desk and reply settings"
```

---

### Task 5: The account page and the mailbox copy

- [ ] **Step 1: The Emails tab**, present only with the engine on. Apply to `frontend/src/pages/AccountDetailPage.tsx`:

```diff
diff --git a/frontend/src/pages/AccountDetailPage.tsx b/frontend/src/pages/AccountDetailPage.tsx
index 420ec27..6594e01 100644
--- a/frontend/src/pages/AccountDetailPage.tsx
+++ b/frontend/src/pages/AccountDetailPage.tsx
@@ -29,4 +29,6 @@ import {
 import { CallConsole } from "@/components/CallConsole";
 import { EmailComposer } from "@/components/EmailComposer";
+import { AccountConversations } from "@/components/engagement/AccountConversations";
+import { useEngineOn } from "@/app/EngagementContext";
 import { useApi } from "@/hooks/useApi";
 import { useApiClient, useAuth } from "@/app/AuthContext";
@@ -52,5 +54,5 @@ import type {
 import styles from "./AccountDetailPage.module.css";
 
-type Tab = "overview" | "contacts" | "signals" | "lookalikes" | "actions";
+type Tab = "overview" | "contacts" | "signals" | "emails" | "lookalikes" | "actions";
 
 interface LookalikeState {
@@ -104,4 +106,5 @@ export function AccountDetailPage() {
   const { windowDays } = useSignalWindow();
   const [tab, setTab] = useState<Tab>("overview");
+  const engineOn = useEngineOn() === true;
   const [running, setRunning] = useState(false);
   const [findingContacts, setFindingContacts] = useState(false);
@@ -628,4 +631,6 @@ export function AccountDetailPage() {
           { value: "contacts", label: "Contacts", count: contacts.data?.length },
           { value: "signals", label: "Signals", count: signals.data?.length },
+          // Every email to and from this account's people, once the engagement engine is on.
+          ...(engineOn ? [{ value: "emails", label: "Emails" }] : []),
           { value: "lookalikes", label: "Lookalikes" },
           { value: "actions", label: "AI Actions" },
@@ -994,4 +999,10 @@ export function AccountDetailPage() {
       </TabPanel>
 
+      {engineOn && (
+        <TabPanel id="panel-emails" active={tab === "emails"}>
+          <AccountConversations accountId={id} />
+        </TabPanel>
+      )}
+
       <TabPanel id="panel-lookalikes" active={tab === "lookalikes"}>
         <div className={styles.panel}>
```

- [ ] **Step 2: My mailboxes says what a connected mailbox now does** (one-off sends and drafts go through it too):

```diff
diff --git a/frontend/src/pages/engagement/MailboxesPage.tsx b/frontend/src/pages/engagement/MailboxesPage.tsx
index b171910..cfecd0c 100644
--- a/frontend/src/pages/engagement/MailboxesPage.tsx
+++ b/frontend/src/pages/engagement/MailboxesPage.tsx
@@ -126,5 +126,5 @@ export function MailboxesPage() {
       <PageHeader
         title="My mailboxes"
-        description="Connect the Gmail or Microsoft 365 mailbox you prospect from. Campaign emails send from it, replies are read in it, and nothing else in it is stored."
+        description="Connect the Gmail or Microsoft 365 mailbox you prospect from. Emails you send from here, one at a time or in a campaign, go out from it. Replies are read in it, and nothing else in it is stored."
         actions={
           <>
@@ -177,5 +177,5 @@ export function MailboxesPage() {
             icon={<Icons.MailIcon />}
             title={team ? "Nobody on the team has connected a mailbox" : "No mailbox connected yet"}
-            description="Connect one above. Until then campaigns cannot send on your behalf."
+            description="Connect one above. Until then nothing can be sent from here on your behalf."
           />
         }
```

- [ ] **Step 3: Commit**

```bash
git add frontend/src/pages/AccountDetailPage.tsx frontend/src/pages/engagement/MailboxesPage.tsx
git commit -m "feat(ui): every email to an account in one timeline"
```

---

### Task 6: The tests

- [ ] **Step 1: The server reads** — `tests/test_engagement_screens_api.py`:

```python
"""What the engagement screens read (spec §9): the engine switch, the candidates a campaign is built
from, the conversation timeline, names on every row, and the scheduled contacts a rep can move.

Real rows throughout; the campaign flow reuses the phase 08 helpers and the phase 09 `Mailbox`
provider double.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from nexus.core.config import get_settings
from tests.conftest import auth, principal_from_token, signup, tenant_session
from tests.test_engagement_replies import Mailbox, _mail, _sync
from tests.test_engagement_sequences import STEPS, _enrollment, _launched, _run


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


async def _member(client, owner_token: str, email: str, role: str) -> str:
    r = await client.post("/api/workspace/members", headers=auth(owner_token), json={
        "email": email, "full_name": email.split("@")[0], "password": "password123",
        "role": role})
    assert r.status_code == 201, r.text
    r = await client.post("/api/auth/login", json={"email": email, "password": "password123"})
    return r.json()["access_token"]


# ---- the switch ----------------------------------------------------------------------------------

async def test_the_status_answers_while_everything_else_is_dark(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "engagement_campaigns_enabled", False)
    token = await signup(client, slug="scrstatus", email="o@scrstatus.com", company="S")
    r = await client.get("/api/engagement/settings/status", headers=auth(token))
    assert r.status_code == 200 and r.json() == {"engine_on": False, "can_manage": True}
    # The screens it gates are genuinely unreachable while it says so.
    assert (await client.get("/api/engagement/campaigns", headers=auth(token))).status_code == 404

    monkeypatch.setattr(get_settings(), "engagement_campaigns_enabled", True)
    rep = await _member(client, token, "rep@scrstatus.com", "rep")
    body = (await client.get("/api/engagement/settings/status", headers=auth(rep))).json()
    assert body == {"engine_on": True, "can_manage": False}


# ---- candidates ----------------------------------------------------------------------------------

async def _book(tid: str):
    """Two accounts on a saved list, one account off it, and people of several kinds."""
    from nexus.core.db import utcnow
    from nexus.engagement.suppression.service import suppress
    from nexus.models.account import Account, Contact
    from nexus.models.workflow import ListItem, ProspectList

    async with tenant_session(tid) as ts:
        acme, beta, gamma = (Account(name=n, domain=d) for n, d in (
            ("Acme", "acme.io"), ("Beta", "beta.io"), ("Gamma", "gamma.io")))
        for a in (acme, beta, gamma):
            ts.add(a)
        await ts.flush()
        people = {
            "vp": Contact(account_id=acme.id, full_name="Ava VP", title="VP Engineering",
                          seniority="vp", email="ava@acme.io"),
            "ic": Contact(account_id=acme.id, full_name="Ian IC", title="Software Engineer",
                          seniority="entry", email="ian@acme.io"),
            "noemail": Contact(account_id=acme.id, full_name="Nora None", title="CTO",
                               seniority="c_suite", email=None),
            "gone": Contact(account_id=acme.id, full_name="Gail Gone", title="VP Sales",
                            seniority="vp", email="gail@acme.io", deleted_at=utcnow()),
            "named": Contact(account_id=beta.id, full_name="Ned Named", title="Head of Data",
                             seniority="director", email="ned@beta.io"),
            "other": Contact(account_id=beta.id, full_name="Olga Other", title="VP Product",
                             seniority="vp", email="olga@beta.io"),
            "offlist": Contact(account_id=gamma.id, full_name="Omar Off", title="VP Engineering",
                               seniority="vp", email="omar@gamma.io"),
        }
        for c in people.values():
            ts.add(c)
        await ts.flush()
        saved = ProspectList(name="Q4 targets")
        ts.add(saved)
        await ts.flush()
        # Acme as a whole account; at Beta, one named person only.
        ts.add(ListItem(list_id=saved.id, account_id=acme.id))
        ts.add(ListItem(list_id=saved.id, account_id=beta.id, contact_id=people["named"].id))
        await ts.flush()
        await suppress(ts, email="ian@acme.io", reason="unsubscribed")
        return saved.id, {k: v.id for k, v in people.items()}


async def test_a_saved_list_expands_to_the_people_at_its_accounts(client, engine_on):
    token = await signup(client, slug="scrcand", email="o@scrcand.com", company="C")
    me = principal_from_token(token)
    list_id, ids = await _book(me.tenant_id)

    r = await client.get("/api/engagement/candidates", headers=auth(token),
                         params={"list_id": list_id})
    assert r.status_code == 200, r.text
    got = {c["contact_id"]: c for c in r.json()}
    # A whole account brings everyone there with an address; a named item brings that person only.
    # No address, a deleted contact and someone off the list are never offered.
    assert set(got) == {ids["vp"], ids["ic"], ids["named"]}
    # Blocked people are shown and marked, so the SDR can see why they will not be added.
    assert got[ids["ic"]]["blocked"] is True and got[ids["vp"]]["blocked"] is False
    assert got[ids["named"]]["account_name"] == "Beta"


async def test_title_and_seniority_narrow_the_expansion(client, engine_on):
    token = await signup(client, slug="scrfilt", email="o@scrfilt.com", company="F")
    me = principal_from_token(token)
    list_id, ids = await _book(me.tenant_id)
    by_title = (await client.get("/api/engagement/candidates", headers=auth(token), params={
        "list_id": list_id, "title": "vp, head of"})).json()
    assert {c["contact_id"] for c in by_title} == {ids["vp"], ids["named"]}
    by_level = (await client.get("/api/engagement/candidates", headers=auth(token), params={
        "seniority": "vp"})).json()
    # Without a list, the whole book: every VP with an address, on the list or not.
    assert {c["contact_id"] for c in by_level} == {ids["vp"], ids["other"], ids["offlist"]}
    by_text = (await client.get("/api/engagement/candidates", headers=auth(token), params={
        "q": "gamma"})).json()
    assert [c["contact_id"] for c in by_text] == [ids["offlist"]]


# ---- names on the campaign rows ------------------------------------------------------------------

async def test_the_review_queue_and_the_contacts_say_who_each_row_is(client, engine_on,
                                                                   monkeypatch):
    from nexus.models.account import Account, Contact
    from nexus.models.engagement import MailboxConnection
    from tests.test_engagement_sequences import seed_relevance_profile

    monkeypatch.setattr(get_settings(), "billing_enforcement", "off")
    token = await signup(client, slug="scrnames", email="sam@scrnames.com", company="N")
    me = principal_from_token(token)
    async with tenant_session(me.tenant_id) as ts:
        await seed_relevance_profile(ts)
        mailbox = MailboxConnection(owner_user_id=me.user_id, provider="google",
                                    email="sam@scrnames.com", status="connected")
        account = Account(name="Acme Robotics", domain="acme.io")
        ts.add(mailbox)
        ts.add(account)
        await ts.flush()
        contact = Contact(account_id=account.id, full_name="Jane Buyer", email="jane@acme.io",
                          title="VP Engineering")
        ts.add(contact)
        await ts.flush()
        mailbox_id, contact_id = mailbox.id, contact.id

    created = (await client.post("/api/engagement/campaigns", headers=auth(token), json={
        "name": "Q4", "mailbox_id": mailbox_id, "steps": STEPS})).json()
    await client.post(f"/api/engagement/campaigns/{created['id']}/contacts", headers=auth(token),
                      json={"contact_ids": [contact_id]})
    expected = ("Jane Buyer", "jane@acme.io", "VP Engineering", "Acme Robotics")
    review = (await client.get(f"/api/engagement/campaigns/{created['id']}/review",
                               headers=auth(token))).json()
    row = review[0]
    assert (row["contact_name"], row["contact_email"], row["contact_title"],
            row["account_name"]) == expected
    enrollments = (await client.get(f"/api/engagement/campaigns/{created['id']}/enrollments",
                                    headers=auth(token))).json()
    row = enrollments[0]
    assert (row["contact_name"], row["contact_email"], row["contact_title"],
            row["account_name"]) == expected


# ---- the conversation timeline -------------------------------------------------------------------

async def test_the_timeline_shows_what_was_sent_and_what_came_back(mailbox_double, monkeypatch):
    from email import message_from_bytes

    from nexus.engagement.sequences.candidates import timeline

    tid, campaign_id = await _launched("scrtime", monkeypatch)
    enrollment = await _enrollment(tid)
    await _run(tid, enrollment.id, enrollment.next_action_at + timedelta(seconds=1))
    our_id = message_from_bytes(mailbox_double.delivered[0])["Message-ID"]
    # The reply arrives after the send, in real time: the fixture's default date is in the past.
    arrived = datetime.now(UTC) + timedelta(minutes=5)
    mailbox_double.arrive(_mail(sender="jane0@acme.io", in_reply_to=our_id, when=arrived,
                                body="Sounds interesting, let's talk next week."), when=arrived)
    await _sync(tid, enrollment.mailbox_connection_id)
    async with tenant_session(tid) as ts:
        by_contact = await timeline(ts, contact_id=enrollment.contact_id)
        by_account = await timeline(ts, account_id=enrollment.account_id)
    assert [e.direction for e in by_contact] == ["out", "in"]
    assert by_contact[0].campaign_id == campaign_id and by_contact[0].campaign_name == "Q4"
    # The reply carries what it was read as, so the timeline says "interested" without a click.
    assert by_contact[1].category == "interested"
    assert [e.message_id for e in by_account] == [e.message_id for e in by_contact]


# ---- scheduled contacts --------------------------------------------------------------------------

async def test_a_scheduled_contact_can_be_given_a_new_date_or_cancelled(mailbox_double,
                                                                       monkeypatch):
    from nexus.engagement.desk.service import DeskError, cancel_scheduled, reschedule
    from nexus.models.engagement import EngagementEnrollment

    tid, _campaign = await _launched("scrsched", monkeypatch)
    enrollment = await _enrollment(tid)
    await _run(tid, enrollment.id, enrollment.next_action_at + timedelta(seconds=1))
    from email import message_from_bytes

    our_id = message_from_bytes(mailbox_double.delivered[0])["Message-ID"]
    mailbox_double.arrive(_mail(sender="jane0@acme.io", in_reply_to=our_id,
                                body="Not now — try me in June please."))
    await _sync(tid, enrollment.mailbox_connection_id)
    new_date = datetime.now(UTC) + timedelta(days=30)
    async with tenant_session(tid) as ts:
        row = await ts.get(EngagementEnrollment, enrollment.id)
        assert row.status == "snoozed"
        await reschedule(ts, row, new_date, user_id="u1")
        assert row.snoozed_until == new_date
        with pytest.raises(DeskError):
            await reschedule(ts, row, datetime.now(UTC) - timedelta(days=1), user_id="u1")
        await cancel_scheduled(ts, row, user_id="u1")
        assert row.status == "stopped"
        # A stopped contact is no longer waiting for anything.
        with pytest.raises(DeskError):
            await reschedule(ts, row, new_date, user_id="u1")


async def test_a_rep_cannot_steer_a_contact_in_a_colleagues_mailbox(client, engine_on):
    from nexus.models.account import Account, Contact
    from nexus.models.engagement import (
        EngagementCampaign,
        EngagementEnrollment,
        MailboxConnection,
    )

    owner = await signup(client, slug="scrown", email="boss@scrown.com", company="O")
    me = principal_from_token(owner)
    ann = await _member(client, owner, "ann@scrown.com", "rep")
    bob = await _member(client, owner, "bob@scrown.com", "rep")
    ann_id = principal_from_token(ann).user_id
    async with tenant_session(me.tenant_id) as ts:
        mailbox = MailboxConnection(owner_user_id=ann_id, provider="google",
                                    email="ann@scrown.com", status="connected")
        account = Account(name="Acme", domain="acme.io")
        ts.add(mailbox)
        ts.add(account)
        await ts.flush()
        contact = Contact(account_id=account.id, full_name="Jane", email="jane@acme.io")
        campaign = EngagementCampaign(name="Ann's", owner_user_id=ann_id,
                                      mailbox_connection_id=mailbox.id, status="active")
        ts.add(contact)
        ts.add(campaign)
        await ts.flush()
        enrollment = EngagementEnrollment(
            campaign_id=campaign.id, contact_id=contact.id, account_id=account.id,
            mailbox_connection_id=mailbox.id, status="snoozed",
            snoozed_until=datetime.now(UTC) + timedelta(days=5))
        ts.add(enrollment)
        await ts.flush()
        enrollment_id = enrollment.id

    later = (datetime.now(UTC) + timedelta(days=20)).isoformat()
    for token, expected in ((bob, 404), (ann, 204)):
        r = await client.post(f"/api/engagement/desk/scheduled/{enrollment_id}/reschedule",
                              headers=auth(token), json={"when": later})
        assert r.status_code == expected, r.text
    # The colleague route had no ownership check at all; it now answers like the rest.
    r = await client.post(f"/api/engagement/desk/colleagues/{enrollment_id}/stop",
                          headers=auth(bob))
    assert r.status_code == 404
```

- [ ] **Step 2: The structural promises** — `tests/test_engagement_screens_ui.py`. There is no frontend test runner, so like `test_plan_gated_nav.py` these read the source:

```python
"""The engagement screens, checked by reading their source (spec §9).

There is no frontend test runner here, so like `test_plan_gated_nav.py` these pin the structural
promises the screens make: which engine a nav item belongs to, that every new route waits for the
engine switch, that nothing sends without a click, and that long AI work shows `WorkingIndicator`.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "frontend" / "src"
NAV = SRC / "app" / "nav.tsx"
APP = SRC / "App.tsx"
PAGES = SRC / "pages" / "engagement"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _nav_block(route: str) -> str:
    match = re.search(r'\{[^{}]*to:\s*"' + re.escape(route) + r'"[^{}]*\}', _read(NAV), re.S)
    assert match, f"{route} is not in NAV_ITEMS"
    return match.group(0)


def test_the_new_screens_appear_only_when_the_engine_is_on():
    for route in ("/engagement/campaigns", "/engagement/replies", "/engagement/templates"):
        assert 'engine: "on"' in _nav_block(route), f"{route} must wait for the engine switch"
    # The old engine's pages leave the menu at the same moment, so a workspace never sees both.
    for route in ("/campaigns", "/cadences"):
        assert 'engine: "off"' in _nav_block(route), f"{route} must leave when the engine is on"


def test_can_see_reads_the_engine_and_unknown_keeps_the_old_pages():
    source = _read(NAV)
    assert 'item.engine === "on" && engineOn !== true' in source
    assert 'item.engine === "off" && engineOn === true' in source
    assert "canSee(item, role, isPlatformAdmin, engineOn)" in _read(
        SRC / "components" / "layout" / "Sidebar.tsx")


def test_every_engagement_route_waits_for_the_engine():
    app = _read(APP)
    routes = re.findall(r'<Route\s+path="(/engagement/[^"]*)"\s+element=\{(.*?)\}\s*/>', app, re.S)
    assert {r for r, _ in routes} >= {
        "/engagement/campaigns", "/engagement/campaigns/new", "/engagement/campaigns/:campaignId",
        "/engagement/replies", "/engagement/replies/settings", "/engagement/templates",
    }
    for route, element in routes:
        assert "<RequireEngine" in element, f"{route} renders without checking the engine switch"
        assert "RequireCapability" in element, f"{route} is not behind its module gate"


def test_an_unreadable_engine_status_reads_as_off():
    """The new pages 404 while the engine is dark, so a status we could not read must not offer
    them; the old pages work either way."""
    source = _read(SRC / "app" / "EngagementContext.tsx")
    assert "state.error ? { engine_on: false" in source


def test_a_reply_is_sent_only_by_the_send_button():
    """D22: the AI drafts, a person presses Send. One call site, inside the click handler."""
    desk = _read(PAGES / "ReplyDeskPage.tsx")
    assert desk.count("api.deskSend(") == 1
    send_handler = desk[desk.index("async function send()"):]
    assert send_handler.index("api.deskSend(") < send_handler.index("}, [")
    assert "onClick={send}" in desk


def test_long_ai_work_shows_the_working_indicator():
    for page, reason in (("ReviewQueue.tsx", "writing and rewriting drafts"),
                         ("ReplyDeskPage.tsx", "suggesting a reply")):
        assert "<WorkingIndicator" in _read(PAGES / page), f"{page}: {reason} needs WorkingIndicator"


def test_launch_is_held_while_the_balance_cannot_cover_the_worst_case():
    panel = _read(PAGES / "LaunchPanel.tsx")
    assert "const blocked = e.gate_applies && !e.covered;" in panel
    assert "disabled={blocked" in panel


def test_the_engagement_pages_use_tokens_not_inline_styles():
    offenders = [p.name for p in list(PAGES.glob("*.tsx"))
                 + list((SRC / "components" / "engagement").glob("*.tsx"))
                 + [SRC / "pages" / "settings" / "EngagementSettings.tsx"]
                 if "style={{" in _read(p)]
    assert not offenders, f"inline styles in {offenders}: use the CSS modules and tokens"


def test_the_account_page_offers_emails_only_with_the_engine_on():
    page = _read(SRC / "pages" / "AccountDetailPage.tsx")
    assert '...(engineOn ? [{ value: "emails", label: "Emails" }] : [])' in page
    assert "<AccountConversations accountId={id} />" in page
```

- [ ] **Step 3: Run** `pytest tests/test_engagement_screens_api.py tests/test_engagement_screens_ui.py tests/test_plan_gated_nav.py tests/test_engagement_desk.py -q -n 4` — expected PASS; `ruff check nexus tests` — clean.

---

### Task 7: See it work

Unit tests prove the parts; the flow is proved in a browser.

- [ ] **Step 1:** Run the branch in its own container with the engine on and a throwaway database: `NEXUS_ENGAGEMENT_CAMPAIGNS_ENABLED=true`, `NEXUS_DATABASE_URL=sqlite+aiosqlite:////tmp/preview.db`, `NEXUS_LLM_PROVIDER=stub`, serving the built bundle. Never point it at a shared database.
- [ ] **Step 2:** Seed a workspace: a relevance profile, a connected mailbox row, three accounts with people, a saved list, a template, a campaign in review, and a running campaign with an interested, a question, a later and a declined reply. Sign in by injecting the signup token into `localStorage["nexus_session"]`, never by typing credentials.
- [ ] **Step 3:** Check, in order: the nav shows Campaigns, Replies and Sequence templates and not the old two; a campaign builds from a template and lands on People; people added from the saved list, including the duplicate-outreach warning; an opening email edited and approved ("Approve with edits"); the launch estimate's lines add up to its total; on Replies, "Suggest a reply" shows the working indicator and threads the subject; "Come back later" moves the reply to Scheduled; a scheduled date moves; the account page's Emails tab shows both directions. Measure `document.documentElement.scrollWidth` against `clientWidth` on each page: no horizontal overflow.

---

## What phase 12 depends on

The campaign page is where reporting lands (funnel, per-step reply rate, per-SDR response time); the dashboard and account pages are where Today and mailbox health go. Phase 12 adds a Reports tab beside People, Review, Launch and Steps rather than a new page, and reads names the same way these screens do: with the rows, never per row.
