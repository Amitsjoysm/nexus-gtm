# Phase 15: Cutover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The engagement engine becomes the only campaign engine. In-flight sequences move to it at the same step and due time, waiting drafts move to review, finished campaigns stay as read-only history, every consumer of the old engine is rewired, and the old code paths and pages are removed.

**Architecture:** `nexus/engagement/cutover/migrate.py` moves one workspace inside a SAVEPOINT; the dry run is the same run rolled back. `scripts/migrate_engagement.py` runs it per workspace, each in its own RLS-bound session. `sequences.service.attach_mailbox` resumes a campaign that had no mailbox, used by both the script (re-runs) and a new route behind the campaign page's chooser. `GET /engagement/legacy-campaigns` lists finished old campaigns. The old `nexus/campaigns/`, `nexus/cadences/`, their routers, worker jobs, runtime settings, pages, client methods and types are removed; tiering, the orchestrator's `setup_cadence`, contact sourcing and discovery's "Add to cadence" are rewired. `engagement_campaigns_enabled` becomes on by default, and the screens fail open.

**Tech Stack:** FastAPI, async SQLAlchemy, React + TypeScript, CSS Modules.

**Roadmap:** [00-roadmap.md](00-roadmap.md). **Spec:** §13 (cutover and dependency map), §15 (rollout), D13 (old tables kept, old code removed), D14 (in-flight moved at the same step; no mailbox pauses with a banner). **Depends on:** phases 01–14. **Runbook:** `docs/engagement/cutover-runbook.md`.

**Verified:** implemented on `feat/sdr-engagement` on top of phases 01–14. In the CI image: `tests/test_engagement_cutover.py` (7 passed: calendar waits become business days; the dry run reports templates, sequences moved, one paused for a mailbox, a draft to review, one old email found and one not, a call task linked and a finished campaign kept, and writes nothing; the real run moves a sequence at step 2 with its due time, both old emails and the recovered thread, links the call task, pauses the mailbox-less campaign, carries the draft into review, leaves the finished campaign, and a second run moves nothing; a mailbox connected later is attached by a re-run and resumes its person; the campaign page's route attaches the owner's mailbox and refuses one that is not; finished campaigns are listed read-only; nothing under `nexus/` imports the removed engine), the rewritten tiering, orchestrator, heartbeat, runtime-config and engine-default tests, the new structural checks, and the full suite: 3,698 passed and 3 failed on the first run, all three from this phase. Two heartbeat tests in `test_crm_auto_sync.py` still expected `advance_cadences` and relied on the engine's old default, and the scroll-container guard caught the `Tabs` strip scrolling without being a containing block. All three were fixed and those suites re-run green (62 passed). `tsc --noEmit`, `npm run build`. In the isolated preview, with old-engine rows seeded: the dry run printed one template, one sequence moving and pausing for its mailbox, and one finished campaign; the real run matched it and a second run moved nothing; `/campaigns` forwarded to the new Campaigns, where the moved campaign showed paused with its reason and **Earlier campaigns** listed the finished one; the campaign page offered "Send from this mailbox", which set it running with Lena Park active at step 1 as the old engine had her; a discovery-style selection handed to the builder showed who would be added, and creating the campaign added the three people at Acme to review. Not run: the live provider suite and release verification against real mailboxes (§14), which need the OAuth apps and test mailboxes the owner is setting up.

---

## Decisions this phase makes

- **The dry run is the real run, rolled back.** Both go through `_apply` inside a SAVEPOINT, and the dry run rolls it back, so the report the owner reads cannot drift from what the real run does. The Sent-folder searches still happen in a dry run: they only read, and "which old emails can we find" is part of what the owner reviews.
- **Idempotent on the legacy ids 0057 added** (`legacy_cadence_id`, `legacy_campaign_id`, `legacy_enrollment_id`) and on idempotency keys for moved emails and drafts. A re-run after an SDR connects a mailbox attaches it, resumes that campaign's people and imports the history that had nowhere to live the first time. Each workspace commits separately, so one failure leaves the rest moved and says which.
- **The old tables are history and are not written.** Nothing advances an old enrollment after this release, because the code that did is gone.
- **No ledger events from the migration.** Statuses are assigned directly rather than through `set_status`: cutover-time "enrollment.started" events would describe the migration, not any SDR's work, and the training data would learn from them.
- **Stated conversions.** Calendar-day waits become business days as `round(days × 5/7)`, at least 1, at most 60; an in-flight person keeps their exact due time, so this only shapes steps not yet reached. A campaign's owner is whoever created it, else the workspace owner; its mailbox is that person's connected mailbox, else it is paused `mailbox_disconnected` (D14). Moved active campaigns skip the launch credit gate because they were already running; the per-send meter still refuses an unaffordable send.
- **What moves and what stays.** Active and paused cadence enrollments move. Drafted or approved targets of a campaign still running (`awaiting_approval`, `approved`, `sending`, `drafting`, `draft_pending`) become drafts in the review queue. Completed, cancelled and failed campaigns stay in the old tables and are listed read-only as "Earlier campaigns". A draft whose campaign has no mailbox yet is reported and carried on a later run, because a message row needs a mailbox.
- **Choosing a mailbox resumes whoever was waiting for one.** `attach_mailbox` accepts only a connected mailbox of the campaign's owner (a campaign sends from its owner's own mailbox), points every live enrollment at it, and resumes those paused `mailbox_disconnected`, including people the advance loop paused when a mailbox disconnected, whom nothing resumed before. Only the owner is offered the choice.
- **On by default, and the screens fail open.** With the old pages gone, `engagement_campaigns_enabled` defaults to on and off is the platform's emergency stop. The nav shows the engagement items unless the engine is confirmed off; an unreadable status reads as on, like entitlements, because the server's 404 is the boundary and hiding the only campaign screens on a blip would take away an SDR's daily driver. `RequireEngine` says "switched off" in place rather than redirecting; `/campaigns` and `/cadences` forward for old bookmarks.
- **Rewired, not dropped.** Tiering's "someone is being worked here" reads live engagement enrollments (active, awaiting review, snoozed: a buyer who asked to hear back in June is exactly the account whose funding round must not be missed). The orchestrator goal `setup_cadence` keeps its stored name and creates a sequence template. Contact sourcing moves to `nexus/contacts/sourcing.py`, because the Accounts page's Find contacts uses it. Discovery's "Add to cadence" becomes "Add to a campaign": the selection travels to the builder in router state (not the URL) and is added the moment the campaign exists; `POST .../contacts` accepts `account_ids`, meaning everyone there with an email address.
- **Removed settings leave the catalog.** `cadence_enabled`, `cadence_batch_size`, `cadence_max_duration_days`, `campaign_sourcing_enabled` and `campaign_sourced_min_send_confidence` were read only by the old engine; the reader-coverage guard would flag them. Stored override rows for them are skipped by the catalog rule. `Settings` ignores unknown environment variables, so old env files still boot.
- **Leftover jobs are harmless.** A queued `run_campaign` or `advance_cadences` job from the previous release is logged as unknown and dropped, not retried.

---

## Files

| Action | Path | Responsibility |
|---|---|---|
| Create | `nexus/engagement/cutover/__init__.py`, `migrate.py`; `scripts/migrate_engagement.py` | the migration and its command |
| Modify | `nexus/engagement/sequences/service.py` | `attach_mailbox` |
| Modify | `nexus/api/routers/engagement_campaigns.py` | `PUT .../mailbox`, `GET /legacy-campaigns`, `account_ids` on add-people |
| Modify | `nexus/ingestion/tiering.py`, `nexus/orchestration/tools.py`, `planner.py` | rewired consumers |
| Move | `nexus/campaigns/sourcing.py` → `nexus/contacts/sourcing.py`; modify `routers/accounts.py`, `routers/agents.py` | contact sourcing |
| Modify | `nexus/workers/tasks.py`, `scheduler.py`, `nexus/api/routers/__init__.py` | old jobs and routers out |
| Modify | `nexus/runtime_config/catalog.py`, `nexus/core/config.py` | old settings out; engine on by default |
| Delete | `nexus/campaigns/`, `nexus/cadences/`, `nexus/api/routers/campaigns.py`, `cadences.py` | the old engine |
| Modify | `frontend/src/app/EngagementContext.tsx`, `app/nav.tsx`, `App.tsx`, `components/layout/Sidebar.tsx` | fail-open gating, nav, forwarding routes |
| Delete | `frontend/src/pages/CampaignsPage.tsx`, `CampaignsPage.module.css`, `CadencesPage.tsx`, `CadencesPage.module.css` | the old pages |
| Modify | `frontend/src/lib/api.ts`, `types.ts`, `display.ts` | old client and types out; new client calls |
| Modify | `components/discovery/ResultsPanel.tsx` (+ CSS), `pages/engagement/CampaignBuilder.tsx`, `CampaignDetailPage.tsx`, `CampaignsPage.tsx`, `pages/ListsPage.tsx`, `CallsPage.tsx`, `SettingsPage.tsx` | the rewired screens |
| Create | `docs/engagement/cutover-runbook.md`, `docs/engagement/oauth-environments.md`; modify `setup-google.md`, `setup-microsoft.md` | operator docs |
| Create/Modify/Delete | tests, listed in Task 7 | tests |

---

### Task 1: The migration

**Files:** Create `nexus/engagement/cutover/__init__.py`, `nexus/engagement/cutover/migrate.py`, `scripts/migrate_engagement.py`; test `tests/test_engagement_cutover.py`.

- [ ] **Step 1: Write the failing tests** — create `tests/test_engagement_cutover.py` from Task 7. Run `pytest tests/test_engagement_cutover.py -n0 -q` — expected FAIL: `No module named 'nexus.engagement.cutover'`.

- [ ] **Step 2: Implement** `nexus/engagement/cutover/__init__.py`:

```python
"""The one-off move from the old Campaigns and Cadences engines to the engagement engine (spec §13)."""
```

and `nexus/engagement/cutover/migrate.py`:

```python
"""Move one workspace from the old engines to the engagement engine (spec §13, D13, D14).

| Old | New |
|---|---|
| `Cadence` + `CadenceStep` | `sequence_templates` |
| Active or paused `CadenceEnrollment` | `engagement_enrollments` at the same step and due time |
| Its sent touches | `engagement_messages` (`out`, `sent`), thread recovered from the Sent folder |
| Drafted or approved targets of a campaign still running | the review queue |
| Completed, cancelled or failed campaigns | left where they are, listed read-only as Legacy |
| `CallTask.cadence_enrollment_id` | `engagement_enrollment_id` |
| `Outcome` rows | unchanged |

**The dry run is the real run, rolled back.** Both go through `_apply` inside a SAVEPOINT, and the
dry run rolls it back, so the report it prints cannot drift from what the real run would do. The
Sent-folder searches still happen in a dry run: they only read, and "which old emails can we find"
is exactly what the owner reviews before saying yes.

**Idempotent, keyed on the legacy ids** the 0057 tables carry: a template is found by
`legacy_cadence_id`, a campaign by `legacy_campaign_id`, an enrollment by `legacy_enrollment_id`,
and a moved email by its idempotency key. Running it twice moves nothing twice, and running it again
after an SDR connects a mailbox imports the history that had nowhere to live the first time.

**The old tables are history and are not written.** An old enrollment stays as it was; after the
cutover nothing advances it, because the code that did is gone.

**No ledger events.** Statuses are assigned directly rather than through `set_status`: a thousand
"enrollment.started" events stamped at cutover time would describe the migration, not any SDR's
work, and the training data would learn from them.

Two conversions, both stated rather than guessed at run time:

* Old delays are calendar days; new ones are business days. `round(days * 5 / 7)`, at least 1 for
  any non-zero wait, at most 60. An in-flight enrollment keeps its exact next due time, so this only
  shapes steps not yet reached.
* A campaign's owner is whoever created it, else the workspace's owner. Its mailbox is that
  person's connected mailbox; with none, the campaign is paused as `mailbox_disconnected` and says
  so, and nothing sends until a mailbox is chosen (D14).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime

LIVE_OLD = ("active", "paused")
OPEN_OLD_CAMPAIGN = ("awaiting_approval", "approved", "sending", "drafting", "draft_pending")
REVIEWABLE_TARGET = ("drafted", "approved")


def business_days(calendar_days: int) -> int:
    """A calendar-day wait as business days. Pure."""
    if not calendar_days or calendar_days <= 0:
        return 0
    return min(60, max(1, round(calendar_days * 5 / 7)))


def step_specs(old_steps: list) -> list[dict]:
    """Old cadence steps as new step specs, in order. Pure; `validate_steps` has the last word."""
    ordered = sorted(old_steps, key=lambda s: s.step_index)
    return [{"channel": s.channel if s.channel in ("email", "call") else "email",
             "angle": (s.angle or "")[:500],
             "delay_business_days": 0 if i == 0 else business_days(s.delay_days)}
            for i, s in enumerate(ordered)]


@dataclass(slots=True)
class CampaignReport:
    legacy_campaign_id: str
    name: str
    status: str = ""                  # the new campaign's status
    enrollments_moved: int = 0
    paused_for_mailbox: int = 0
    drafts_to_review: int = 0
    emails_found: int = 0             # old emails found in the Sent folder, so replies thread
    emails_not_found: int = 0         # the next follow-up starts a new thread
    skipped: list[dict] = field(default_factory=list)


@dataclass(slots=True)
class Report:
    tenant_id: str
    dry_run: bool
    templates_created: int = 0
    templates_skipped: list[dict] = field(default_factory=list)
    campaigns: list[CampaignReport] = field(default_factory=list)
    call_tasks_linked: int = 0
    legacy_campaigns: int = 0         # completed/cancelled/failed, left read-only

    def as_dict(self) -> dict:
        return asdict(self)

    @property
    def totals(self) -> dict:
        c = self.campaigns
        return {"sequences_moved": sum(x.enrollments_moved for x in c),
                "paused_for_mailbox": sum(x.paused_for_mailbox for x in c),
                "drafts_to_review": sum(x.drafts_to_review for x in c),
                "emails_found": sum(x.emails_found for x in c),
                "emails_not_found": sum(x.emails_not_found for x in c)}


async def migrate(ts, *, dry_run: bool, now: datetime | None = None) -> Report:
    """Migrate this workspace, or report what migrating it would do."""
    from nexus.core.db import utcnow

    report = Report(tenant_id=ts.tenant_id, dry_run=dry_run)
    savepoint = await ts.session.begin_nested()
    try:
        await _apply(ts, report, now=now or utcnow())
    except Exception:
        await savepoint.rollback()
        raise
    if dry_run:
        await savepoint.rollback()
    else:
        await savepoint.commit()
    return report


async def _owner(ts, created_by: str | None) -> str | None:
    from nexus.models.identity import Membership

    if created_by:
        return created_by
    owners = await ts.list(Membership, Membership.role == "owner")
    return owners[0].user_id if owners else None


async def _mailbox_of(ts, user_id: str | None):
    from nexus.models.engagement import MailboxConnection

    if not user_id:
        return None
    rows = await ts.list(MailboxConnection, MailboxConnection.owner_user_id == user_id,
                         MailboxConnection.status == "connected")
    return min(rows, key=lambda m: m.created_at) if rows else None


async def _templates(ts, report: Report) -> dict[str, list[dict]]:
    """Every old cadence as a sequence template. Returns the validated specs by cadence id."""
    from nexus.engagement.sequences.service import CampaignError, validate_steps
    from nexus.models.cadence import Cadence, CadenceStep
    from nexus.models.engagement import SequenceTemplate

    specs_by_cadence: dict[str, list[dict]] = {}
    for cadence in await ts.list(Cadence):
        steps = await ts.list(CadenceStep, CadenceStep.cadence_id == cadence.id)
        try:
            specs = validate_steps(step_specs(steps))
        except CampaignError as exc:
            report.templates_skipped.append({"cadence_id": cadence.id, "name": cadence.name,
                                             "reason": str(exc)})
            continue
        specs_by_cadence[cadence.id] = specs
        if await ts.first(SequenceTemplate, SequenceTemplate.legacy_cadence_id == cadence.id):
            continue
        ts.add(SequenceTemplate(name=cadence.name[:200], description=cadence.description or "",
                                steps=specs, created_by_user_id=cadence.created_by_user_id,
                                legacy_cadence_id=cadence.id,
                                archived_at=None if cadence.is_active else cadence.updated_at))
        report.templates_created += 1
    await ts.flush()
    return specs_by_cadence


async def _campaign_for(ts, old, specs: list[dict]):
    """The engagement campaign standing for ``old``, created on first sight."""
    from nexus.engagement.sequences.service import _write_steps
    from nexus.models.engagement import EngagementCampaign

    existing = await ts.first(EngagementCampaign, EngagementCampaign.legacy_campaign_id == old.id)
    if existing is not None:
        if existing.mailbox_connection_id is None:
            # Moved before its owner had a mailbox: attach the one they have connected since.
            from nexus.engagement.sequences.service import attach_mailbox

            mailbox = await _mailbox_of(ts, existing.owner_user_id)
            if mailbox is not None:
                await attach_mailbox(ts, existing, mailbox)
        return existing
    owner = await _owner(ts, old.created_by_user_id)
    if owner is None:
        return None
    mailbox = await _mailbox_of(ts, owner)
    campaign = EngagementCampaign(
        name=old.name[:200], owner_user_id=owner,
        mailbox_connection_id=getattr(mailbox, "id", None), status="reviewing",
        review_every_touch=bool(old.review_each_touch), source_list_id=old.list_id,
        legacy_campaign_id=old.id)
    ts.add(campaign)
    await ts.flush()
    await _write_steps(ts, campaign, specs)
    return campaign


async def _history(ts, report: CampaignReport, *, campaign, enrollment, contact, account,
                   mailbox, touches: list, now: datetime) -> None:
    """The emails the old engine sent this person, recorded so replies find their thread."""
    from nexus.engagement.mailboxes.registry import open_provider
    from nexus.engagement.subjects import normalize_subject
    from nexus.models.engagement import EngagementMessage, EngagementThread

    provider = None
    try:
        provider = await open_provider(ts, mailbox)
    except Exception:  # noqa: BLE001 - an unreachable mailbox finds nothing, it does not stop the move
        provider = None
    latest_thread = None
    for touch in sorted(touches, key=lambda t: t.step_index):
        key = f"legacy-touch:{touch.id}"
        if await ts.first(EngagementMessage, EngagementMessage.idempotency_key == key):
            continue
        draft = touch.draft or {}
        subject = (draft.get("subject") or "").strip()
        body = (draft.get("body") or "").strip()
        sent_at = touch.sent_at or now
        found = None
        if provider is not None and subject:
            try:
                found = await provider.search_sent(to=contact.email, subject=subject,
                                                   around=sent_at)
            except Exception:  # noqa: BLE001 - a failed search is "not found", never a stop
                found = None
        thread = None
        if found is not None:
            report.emails_found += 1
            thread = await ts.first(
                EngagementThread, EngagementThread.mailbox_connection_id == mailbox.id,
                EngagementThread.provider_thread_id == found.provider_thread_id)
            if thread is None:
                thread = EngagementThread(
                    mailbox_connection_id=mailbox.id, provider_thread_id=found.provider_thread_id,
                    contact_id=contact.id, account_id=account.id, enrollment_id=enrollment.id,
                    base_subject=normalize_subject(subject), last_message_at=sent_at)
                ts.add(thread)
                await ts.flush()
            latest_thread = thread
        else:
            report.emails_not_found += 1
        ts.add(EngagementMessage(
            mailbox_connection_id=mailbox.id, thread_id=getattr(thread, "id", None),
            enrollment_id=enrollment.id, contact_id=contact.id, direction="out", kind="step",
            status="sent", step_index=touch.step_index, idempotency_key=key,
            provider_message_id=getattr(found, "provider_message_id", None),
            rfc_message_id=(getattr(found, "rfc_message_id", "") or None),
            from_addr=mailbox.email, to_addrs=[contact.email], subject=subject, body_text=body,
            sent_at=sent_at))
        await ts.flush()
    if latest_thread is not None:
        enrollment.current_thread_id = latest_thread.id


async def _enrollments(ts, report: Report, specs_by_cadence: dict, *, now: datetime) -> dict:
    """In-flight cadence enrollments, moved at the same step and due time. Returns old → new id."""
    from nexus.engagement.timekeeping import resolve_zone
    from nexus.models.account import Account, Contact
    from nexus.models.cadence import CadenceEnrollment, CadenceTouch
    from nexus.models.campaign import Campaign
    from nexus.models.engagement import EngagementEnrollment, MailboxConnection

    moved: dict[str, str] = {}
    by_campaign: dict[str, CampaignReport] = {c.legacy_campaign_id: c for c in report.campaigns}
    for old in await ts.list(CadenceEnrollment, CadenceEnrollment.status.in_(LIVE_OLD)):
        old_campaign = await ts.get(Campaign, old.campaign_id)
        entry = by_campaign.get(old.campaign_id)
        if entry is None:
            entry = CampaignReport(legacy_campaign_id=old.campaign_id,
                                   name=getattr(old_campaign, "name", ""))
            by_campaign[old.campaign_id] = entry
            report.campaigns.append(entry)
        specs = specs_by_cadence.get(old.cadence_id)
        contact = await ts.get(Contact, old.contact_id) if old.contact_id else None
        why = ("its cadence could not become a template" if specs is None
               else "no contact on the enrollment" if contact is None
               else "the contact has no email address" if not (contact.email or "").strip()
               else "")
        if why or old_campaign is None:
            entry.skipped.append({"legacy_enrollment_id": old.id, "reason": why or "no campaign"})
            continue
        campaign = await _campaign_for(ts, old_campaign, specs)
        if campaign is None:
            entry.skipped.append({"legacy_enrollment_id": old.id,
                                  "reason": "nobody to own the campaign"})
            continue
        mailbox = await ts.get(MailboxConnection, campaign.mailbox_connection_id) \
            if campaign.mailbox_connection_id else None
        account = await ts.get(Account, old.account_id)
        new = await ts.first(EngagementEnrollment,
                             EngagementEnrollment.legacy_enrollment_id == old.id)
        if new is None:
            if await ts.first(EngagementEnrollment,
                              EngagementEnrollment.campaign_id == campaign.id,
                              EngagementEnrollment.contact_id == contact.id):
                entry.skipped.append({"legacy_enrollment_id": old.id,
                                      "reason": "the person is already in this campaign"})
                continue
            zone = resolve_zone(contact_tz=(contact.custom_fields or {}).get("timezone"),
                                account_country=getattr(account, "country", None),
                                account_region=getattr(account, "region", None),
                                sdr_tz=getattr(mailbox, "timezone", None))
            if mailbox is None:
                status, reason = "paused", "mailbox_disconnected"
                entry.paused_for_mailbox += 1
            elif old.status == "paused":
                status, reason = "paused", "manual"
            else:
                status, reason = "active", None
            new = EngagementEnrollment(
                campaign_id=campaign.id, contact_id=contact.id, account_id=old.account_id,
                mailbox_connection_id=campaign.mailbox_connection_id, status=status,
                status_reason=reason, current_step_index=old.current_step_index,
                next_action_at=old.next_touch_at, started_at=old.started_at,
                contact_timezone=zone.name, legacy_enrollment_id=old.id)
            ts.add(new)
            await ts.flush()
            entry.enrollments_moved += 1
        moved[old.id] = new.id
        if mailbox is not None:
            touches = await ts.list(CadenceTouch, CadenceTouch.enrollment_id == old.id,
                                    CadenceTouch.status == "sent")
            await _history(ts, entry, campaign=campaign, enrollment=new, contact=contact,
                           account=account, mailbox=mailbox, touches=touches, now=now)
        _settle(campaign, has_live=True, now=now)
    return moved


def _settle(campaign, *, has_live: bool, now: datetime) -> None:
    """A campaign with people mid-sequence runs; without a mailbox it waits, and says why."""
    if campaign.mailbox_connection_id is None:
        campaign.status, campaign.pause_reason = "paused", "mailbox_disconnected"
    elif has_live:
        campaign.status, campaign.pause_reason = "active", None
        campaign.launched_at = campaign.launched_at or now


async def _review_drafts(ts, report: Report, specs_by_cadence: dict, *, now: datetime) -> None:
    """Drafted or approved targets of a campaign still running become drafts in the review queue."""
    from nexus.engagement.sequences.service import validate_steps
    from nexus.engagement.timekeeping import resolve_zone
    from nexus.models.account import Account, Contact
    from nexus.models.campaign import Campaign, CampaignTarget
    from nexus.models.engagement import EngagementEnrollment, EngagementMessage, MailboxConnection

    by_campaign = {c.legacy_campaign_id: c for c in report.campaigns}
    for old in await ts.list(Campaign, Campaign.status.in_(OPEN_OLD_CAMPAIGN)):
        targets = await ts.list(CampaignTarget, CampaignTarget.campaign_id == old.id,
                                CampaignTarget.status.in_(REVIEWABLE_TARGET))
        if not targets:
            continue
        entry = by_campaign.get(old.id)
        if entry is None:
            entry = CampaignReport(legacy_campaign_id=old.id, name=old.name)
            by_campaign[old.id] = entry
            report.campaigns.append(entry)
        specs = specs_by_cadence.get(old.cadence_id) if old.cadence_id else \
            [{"channel": "email", "angle": "", "delay_business_days": 0}]
        if specs is None:
            entry.skipped.append({"legacy_campaign_id": old.id,
                                  "reason": "its cadence could not become a template"})
            continue
        campaign = await _campaign_for(ts, old, validate_steps(specs))
        if campaign is None:
            entry.skipped.append({"legacy_campaign_id": old.id,
                                  "reason": "nobody to own the campaign"})
            continue
        mailbox = await ts.get(MailboxConnection, campaign.mailbox_connection_id) \
            if campaign.mailbox_connection_id else None
        for target in targets:
            draft = target.draft or {}
            key = f"legacy-target:{target.id}"
            if await ts.first(EngagementMessage, EngagementMessage.idempotency_key == key):
                continue
            contact = await ts.get(Contact, draft.get("contact_id")) \
                if draft.get("contact_id") else None
            if contact is None or not (contact.email or "").strip():
                entry.skipped.append({"legacy_target_id": target.id,
                                      "reason": "the draft has no contact with an email"})
                continue
            if mailbox is None:
                # A draft needs a mailbox row to live on; it is carried on a later run.
                entry.skipped.append({"legacy_target_id": target.id,
                                      "reason": "no mailbox yet; run again after connecting one"})
                continue
            if await ts.first(EngagementEnrollment,
                              EngagementEnrollment.campaign_id == campaign.id,
                              EngagementEnrollment.contact_id == contact.id):
                entry.skipped.append({"legacy_target_id": target.id,
                                      "reason": "the person is already in this campaign"})
                continue
            account = await ts.get(Account, target.account_id)
            zone = resolve_zone(contact_tz=(contact.custom_fields or {}).get("timezone"),
                                account_country=getattr(account, "country", None),
                                account_region=getattr(account, "region", None),
                                sdr_tz=mailbox.timezone)
            enrollment = EngagementEnrollment(
                campaign_id=campaign.id, contact_id=contact.id, account_id=target.account_id,
                mailbox_connection_id=mailbox.id, status="awaiting_review",
                contact_timezone=zone.name)
            ts.add(enrollment)
            await ts.flush()
            subject = (draft.get("subject") or "").strip()
            body = (draft.get("body") or "").strip()
            ts.add(EngagementMessage(
                mailbox_connection_id=mailbox.id, enrollment_id=enrollment.id,
                contact_id=contact.id, direction="out", kind="step", status="draft",
                step_index=0, idempotency_key=key, from_addr=mailbox.email,
                to_addrs=[contact.email], subject=subject, body_text=body, ai_subject=subject,
                ai_body=body, quality_problems=[]))
            await ts.flush()
            entry.drafts_to_review += 1
        if campaign.status not in ("active", "paused"):
            campaign.status = "reviewing"


async def _call_tasks(ts, report: Report, moved: dict[str, str]) -> None:
    from nexus.models.calling import CallTask

    if not moved:
        return
    for task in await ts.list(CallTask, CallTask.cadence_enrollment_id.in_(list(moved)),
                              CallTask.engagement_enrollment_id.is_(None)):
        task.engagement_enrollment_id = moved[task.cadence_enrollment_id]
        report.call_tasks_linked += 1


async def _apply(ts, report: Report, *, now: datetime) -> None:
    from nexus.models.campaign import Campaign
    from nexus.models.engagement import EngagementCampaign

    specs_by_cadence = await _templates(ts, report)
    moved = await _enrollments(ts, report, specs_by_cadence, now=now)
    await _review_drafts(ts, report, specs_by_cadence, now=now)
    await _call_tasks(ts, report, moved)
    report.legacy_campaigns = len(await ts.list(
        Campaign, Campaign.status.in_(("completed", "cancelled", "failed"))))
    for entry in report.campaigns:
        new = await ts.first(EngagementCampaign,
                             EngagementCampaign.legacy_campaign_id == entry.legacy_campaign_id)
        entry.status = getattr(new, "status", "not moved")
    await ts.flush()


def render(report: Report) -> str:
    """The report as the owner reads it before saying yes."""
    t = report.totals
    lines = [f"Workspace {report.tenant_id} ({'DRY RUN, nothing written' if report.dry_run else 'migrated'})",
             f"  Sequence templates created: {report.templates_created}",
             f"  Sequences moved: {t['sequences_moved']}"
             f" ({t['paused_for_mailbox']} paused until a mailbox is connected)",
             f"  Opening emails moving to review: {t['drafts_to_review']}",
             f"  Old emails found in Sent folders: {t['emails_found']}; not found: "
             f"{t['emails_not_found']} (their next follow-up starts a new thread)",
             f"  Call tasks linked: {report.call_tasks_linked}",
             f"  Finished campaigns left read-only as Legacy: {report.legacy_campaigns}"]
    for skipped in report.templates_skipped:
        lines.append(f"  ! Cadence '{skipped['name']}' not moved: {skipped['reason']}")
    for entry in report.campaigns:
        lines.append(f"  - {entry.name or entry.legacy_campaign_id}: {entry.status}, "
                     f"{entry.enrollments_moved} moved, {entry.drafts_to_review} to review")
        for skipped in entry.skipped:
            lines.append(f"      ! {skipped['reason']} ({next(iter(skipped.values()))})")
    return "\n".join(lines)
```

- [ ] **Step 3: The command** — `scripts/migrate_engagement.py`:

```python
# scripts/migrate_engagement.py
"""Move every workspace from the old Campaigns and Cadences to the engagement engine (spec §13).

Run the dry run first and read it with the owner. It writes nothing, and it reports:
sequences to move, sequences that will pause until their owner connects a mailbox, the old emails
found and not found in Sent folders, and campaigns whose drafts move to review.

    python scripts/migrate_engagement.py --dry-run
    python scripts/migrate_engagement.py --dry-run --tenant <tenant_id>
    python scripts/migrate_engagement.py                 # for real, every workspace
    python scripts/migrate_engagement.py --json          # machine-readable report

Idempotent: run it again at any time, including after SDRs connect mailboxes, which imports the
history that had nowhere to live the first time. Each workspace runs in its own RLS-bound session
and its own transaction, so one workspace failing leaves the others migrated and says which.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from sqlalchemy import select, union


async def _tenants(only: str | None) -> list[str]:
    """Workspaces with anything to move: a cadence or a campaign."""
    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.cadence import Cadence
    from nexus.models.campaign import Campaign

    if only:
        return [only]
    async with get_platform_sessionmaker()() as session:
        rows = await session.execute(union(select(Cadence.tenant_id), select(Campaign.tenant_id)))
        return sorted({tenant_id for (tenant_id,) in rows})


async def main(*, dry_run: bool, tenant: str | None, as_json: bool) -> int:
    from nexus.engagement.cutover.migrate import migrate, render
    from nexus.workers.tasks import tenant_session

    failures = 0
    reports = []
    for tenant_id in await _tenants(tenant):
        try:
            async with tenant_session(tenant_id) as ts:
                report = await migrate(ts, dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001 - report the workspace, carry on with the rest
            failures += 1
            print(f"Workspace {tenant_id}: FAILED, nothing written ({type(exc).__name__}: {exc})",
                  file=sys.stderr)
            continue
        reports.append(report)
        if not as_json:
            print(render(report))
            print()
    if as_json:
        print(json.dumps([{**r.as_dict(), "totals": r.totals} for r in reports], indent=2,
                         default=str))
    return 1 if failures else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="report only; write nothing")
    parser.add_argument("--tenant", help="one workspace id")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(dry_run=args.dry_run, tenant=args.tenant, as_json=args.json)))
```

- [ ] **Step 4: Run** `pytest tests/test_engagement_cutover.py -n0 -q -k "business or dry_run or real_run"` — expected PASS.

---

### Task 2: Attaching a mailbox, and the Legacy list

**Files:** Modify `nexus/engagement/sequences/service.py`, `nexus/api/routers/engagement_campaigns.py`.

- [ ] **Step 1: `attach_mailbox`**, the no-steps refusal kept from phase 14 — apply to `nexus/engagement/sequences/service.py`:

```diff
diff --git a/nexus/engagement/sequences/service.py b/nexus/engagement/sequences/service.py
index b81c4c6..f2fd92f 100644
--- a/nexus/engagement/sequences/service.py
+++ b/nexus/engagement/sequences/service.py
@@ -403,4 +403,36 @@ async def remove(ts, enrollment, *, user_id: str | None = None) -> None:
 
 
+# ---- the sending mailbox ------------------------------------------------------------------------
+
+async def attach_mailbox(ts, campaign, mailbox) -> int:
+    """Send this campaign from ``mailbox``, and resume whoever was waiting for one.
+
+    A campaign moved from the old engine whose owner had no connected mailbox is paused as
+    `mailbox_disconnected` (D14), and so is anyone the advance loop found with a disconnected one.
+    Nothing brought them back: reconnecting a mailbox or choosing another left them paused for
+    good. Returns how many people resumed. Their due times are kept, so anyone overdue goes out on
+    the next tick, in order.
+    """
+    from nexus.models.engagement import EngagementEnrollment
+
+    if mailbox is None or mailbox.status != "connected":
+        raise CampaignError("Choose a connected mailbox.")
+    if mailbox.owner_user_id != campaign.owner_user_id:
+        raise CampaignError("A campaign sends from its owner's own mailbox.")
+    campaign.mailbox_connection_id = mailbox.id
+    resumed = 0
+    for enrollment in await ts.list(EngagementEnrollment,
+                                    EngagementEnrollment.campaign_id == campaign.id,
+                                    EngagementEnrollment.status.notin_(("stopped", "completed"))):
+        enrollment.mailbox_connection_id = mailbox.id
+        if enrollment.status == "paused" and enrollment.status_reason == "mailbox_disconnected":
+            enrollment.status, enrollment.status_reason = "active", None
+            resumed += 1
+    if campaign.status == "paused" and campaign.pause_reason == "mailbox_disconnected":
+        campaign.status, campaign.pause_reason = "active", None
+    await ts.flush()
+    return resumed
+
+
 # ---- launch, pause, overrides -------------------------------------------------------------------
 
```

- [ ] **Step 2: The routes** — `PUT /engagement/campaigns/{id}/mailbox`, `GET /engagement/legacy-campaigns`, and `account_ids` on `POST .../contacts`. Apply to `nexus/api/routers/engagement_campaigns.py`:

```diff
diff --git a/nexus/api/routers/engagement_campaigns.py b/nexus/api/routers/engagement_campaigns.py
index 58912f7..d4ca64a 100644
--- a/nexus/api/routers/engagement_campaigns.py
+++ b/nexus/api/routers/engagement_campaigns.py
@@ -1,7 +1,7 @@
 """Engagement campaigns: build, draft, review, launch, and steer (spec §9).
 
-Dark until the cutover: every route answers 404 while `engagement_campaigns_enabled` is off, so the
-new engine cannot be reached by URL before it is switched on — the same effect as the hidden nav
-item, enforced where it matters.
+Every route answers 404 while `engagement_campaigns_enabled` is off. Since the cutover (spec §13)
+that switch is on by default and off is the platform's emergency stop, enforced here rather than
+only in the navigation.
 
 An SDR works their own campaigns (`run_engagement`); a manager can act on anyone's
@@ -78,7 +78,11 @@ class CampaignOut(BaseModel):
 
 class ContactsIn(BaseModel):
+    """People to add, named directly or by company. A company means everyone there with an email
+    address, which is what "add these accounts to a campaign" from discovery or a list means."""
+
     model_config = {"extra": "forbid"}
 
-    contact_ids: list[str] = Field(min_length=1, max_length=2000)
+    contact_ids: list[str] = Field(default_factory=list, max_length=2000)
+    account_ids: list[str] = Field(default_factory=list, max_length=500)
 
 
@@ -263,4 +267,42 @@ async def list_campaigns(
 
 
+class LegacyCampaignOut(BaseModel):
+    id: str
+    name: str
+    status: str
+    created_at: datetime
+    targets: int
+    sent: int
+
+
+@router.get("/legacy-campaigns", response_model=list[LegacyCampaignOut])
+async def legacy_campaigns(
+    team: bool = False,
+    ts: TenantSession = Depends(get_tenant_session),
+    principal: Principal = Depends(require(Permission.run_engagement)),
+) -> list[LegacyCampaignOut]:
+    """Campaigns the old engine finished (spec §13): read-only history, never moved. What each
+    sent is here; the old tables are kept exactly as they were."""
+    from sqlalchemy import func, select
+
+    from nexus.models.campaign import Campaign, CampaignTarget
+
+    where = [Campaign.status.in_(("completed", "cancelled", "failed"))]
+    if not (team and _is_manager(principal)):
+        where.append(Campaign.created_by_user_id == principal.user_id)
+    rows = await ts.list(Campaign, *where)
+    counts = {} if not rows else {
+        (campaign_id, status_): n for campaign_id, status_, n in (await ts.session.execute(
+            select(CampaignTarget.campaign_id, CampaignTarget.status, func.count())
+            .where(CampaignTarget.tenant_id == ts.tenant_id)
+            .where(CampaignTarget.campaign_id.in_([c.id for c in rows]))
+            .group_by(CampaignTarget.campaign_id, CampaignTarget.status))).all()}
+    return [LegacyCampaignOut(
+        id=c.id, name=c.name, status=c.status, created_at=c.created_at,
+        targets=sum(n for (cid, _s), n in counts.items() if cid == c.id),
+        sent=counts.get((c.id, "sent"), 0))
+        for c in sorted(rows, key=lambda c: c.created_at, reverse=True)]
+
+
 @router.post("/campaigns", response_model=CampaignOut, status_code=201)
 async def create(
@@ -308,4 +350,29 @@ async def put_steps(
 
 
+class MailboxIn(BaseModel):
+    model_config = {"extra": "forbid"}
+
+    mailbox_id: str
+
+
+@router.put("/campaigns/{campaign_id}/mailbox", response_model=CampaignOut)
+async def put_mailbox(
+    campaign_id: str, body: MailboxIn,
+    ts: TenantSession = Depends(get_tenant_session),
+    principal: Principal = Depends(require(Permission.run_engagement)),
+) -> CampaignOut:
+    """Send from this mailbox, and resume whoever was waiting for one (a campaign moved from the old
+    engine before its owner connected a mailbox, or one whose mailbox was disconnected)."""
+    from nexus.engagement.sequences.service import CampaignError, attach_mailbox
+    from nexus.models.engagement import MailboxConnection
+
+    campaign = await _campaign(ts, campaign_id, principal)
+    try:
+        await attach_mailbox(ts, campaign, await ts.get(MailboxConnection, body.mailbox_id))
+    except CampaignError as exc:
+        raise _refuse(exc) from exc
+    return await _out(ts, campaign)
+
+
 @router.post("/campaigns/{campaign_id}/contacts")
 async def add_contacts(
@@ -316,7 +383,18 @@ async def add_contacts(
     from nexus.engagement.sequences.service import CampaignError, enroll
 
+    from nexus.models.account import Contact
+
     campaign = await _campaign(ts, campaign_id, principal)
+    contact_ids = list(body.contact_ids)
+    if body.account_ids:
+        contact_ids += [c.id for c in await ts.list(
+            Contact, Contact.account_id.in_(body.account_ids), Contact.deleted_at.is_(None),
+            Contact.email.is_not(None)) if (c.email or "").strip()]
+    if not contact_ids:
+        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
+                            "Nobody to add: choose people, or companies with contacts that have "
+                            "an email address.")
     try:
-        result = await enroll(ts, campaign, body.contact_ids)
+        result = await enroll(ts, campaign, contact_ids[:2000])
     except CampaignError as exc:
         raise _refuse(exc) from exc
```

- [ ] **Step 3: Run** `pytest tests/test_engagement_cutover.py -n0 -q` — expected PASS (6).

---

### Task 3: Rewire what used the old engine

- [ ] **Step 1: Tiering** reads live engagement enrollments — `nexus/ingestion/tiering.py`:

```diff
diff --git a/nexus/ingestion/tiering.py b/nexus/ingestion/tiering.py
index 107b61d..cddd392 100644
--- a/nexus/ingestion/tiering.py
+++ b/nexus/ingestion/tiering.py
@@ -44,11 +44,17 @@ async def _has_recent_signal(ts, account_id: str, since: datetime) -> bool:
 
 
-async def _in_active_cadence(ts, account_id: str) -> bool:
-    from nexus.models.cadence import CadenceEnrollment, ENROLL_ACTIVE
+#: Engagement enrollments a rep is working: someone is being emailed, or waits to be.
+WORKING = ("active", "awaiting_review", "snoozed")
+
+
+async def _in_active_sequence(ts, account_id: str) -> bool:
+    """Someone at this account is in a live engagement sequence. Snoozed counts: a buyer who asked
+    to hear back in June is exactly the account whose funding round the rep must not miss."""
+    from nexus.models.engagement import EngagementEnrollment
 
     return await ts.first(
-        CadenceEnrollment,
-        CadenceEnrollment.account_id == account_id,
-        CadenceEnrollment.status == ENROLL_ACTIVE,
+        EngagementEnrollment,
+        EngagementEnrollment.account_id == account_id,
+        EngagementEnrollment.status.in_(WORKING),
     ) is not None
 
@@ -80,5 +86,5 @@ async def classify(ts, account, *, new_signals: list | None = None) -> str:
         # A rep is actively working this account. Whatever the signal history says, an account
         # someone is emailing today must not go on a three-day crawl cycle.
-        if await _in_active_cadence(ts, account.id):
+        if await _in_active_sequence(ts, account.id):
             return HOT
         # Somebody deliberately put it on a list. That is an explicit statement of interest and is
```

- [ ] **Step 2: The orchestrator's `setup_cadence`** creates a sequence template — `nexus/orchestration/tools.py`:

```diff
diff --git a/nexus/orchestration/tools.py b/nexus/orchestration/tools.py
index 201a3e4..63e4022 100644
--- a/nexus/orchestration/tools.py
+++ b/nexus/orchestration/tools.py
@@ -330,42 +330,50 @@ _DEFAULT_CADENCE_STEPS = [
 
 class SetupCadenceTool(Tool):
-    """Let the orchestrator define a multi-touch cadence (email + call steps) on its own.
+    """Let the orchestrator define a multi-touch sequence (email + call steps) on its own.
 
-    Reads ``cadence_name`` / ``steps`` / ``description`` from the run's goal_input; when steps
-    aren't specified it picks a sensible cold-calling 3-touch (email -> call -> email). Creating a
-    cadence definition is not outbound, so no approval gate — actual sends/calls still flow through
-    the campaign launch + approval gate."""
+    Creates a **sequence template** (the engagement engine's replacement for cadences, spec §13).
+    The goal and tool keep the name ``setup_cadence`` because it is stored on existing runs. Reads
+    ``cadence_name`` / ``steps`` / ``description`` from the run's goal_input; when steps aren't
+    specified it picks a sensible cold-calling 3-touch (email -> call -> email). Old-style
+    ``delay_days`` are calendar days and are converted like the cutover converts them. Defining a
+    template is not outbound, so no approval gate: every send still goes through a campaign's
+    review queue."""
 
     name = "setup_cadence"
-    description = "Create a multi-touch cadence (email and/or call steps) from a brief."
+    description = "Create a multi-touch sequence template (email and/or call steps) from a brief."
     requires_approval = False
 
     async def run(self, tc: ToolContext) -> dict:
-        from nexus.cadences.service import CadenceError, get_cadence_service
+        from nexus.engagement.cutover.migrate import business_days
+        from nexus.engagement.sequences.service import CampaignError, validate_steps
+        from nexus.models.engagement import SequenceTemplate
 
         gi = {**(tc.run.goal_input or {}), **(tc.inputs or {})}
-        steps = gi.get("steps") or _DEFAULT_CADENCE_STEPS
-        name = gi.get("cadence_name") or gi.get("name") or "AI cadence"
+        raw = gi.get("steps") or _DEFAULT_CADENCE_STEPS
+        name = (gi.get("cadence_name") or gi.get("name") or "AI sequence")[:200]
+        specs = []
+        for i, step in enumerate(raw):
+            wait = step.get("delay_business_days")
+            if wait is None:
+                wait = business_days(int(step.get("delay_days", 0) or 0))
+            specs.append({"channel": step.get("channel", "email"),
+                          "angle": step.get("angle", ""),
+                          "delay_business_days": 0 if i == 0 else int(wait)})
         try:
-            cadence = await get_cadence_service().create_cadence(
-                tc.ts,
-                name=name,
-                description=gi.get("description"),
-                steps=steps,
-                created_by_user_id=getattr(tc.run, "created_by_user_id", None),
-            )
-        except CadenceError as exc:
+            steps = validate_steps(specs)
+        except CampaignError as exc:
             raise ToolError(str(exc))
+        template = SequenceTemplate(name=name, description=gi.get("description") or "",
+                                    steps=steps,
+                                    created_by_user_id=getattr(tc.run, "created_by_user_id", None))
+        tc.ts.add(template)
+        await tc.ts.flush()
         out = {
-            "cadence_id": cadence.id,
-            "name": cadence.name,
-            "steps": [
-                {"channel": s.get("channel", "email"),
-                 "delay_days": int(s.get("delay_days", 0)),
-                 "angle": s.get("angle", "")}
-                for s in steps
-            ],
+            "template_id": template.id,
+            "name": template.name,
+            "steps": [{"channel": s["channel"], "delay_business_days": s["delay_business_days"],
+                       "angle": s["angle"]} for s in steps],
         }
-        tc.blackboard["cadence"] = out
+        tc.blackboard["sequence_template"] = out
         return out
 
```

and its recipe's docstring in `nexus/orchestration/planner.py`:

```diff
diff --git a/nexus/orchestration/planner.py b/nexus/orchestration/planner.py
index 6175944..f0f004c 100644
--- a/nexus/orchestration/planner.py
+++ b/nexus/orchestration/planner.py
@@ -112,5 +112,5 @@ def _discover_plan(goal_input: dict) -> list[PlanStep]:
 
 def _setup_cadence_plan(goal_input: dict) -> list[PlanStep]:
-    """Define a multi-touch cadence (email/call). cadence_name/steps/description ride on
+    """Define a multi-touch sequence template (email/call). cadence_name/steps/description ride on
     run.goal_input, which SetupCadenceTool reads. Creating a definition is not outbound — no gate."""
     return [PlanStep(idx=0, tool="setup_cadence", depends_on=[], requires_approval=False)]
```

- [ ] **Step 3: Contact sourcing moves** out of the old package:

```bash
git mv nexus/campaigns/sourcing.py nexus/contacts/sourcing.py
```

Then point its two callers at the new path — `nexus/api/routers/accounts.py`:

```diff
diff --git a/nexus/api/routers/accounts.py b/nexus/api/routers/accounts.py
index 5586e2f..6d39edb 100644
--- a/nexus/api/routers/accounts.py
+++ b/nexus/api/routers/accounts.py
@@ -714,5 +714,5 @@ async def source_contacts(
     if account is None:
         raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
-    from nexus.campaigns.sourcing import source_account_contacts
+    from nexus.contacts.sourcing import source_account_contacts
 
     created = await source_account_contacts(ts, account, limit=max(1, min(limit, 25)))
```

`nexus/api/routers/agents.py`:

```diff
diff --git a/nexus/api/routers/agents.py b/nexus/api/routers/agents.py
index c41fcc8..b930ac1 100644
--- a/nexus/api/routers/agents.py
+++ b/nexus/api/routers/agents.py
@@ -161,5 +161,5 @@ async def run_pipeline(
             result["new_contacts"] = 0
             return result
-        from nexus.campaigns.sourcing import source_account_contacts
+        from nexus.contacts.sourcing import source_account_contacts
         from nexus.core.config import get_settings
 
```

and update its docstring and logger name in `nexus/contacts/sourcing.py`:

```diff
diff --git a/nexus/campaigns/sourcing.py b/nexus/contacts/sourcing.py
similarity index 95%
rename from nexus/campaigns/sourcing.py
rename to nexus/contacts/sourcing.py
index 31bd925..993312b 100644
--- a/nexus/campaigns/sourcing.py
+++ b/nexus/contacts/sourcing.py
@@ -2,6 +2,7 @@
 
 Composes the registry (net-new contact search) and the waterfall enricher (verifying email
-finder). Owns no orchestration — the campaign draft phase calls it once when a target would be
-skipped for ``SKIP_NO_CONTACT``. Never raises across its boundary: a no-candidate / failed
+finder). Owns no orchestration: the account's Find contacts action and the account pipeline agent
+call it. (It lived in ``nexus/campaigns/`` because the old campaign draft phase was its first
+caller; that engine is gone, spec §13.) Never raises across its boundary: a no-candidate / failed
 sourcing returns ``SourcingOutcome(None, False, 0.0)`` so the caller can skip cleanly. All
 synthetic personas are provenance-marked (``enrichment_source="sourcing:<provider>"``) and,
@@ -16,5 +17,5 @@ from nexus.core.tenancy import TenantSession
 from nexus.models.account import Account, Contact
 
-logger = logging.getLogger("nexus.campaigns.sourcing")
+logger = logging.getLogger("nexus.contacts.sourcing")
 
 
```

---

### Task 4: Remove the old engine

- [ ] **Step 1: Worker jobs** — `run_campaign` and `advance_cadences` out of `nexus/workers/tasks.py`:

```diff
diff --git a/nexus/workers/tasks.py b/nexus/workers/tasks.py
index 6e748e3..a859431 100644
--- a/nexus/workers/tasks.py
+++ b/nexus/workers/tasks.py
@@ -66,67 +66,4 @@ async def handle_run_orchestration(payload: dict) -> dict:
 
 
-async def handle_run_campaign(payload: dict) -> dict:
-    """Off-request campaign driver. ``phase`` selects the work:
-    ``"draft"`` runs the draft phase; ``"send"`` runs the send phase. Idempotent — a
-    campaign already past the requested phase is a no-op returning its current status."""
-    tenant_id = payload["tenant_id"]
-    campaign_id = payload["campaign_id"]
-    phase = payload.get("phase", "draft")
-    from nexus.models.campaign import Campaign, CAMP_AWAITING_APPROVAL, CAMP_TERMINAL
-    from nexus.campaigns.service import get_campaign_service
-
-    async with tenant_session(tenant_id) as ts:
-        campaign = await ts.get(Campaign, campaign_id)
-        if campaign is None:
-            return {"error": "campaign_not_found", "campaign_id": campaign_id}
-        svc = get_campaign_service()
-        if phase == "draft" and campaign.status not in CAMP_TERMINAL \
-                and campaign.status != CAMP_AWAITING_APPROVAL:
-            await svc.run_draft_phase(ts, campaign)
-        elif phase == "send" and campaign.status == "approved":
-            await svc.run_send_phase(ts, campaign)
-        return {"campaign_id": campaign.id, "status": campaign.status}
-
-
-async def handle_advance_cadences(payload: dict) -> dict:
-    """Periodic cadence driver. Scans globally for tenants with a due enrollment, then
-    advances each tenant's due enrollments inside its own tenant-bound session.
-
-    The scan uses a raw, tenant-agnostic session (it only reads tenant ids, never ORM
-    rows), keeping the per-tenant isolation guarantee for the actual work. Inert unless
-    ``cadence_enabled`` is set."""
-    from datetime import datetime, timezone
-
-    from sqlalchemy import distinct, select
-
-    from nexus.core.config import get_settings
-    from nexus.cadences.service import get_cadence_service
-    from nexus.models.cadence import CadenceEnrollment, ENROLL_ACTIVE
-
-    settings = get_settings()
-    if not settings.cadence_enabled:
-        return {"skipped": "cadence_disabled"}
-
-    now = datetime.now(timezone.utc)
-    batch = settings.cadence_batch_size
-
-    async with get_sessionmaker()() as session:
-        rows = await session.scalars(
-            select(distinct(CadenceEnrollment.tenant_id)).where(
-                CadenceEnrollment.status == ENROLL_ACTIVE,
-                CadenceEnrollment.next_touch_at <= now,
-            )
-        )
-        tenant_ids = list(rows.all())
-
-    processed = 0
-    for tid in tenant_ids:
-        async with tenant_session(tid) as ts:
-            processed += await get_cadence_service().advance_due_for_tenant(
-                ts, now=now, limit=batch
-            )
-    return {"tenants": len(tenant_ids), "processed": processed}
-
-
 async def handle_refresh_due_accounts(payload: dict) -> dict:
     """Periodic account-refresh driver. Scans globally for accounts that are due for a
@@ -1237,6 +1174,4 @@ HANDLERS: dict[str, Handler] = {
     "process_account": handle_process_account,
     "run_orchestration": handle_run_orchestration,
-    "run_campaign": handle_run_campaign,
-    "advance_cadences": handle_advance_cadences,
     "refresh_due_accounts": handle_refresh_due_accounts,
     "sync_crm_account": handle_sync_crm_account,
@@ -1291,21 +1226,4 @@ async def enqueue_run_orchestration(
 
 
-async def enqueue_run_campaign(
-    tenant_id: str, campaign_id: str, *, phase: str = "draft", queue: TaskQueue | None = None
-) -> None:
-    queue = queue or get_task_queue()
-    await queue.enqueue(
-        Job(
-            name="run_campaign",
-            payload={"tenant_id": tenant_id, "campaign_id": campaign_id, "phase": phase},
-        )
-    )
-
-
-async def enqueue_advance_cadences(*, queue: TaskQueue | None = None) -> None:
-    queue = queue or get_task_queue()
-    await queue.enqueue(Job(name="advance_cadences", payload={}))
-
-
 async def enqueue_refresh_due_accounts(*, queue: TaskQueue | None = None) -> None:
     queue = queue or get_task_queue()
```

and out of the heartbeat, `nexus/workers/scheduler.py`:

```diff
diff --git a/nexus/workers/scheduler.py b/nexus/workers/scheduler.py
index 85ee2aa..8091336 100644
--- a/nexus/workers/scheduler.py
+++ b/nexus/workers/scheduler.py
@@ -3,5 +3,5 @@
 A periodic coroutine that runs alongside the pull-only worker loop. Each tick, while the
 global ``automation_enabled`` switch is on, it enqueues the recurring driver jobs
-(``advance_cadences`` + ``refresh_due_accounts``). Both drivers are idempotent and
+(``refresh_due_accounts`` and the engagement drivers). Every driver is idempotent and
 self-filtering, so enqueuing them every tick is safe and needs no per-job bookkeeping.
 
@@ -19,5 +19,4 @@ from nexus.core.db import get_sessionmaker
 from nexus.workers.queue import TaskQueue, get_task_queue
 from nexus.workers.tasks import (
-    enqueue_advance_cadences,
     enqueue_backfill_companies,
     enqueue_crawl_companies,
@@ -54,5 +53,5 @@ async def _enqueue_due(queue: TaskQueue) -> int:
 
     Runs under a per-tick advisory lock so only the leader worker enqueues (see module docstring).
-    The cadence + account-refresh drivers gate on automation_enabled; the CRM sweep gates on its
+    The account-refresh drivers gate on automation_enabled; the CRM sweep gates on its
     own crm_sync_enabled switch. Each handler re-checks its switch, so this is a pre-filter.
 
@@ -126,5 +125,4 @@ async def _enqueue_due(queue: TaskQueue) -> int:
                     count += 1
             if settings.automation_enabled:
-                await enqueue_advance_cadences(queue=queue)
                 await enqueue_refresh_due_accounts(queue=queue)
                 # Digest rides the automation switch; its handler is idempotent per interval.
@@ -133,5 +131,5 @@ async def _enqueue_due(queue: TaskQueue) -> int:
                 # slot (Tenant.icp_discovery_last_run_at, row-locked), so it can't double-run.
                 await enqueue_discover_icp_accounts(queue=queue)
-                count += 4
+                count += 3
             if settings.crm_sync_enabled:
                 await enqueue_sync_crm_due_accounts(queue=queue)
```

- [ ] **Step 2: Routers** — `nexus/api/routers/__init__.py`:

```diff
diff --git a/nexus/api/routers/__init__.py b/nexus/api/routers/__init__.py
index 3353907..a94ac1c 100644
--- a/nexus/api/routers/__init__.py
+++ b/nexus/api/routers/__init__.py
@@ -22,7 +22,5 @@ from nexus.api.routers import (
     auth,
     billing,
-    cadences,
     calling,
-    campaigns,
     chat,
     contacts,
@@ -57,6 +55,4 @@ all_routers = [
     agents.router,
     workflow.router,
-    campaigns.router,
-    cadences.router,
     calling.router,
     alerts.router,
```

- [ ] **Step 3: Settings** — the old engine's five settings leave the catalog, and the engine is on by default. `nexus/runtime_config/catalog.py`:

```diff
diff --git a/nexus/runtime_config/catalog.py b/nexus/runtime_config/catalog.py
index 34ecf82..a94e4d6 100644
--- a/nexus/runtime_config/catalog.py
+++ b/nexus/runtime_config/catalog.py
@@ -36,5 +36,4 @@ be asked:
 * ``personalization_posts_window``: passed verbatim to the Apify posts actor, whose accepted values
   nobody has observed.
-* ``cadence_tick_interval_s``: nothing reads it.
 * ``signal_sources``: read once into the ingestion service singleton, which the test suite injects
   its demo source through, so there is no safe rebuild hook for it.
@@ -163,11 +162,4 @@ _SPECS: tuple[SettingSpec, ...] = (
         option_labels=(("search", "Exa search"), ("stub", "Off (test double)")),
     ),
-    SettingSpec(
-        key="campaign_sourcing_enabled", label="Contact sourcing", group=CONTACTS, kind="bool",
-        effect="Enables net-new contact providers and the verifying email finder.",
-        warning="Each sourced contact is a paid lookup plus a verification. Off means the stub, "
-                "which returns nothing rather than costing anything.",
-        risk="medium",
-    ),
     SettingSpec(
         key="account_enrich_enabled", label="Fill blank firmographics", group=CONTACTS,
@@ -451,33 +443,4 @@ _SPECS: tuple[SettingSpec, ...] = (
 
     # ---- outreach and CRM -----------------------------------------------------------------------
-    SettingSpec(
-        key="cadence_enabled", label="Email cadences", group=OUTREACH, kind="bool",
-        effect="Turns on the multi-touch cadence engine. The advance tick is a no-op until this "
-               "is set.",
-        warning="Cadence steps send real email to real prospects. Check the sending domain and the "
-                "drafted copy before enabling.",
-        risk="high",
-    ),
-    SettingSpec(
-        key="cadence_batch_size", label="Enrollments advanced per tick", group=OUTREACH,
-        kind="int", minimum=1, maximum=1000,
-        effect="Most cadence enrollments one worker advances per tick.",
-    ),
-    SettingSpec(
-        key="cadence_max_duration_days", label="Stop an enrollment after (days)", group=OUTREACH,
-        kind="int", minimum=1, maximum=365,
-        effect="An enrollment running longer than this is stopped mid-sequence.",
-        warning="Lowering it stops long sequences that are already in flight on the next tick.",
-        risk="medium",
-    ),
-    SettingSpec(
-        key="campaign_sourced_min_send_confidence", label="Sourced address send bar",
-        group=OUTREACH, kind="float", minimum=0, maximum=1,
-        effect="The verification confidence an address the product found (rather than one a "
-               "customer imported) must reach before a campaign sends to it.",
-        warning="Lower sends bulk email to less-proven addresses, which raises bounces and can get "
-                "the sending domain blocklisted.",
-        risk="high",
-    ),
     SettingSpec(
         key="crm_sync_enabled", label="Push to CRM", group=OUTREACH, kind="bool",
@@ -552,6 +515,7 @@ _SPECS: tuple[SettingSpec, ...] = (
         effect="Turns on the engagement campaigns, the reply desk and the workers that send "
                "follow-ups and read replies.",
-        warning="Campaign steps send real email from SDR mailboxes and replies are read and "
-                "acted on. Switch on only after the cutover dry run has been reviewed.",
+        warning="On by default since the cutover (spec §13): it replaced the old Campaigns and "
+                "Cadences. Off stops every campaign sending and every mailbox being read, and "
+                "hides the campaign, reply and template screens for every workspace.",
         risk="high",
     ),
```

`nexus/core/config.py`:

```diff
diff --git a/nexus/core/config.py b/nexus/core/config.py
index 5354695..d60b080 100644
--- a/nexus/core/config.py
+++ b/nexus/core/config.py
@@ -405,20 +405,10 @@ class Settings(BaseSettings):
     email_finder_max_candidates: int = 12       # permutation cap per contact (10 patterns + headroom)
     contact_search_sources: str = "stub"        # ordered net-new contact providers
-    campaign_sourcing_enabled: bool = True       # inline auto-retry on SKIP_NO_CONTACT
-    campaign_sourced_min_send_confidence: float = 0.5  # bar a sourced address must clear to send
     crm_provider: str = "stub"                 # outbound CRM connector: stub|salesforce|hubspot
     hubspot_access_token: str = ""             # HubSpot private-app token (when crm_provider=hubspot)
     hubspot_api_base: str = "https://api.hubapi.com"  # override for region/proxy/testing
-    # Channel & Cadence (sub-project C): multi-touch email cadence engine. Disabled by
-    # default (safe opt-in, like campaign_sourcing) so the advance tick is a no-op until a
-    # deployment turns it on with one env line.
-    cadence_enabled: bool = False             # master switch for the advance tick
-    cadence_tick_interval_s: int = 60         # production due-scan cadence (seconds)
-    cadence_batch_size: int = 100             # max enrollments claimed per tick per worker
-    cadence_max_duration_days: int = 30       # duration-cap safety bound (mid-sequence stop)
-
     # Continuous Automation (sub-project D): autonomous heartbeat that drives the recurring
-    # GTM loop (account refresh + cadence advance). OFF by default (safe opt-in, like
-    # cadence_enabled) so the test suite stays deterministic and zero-network.
+    # GTM loop (account refresh, digests, ICP discovery). OFF by default (safe opt-in) so the test
+    # suite stays deterministic and zero-network.
     automation_enabled: bool = False            # global master switch for the heartbeat
     automation_tick_interval_s: int = 60        # heartbeat period (seconds)
@@ -558,7 +548,8 @@ class Settings(BaseSettings):
     # notifications. Never client-supplied.
     engagement_public_base_url: str = ""
-    # Release B switch: campaigns, the reply desk and their workers. Mailbox connection and the
-    # ledger do not depend on it.
-    engagement_campaigns_enabled: bool = False
+    # Campaigns, the reply desk and their workers. ON since the cutover (spec §13), which removed
+    # the old Campaigns and Cadences; off is now the emergency stop for sending and mailbox reads.
+    # Mailbox connection and the ledger do not depend on it.
+    engagement_campaigns_enabled: bool = True
     # Training & insights ledger stores and the pseudonymisation secret (§18). Managed in
     # Provider keys (ledger_archive, ledger_training, ledger_insights, ledger_pseudonym).
```

- [ ] **Step 4: Delete the code:**

```bash
git rm -r nexus/campaigns nexus/cadences nexus/api/routers/campaigns.py nexus/api/routers/cadences.py
```

The models in `nexus/models/campaign.py` and `nexus/models/cadence.py` stay: their tables are history, the migration reads them, and the Legacy list reads them.

- [ ] **Step 5: Commit**

```bash
git add -A nexus scripts
git commit -m "feat(engagement): cutover migration; the old Campaigns and Cadences engines removed"
```

---

### Task 5: The screens

- [ ] **Step 1: Gate open, not closed** — `frontend/src/app/EngagementContext.tsx`:

```tsx
import { createContext, useContext, type ReactNode } from "react";
import { Skeleton } from "@/components/ui";
import { FeatureUnavailable } from "@/components/FeatureUnavailable";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import type { EngagementStatus } from "@/lib/types";

/**
 * Whether the engagement engine is switched on, fetched once at the shell.
 *
 * Since the cutover (spec §13) the engine is the only campaign engine, on by default, and switching
 * it off is the platform's emergency stop for sending. So this now fails OPEN, like
 * `EntitlementsContext`: an unreadable status reads as on, because the server is the boundary (every
 * engagement route answers 404 while the engine is off) and hiding the only campaign screens on a
 * network blip would take away an SDR's daily driver. Before the cutover it failed closed, because
 * the old Campaigns and Cadences worked either way and were the safe place to send people.
 */
const EngagementContext = createContext<EngagementStatus | null>(null);

export function useEngagementStatus(): EngagementStatus | null {
  return useContext(EngagementContext);
}

/** `true` / `false` once known (an unreadable status is `true`), `null` while loading. */
export function useEngineOn(): boolean | null {
  const status = useContext(EngagementContext);
  return status ? status.engine_on : null;
}

export function EngagementProvider({ children }: { children: ReactNode }) {
  const api = useApiClient();
  const state = useApi<EngagementStatus>((signal) => api.engagementStatus(signal), []);
  const value = state.data ?? (state.error ? { engine_on: true, can_manage: false } : null);
  return <EngagementContext.Provider value={value}>{children}</EngagementContext.Provider>;
}

/**
 * An engagement page renders unless the engine is confirmed off. Off is said in place, keeping the
 * URL, so the page comes back where the reader left it when the engine is switched on again; there
 * is no other campaign screen to send them to.
 */
export function RequireEngine({ children, name = "Campaigns" }: {
  children: ReactNode;
  name?: string;
}) {
  const on = useEngineOn();
  if (on === null) return <Skeleton width="100%" height={240} />;
  if (!on) {
    return (
      <FeatureUnavailable
        name={name} state="maintenance"
        message="Campaigns, replies and sequence templates are switched off for now. Nothing is being sent or read from your mailbox."
      />
    );
  }
  return <>{children}</>;
}
```

- [ ] **Step 2: Navigation and routes** — `frontend/src/app/nav.tsx`:

```diff
diff --git a/frontend/src/app/nav.tsx b/frontend/src/app/nav.tsx
index 5a1aa72..b24001f 100644
--- a/frontend/src/app/nav.tsx
+++ b/frontend/src/app/nav.tsx
@@ -53,10 +53,10 @@ export interface NavItem {
   capability?: string;
   /**
-   * Which engagement engine the item belongs to. `"on"` items (the new Campaigns, Replies and
-   * Sequence templates) appear only once the engine is confirmed switched on; `"off"` items (the
-   * old Campaigns and Cadences) disappear at that moment. Before phase 15 removes the old engine,
-   * this is the whole of the switch-over in the navigation: one flag, two sets of pages, never both.
+   * The item belongs to the engagement engine (Campaigns, Replies, Sequence templates) and leaves
+   * the navigation only while the engine is confirmed switched off, the platform's emergency stop.
+   * Before the cutover this also carried `"off"` items, the old Campaigns and Cadences; that engine
+   * is gone (spec §13), so only `"on"` remains.
    */
-  engine?: "on" | "off";
+  engine?: "on";
 }
 
@@ -105,15 +105,4 @@ export const NAV_ITEMS: NavItem[] = [
     capability: "module.agents",
   },
-  // Their OWN gates, not `module.outreach`. Holding bulk outreach back is a common position, and
-  // while these shared a gate with `ai.email_draft` it could not be taken without also taking down
-  // the email composer a rep uses one contact at a time.
-  {
-    to: "/campaigns", label: "Campaigns", icon: <SendIcon />, minRole: "manager",
-    capability: "module.campaigns", engine: "off",
-  },
-  {
-    to: "/cadences", label: "Cadences", icon: <MessageIcon />, minRole: "manager",
-    capability: "module.cadences", engine: "off",
-  },
   {
     to: "/plays", label: "Plays", icon: <BoltIcon />, minRole: "manager",
@@ -156,8 +145,7 @@ export function canSee(
   // enforces this regardless — the nav entry only decides whether the link is offered.
   if (item.platformOnly) return isPlatformAdmin;
-  // New-engine pages only once the switch is CONFIRMED on; old-engine pages until it is. Unknown
-  // (still loading) keeps the old ones, which work either way.
-  if (item.engine === "on" && engineOn !== true) return false;
-  if (item.engine === "off" && engineOn === true) return false;
+  // Engagement pages leave only while the engine is CONFIRMED off. Unknown (still loading) shows
+  // them, so the sidebar does not rearrange itself a moment after every page load.
+  if (item.engine === "on" && engineOn === false) return false;
   if (!item.minRole) return true;
   if (!role) return false;
```

`frontend/src/App.tsx`:

```diff
diff --git a/frontend/src/App.tsx b/frontend/src/App.tsx
index a005609..be9fb24 100644
--- a/frontend/src/App.tsx
+++ b/frontend/src/App.tsx
@@ -64,6 +64,4 @@ const RunDetailPage = lazyPage(() => import("@/pages/RunDetailPage"), "RunDetail
 const ApprovalsPage = lazyPage(() => import("@/pages/ApprovalsPage"), "ApprovalsPage");
 const ChatPage = lazyPage(() => import("@/pages/ChatPage"), "ChatPage");
-const CampaignsPage = lazyPage(() => import("@/pages/CampaignsPage"), "CampaignsPage");
-const CadencesPage = lazyPage(() => import("@/pages/CadencesPage"), "CadencesPage");
 const MailboxesPage = lazyPage(() => import("@/pages/engagement/MailboxesPage"), "MailboxesPage");
 const DataUsePage = lazyPage(() => import("@/pages/DataUsePage"), "DataUsePage");
@@ -319,14 +317,8 @@ export function App() {
                   }
                 />
-                <Route
-                  path="/campaigns"
-                  element={
-                    <RequireRole minRole="manager">
-                      <RequireCapability capability="module.campaigns" name="Campaigns">
-                        <CampaignsPage />
-                      </RequireCapability>
-                    </RequireRole>
-                  }
-                />
+                {/* The old Campaigns and Cadences were replaced at the cutover (spec §13); their
+                    addresses still arrive from bookmarks and old links, so they forward. */}
+                <Route path="/campaigns" element={<Navigate to="/engagement/campaigns" replace />} />
+                <Route path="/cadences" element={<Navigate to="/engagement/templates" replace />} />
                 {/* Every member: an SDR connects their own mailbox. Gated like the email composer,
                     on module.outreach, so a plan without outreach hides it everywhere. */}
@@ -382,5 +374,5 @@ export function App() {
                   path="/engagement/replies"
                   element={
-                    <RequireEngine>
+                    <RequireEngine name="Replies">
                       <RequireCapability capability="module.campaigns" name="Replies">
                         <ReplyDeskPage />
@@ -392,5 +384,5 @@ export function App() {
                   path="/engagement/replies/settings"
                   element={
-                    <RequireEngine>
+                    <RequireEngine name="Reply settings">
                       <RequireCapability capability="module.campaigns" name="Reply settings">
                         <EngagementSettingsPage />
@@ -402,5 +394,5 @@ export function App() {
                   path="/engagement/templates"
                   element={
-                    <RequireEngine fallback="/cadences">
+                    <RequireEngine name="Sequence templates">
                       <RequireCapability capability="module.cadences" name="Sequence templates">
                         <SequenceTemplatesPage />
@@ -409,14 +401,4 @@ export function App() {
                   }
                 />
-                <Route
-                  path="/cadences"
-                  element={
-                    <RequireRole minRole="manager">
-                      <RequireCapability capability="module.cadences" name="Cadences">
-                        <CadencesPage />
-                      </RequireCapability>
-                    </RequireRole>
-                  }
-                />
                 <Route
                   path="/plays"
```

`frontend/src/components/layout/Sidebar.tsx`:

```diff
diff --git a/frontend/src/components/layout/Sidebar.tsx b/frontend/src/components/layout/Sidebar.tsx
index 1c411d0..19673cb 100644
--- a/frontend/src/components/layout/Sidebar.tsx
+++ b/frontend/src/components/layout/Sidebar.tsx
@@ -27,6 +27,6 @@ export function Sidebar({ open, collapsed = false, onNavigate, onToggleCollapse
   // slow or failing billing endpoint never deletes the customer's navigation.
   const entitlements = useEntitlements();
-  // Which engagement engine the workspace is on: the new Campaigns, Replies and Sequence templates
-  // replace the old Campaigns and Cadences only once the switch is confirmed on.
+  // The engagement pages (Campaigns, Replies, Sequence templates) leave only while the engine is
+  // confirmed switched off, the platform's emergency stop.
   const engineOn = useEngineOn();
 
```

The reply desk now has four tabs, which is wider than a phone-width pane: the `Tabs` primitive scrolls sideways instead of widening the page, keeps labels on one line, draws its divider as an inset shadow (a scroller clips children at its padding edge, which would cut the selected underline), and is `position: relative`, as every scroller must be (`tests/test_scroll_containers.py`). The segmented variant keeps its own border. `frontend/src/components/ui/Tabs.module.css`:

```diff
diff --git a/frontend/src/components/ui/Tabs.module.css b/frontend/src/components/ui/Tabs.module.css
index b6ac923..f4f4e9e 100644
--- a/frontend/src/components/ui/Tabs.module.css
+++ b/frontend/src/components/ui/Tabs.module.css
@@ -2,6 +2,10 @@
    it (four tabs on a phone-width reply desk did both). The divider is an inset shadow, not a border:
    a scrolling box clips its children at the padding edge, which would cut the selected tab's
-   underline where it used to overlap the border, and a child always paints over its parent's shadow. */
+   underline where it used to overlap the border, and a child always paints over its parent's shadow.
+   `position: relative` makes the scroller the containing block of what it scrolls, or an absolutely
+   positioned child (every `.sr-only` label) escapes it and stretches the page
+   (tests/test_scroll_containers.py). */
 .tabs {
+  position: relative;
   display: flex;
   gap: var(--space-1);
```

- [ ] **Step 3: Delete the old pages:**

```bash
git rm frontend/src/pages/CampaignsPage.tsx frontend/src/pages/CampaignsPage.module.css frontend/src/pages/CadencesPage.tsx frontend/src/pages/CadencesPage.module.css
```

- [ ] **Step 4: Client and types** — the old campaign and cadence methods, the campaign event stream and their types go; the new calls come in. `frontend/src/lib/api.ts`:

```diff
diff --git a/frontend/src/lib/api.ts b/frontend/src/lib/api.ts
index 90e2cec..4ba5201 100644
--- a/frontend/src/lib/api.ts
+++ b/frontend/src/lib/api.ts
@@ -97,4 +97,5 @@ import type {
   TodayItem,
   ContactInsight,
+  LegacyCampaign,
   BestTimeSuggestion,
   ReferralCandidate,
@@ -125,13 +126,4 @@ import type {
   DialResult,
   CallBrief,
-  Cadence,
-  CadenceEnrollment,
-  CadenceInput,
-  CadenceReport,
-  Campaign,
-  CampaignDetail,
-  CampaignInput,
-  CampaignPreview,
-  CampaignProgress,
   ChatSession,
   ChatStreamEvent,
@@ -141,5 +133,4 @@ import type {
   CreateSessionRequest,
   TitleRecommendation,
-  LaunchFromSelectionInput,
   LookalikeResponse,
   ContactLookalikeResponse,
@@ -157,5 +148,4 @@ import type {
   CustomFieldDef,
   DiscoveryResult,
-  EnrollmentDetail,
   InboxTask,
   LearnedWeights,
@@ -855,78 +845,4 @@ export class ApiClient {
   }
 
-  // ---- segment campaigns ----
-  listCampaigns(signal?: AbortSignal) {
-    return this.request<Campaign[]>("/campaigns", { signal });
-  }
-  getCampaign(id: string, signal?: AbortSignal) {
-    return this.request<CampaignDetail>(`/campaigns/${id}`, { signal });
-  }
-  createCampaign(body: CampaignInput, signal?: AbortSignal) {
-    return this.request<Campaign>("/campaigns", { method: "POST", body, signal });
-  }
-  launchFromSelection(body: LaunchFromSelectionInput, signal?: AbortSignal) {
-    return this.request<Campaign>("/campaigns/launch-from-selection", {
-      method: "POST",
-      body,
-      signal,
-    });
-  }
-  previewCampaign(id: string, signal?: AbortSignal) {
-    return this.request<CampaignPreview>(`/campaigns/${id}/preview`, { signal });
-  }
-  approveCampaign(id: string, signal?: AbortSignal) {
-    return this.request<Campaign>(`/campaigns/${id}/approve`, { method: "POST", signal });
-  }
-  cancelCampaign(id: string, signal?: AbortSignal) {
-    return this.request<Campaign>(`/campaigns/${id}/cancel`, { method: "POST", signal });
-  }
-
-  // ---- cadences ----
-  listCadences(signal?: AbortSignal) {
-    return this.request<Cadence[]>("/cadences", { signal });
-  }
-  getCadence(id: string, signal?: AbortSignal) {
-    return this.request<Cadence>(`/cadences/${id}`, { signal });
-  }
-  createCadence(body: CadenceInput, signal?: AbortSignal) {
-    return this.request<Cadence>("/cadences", { method: "POST", body, signal });
-  }
-  deactivateCadence(id: string, signal?: AbortSignal) {
-    return this.request<null>(`/cadences/${id}`, { method: "DELETE", signal });
-  }
-  listEnrollments(campaignId: string, signal?: AbortSignal) {
-    return this.request<CadenceEnrollment[]>(
-      `/campaigns/${campaignId}/enrollments`,
-      { signal },
-    );
-  }
-  getEnrollment(id: string, signal?: AbortSignal) {
-    return this.request<EnrollmentDetail>(`/enrollments/${id}`, { signal });
-  }
-  cadenceReport(campaignId: string, signal?: AbortSignal) {
-    return this.request<CadenceReport>(`/campaigns/${campaignId}/cadence-report`, { signal });
-  }
-  pauseEnrollment(id: string, signal?: AbortSignal) {
-    return this.request<CadenceEnrollment>(`/enrollments/${id}/pause`, { method: "POST", signal });
-  }
-  resumeEnrollment(id: string, signal?: AbortSignal) {
-    return this.request<CadenceEnrollment>(`/enrollments/${id}/resume`, { method: "POST", signal });
-  }
-  stopEnrollment(id: string, signal?: AbortSignal) {
-    return this.request<CadenceEnrollment>(`/enrollments/${id}/stop`, { method: "POST", signal });
-  }
-  approveTouch(enrollmentId: string, stepIndex: number, editedBody?: string, signal?: AbortSignal) {
-    return this.request<CadenceEnrollment>(
-      `/enrollments/${enrollmentId}/touches/${stepIndex}/approve`,
-      { method: "POST", body: { edited_body: editedBody ?? null }, signal },
-    );
-  }
-  rejectTouch(enrollmentId: string, stepIndex: number, stop = false, signal?: AbortSignal) {
-    return this.request<CadenceEnrollment>(
-      `/enrollments/${enrollmentId}/touches/${stepIndex}/reject`,
-      { method: "POST", body: { stop }, signal },
-    );
-  }
-
   // ---- outcome-feedback loop ----
   recordOutcome(body: OutcomeInput, signal?: AbortSignal) {
@@ -1290,7 +1206,18 @@ export class ApiClient {
     return this.request<EngagementCandidate[]>("/engagement/candidates", { query: params, signal });
   }
-  addCampaignContacts(id: string, contactIds: string[]) {
+  /** Add people by id, and everyone with an email address at the given companies. */
+  addCampaignContacts(id: string, contactIds: string[], accountIds: string[] = []) {
     return this.request<EnrollResult>(`/engagement/campaigns/${id}/contacts`, {
-      method: "POST", body: { contact_ids: contactIds },
+      method: "POST", body: { contact_ids: contactIds, account_ids: accountIds },
+    });
+  }
+  setCampaignMailbox(id: string, mailboxId: string) {
+    return this.request<EngagementCampaign>(`/engagement/campaigns/${id}/mailbox`, {
+      method: "PUT", body: { mailbox_id: mailboxId },
+    });
+  }
+  legacyCampaigns(team = false, signal?: AbortSignal) {
+    return this.request<LegacyCampaign[]>("/engagement/legacy-campaigns", {
+      query: { team }, signal,
     });
   }
@@ -2171,43 +2098,4 @@ export class ApiClient {
     }
   }
-
-  /**
-   * Stream a campaign's draft/send progress over SSE. Same hand-rolled fetch+reader as
-   * `streamRunEvents` (EventSource can't send Authorization). The server emits `progress`
-   * frames (status + per-status target counts) and closes at a terminal status or the
-   * approval gate — at which point the promise resolves and the caller refetches once.
-   */
-  async streamCampaignEvents(
-    campaignId: string,
-    opts: { onProgress: (p: CampaignProgress) => void; lastEventId?: number },
-    signal?: AbortSignal,
-  ): Promise<void> {
-    const headers: Record<string, string> = { Accept: "text/event-stream" };
-    if (this.token) headers["Authorization"] = `Bearer ${this.token}`;
-    if (opts.lastEventId) headers["Last-Event-ID"] = String(opts.lastEventId);
-    const res = await fetch(this.buildUrl(`/campaigns/${campaignId}/events`), { headers, signal });
-    if (res.status === 401) this.onUnauthorized?.();
-    if (!res.ok || !res.body) {
-      throw new ApiError(res.status, res.statusText || "Couldn't open the campaign stream");
-    }
-    const reader = res.body.getReader();
-    const decoder = new TextDecoder();
-    let buffer = "";
-    try {
-      for (;;) {
-        const { value, done } = await reader.read();
-        if (done) break;
-        buffer += decoder.decode(value, { stream: true });
-        const frames = buffer.split("\n\n");
-        buffer = frames.pop() ?? "";
-        for (const frame of frames) {
-          const ev = parseSseFrame(frame);
-          if (ev && ev.type === "progress") opts.onProgress(ev.data as unknown as CampaignProgress);
-        }
-      }
-    } finally {
-      reader.cancel().catch(() => {});
-    }
-  }
 }
 
```

`frontend/src/lib/types.ts`:

```diff
diff --git a/frontend/src/lib/types.ts b/frontend/src/lib/types.ts
index f746680..5038918 100644
--- a/frontend/src/lib/types.ts
+++ b/frontend/src/lib/types.ts
@@ -936,159 +936,4 @@ export interface NewWorkspaceRequest {
 }
 
-// ---- segment campaigns ----
-export type CampaignStatus =
-  | "draft_pending"
-  | "drafting"
-  | "awaiting_approval"
-  | "approved"
-  | "sending"
-  | "completed"
-  | "cancelled"
-  | "failed";
-
-export type CampaignTargetStatus =
-  | "pending"
-  | "drafting"
-  | "drafted"
-  | "skipped"
-  | "approved"
-  | "sent"
-  | "failed";
-
-export interface CampaignTarget {
-  id: string;
-  account_id: string;
-  status: CampaignTargetStatus | string;
-  skip_reason: string | null;
-  draft: Record<string, unknown>;
-  error: string | null;
-}
-
-export interface Campaign {
-  id: string;
-  name: string;
-  list_id: string;
-  status: CampaignStatus | string;
-  sequence: string;
-  icp: Record<string, unknown>;
-  report: Record<string, number>;
-  send_risky: boolean;
-  cadence_id: string | null;
-  review_each_touch: boolean;
-  created_at: string;
-}
-
-export interface CampaignDetail extends Campaign {
-  targets: CampaignTarget[];
-  /** Reply attribution: per-stage outcome counts recorded against this campaign. */
-  outcomes: Record<string, number>;
-}
-
-export interface CampaignPreview {
-  campaign_id: string;
-  status: CampaignStatus | string;
-  report: Record<string, number>;
-  sample: CampaignTarget[];
-}
-
-export interface CampaignInput {
-  name: string;
-  list_id: string;
-  icp?: Record<string, unknown>;
-  sequence?: string;
-  send_risky?: boolean;
-  cadence_id?: string | null;
-  review_each_touch?: boolean;
-}
-
-/** Turn a discovery-results selection into a gated personalized cadence in one call. */
-export interface LaunchFromSelectionInput {
-  name: string;
-  account_ids?: string[];
-  contact_ids?: string[];
-  icp?: Record<string, unknown>;
-  mode: "new_cadence" | "existing_cadence";
-  cadence_id?: string | null;
-  review_each_touch?: boolean;
-}
-
-/** One frame from a campaign's SSE progress stream (status + per-status target counts). */
-export interface CampaignProgress {
-  status: CampaignStatus | string;
-  counts: Record<string, number>;
-  report: Record<string, number>;
-}
-
-// ---- cadences ----
-export type EnrollmentStatus = "active" | "paused" | "completed" | "stopped";
-export type TouchStatus = "sent" | "skipped" | "failed" | "awaiting_approval";
-
-export interface CadenceStep {
-  step_index: number;
-  delay_days: number;
-  angle: string;
-  channel: string;
-}
-
-export interface CadenceStepInput {
-  delay_days: number;
-  angle: string;
-  channel: string;
-}
-
-export interface Cadence {
-  id: string;
-  name: string;
-  description: string | null;
-  is_active: boolean;
-  created_at: string;
-  steps: CadenceStep[];
-}
-
-export interface CadenceInput {
-  name: string;
-  description?: string | null;
-  steps: CadenceStepInput[];
-}
-
-export interface CadenceEnrollment {
-  id: string;
-  campaign_id: string;
-  account_id: string;
-  contact_id: string | null;
-  cadence_id: string;
-  current_step_index: number;
-  status: EnrollmentStatus | string;
-  stop_reason: string | null;
-  next_touch_at: string | null;
-  started_at: string | null;
-  completed_at: string | null;
-}
-
-export interface CadenceTouch {
-  id: string;
-  enrollment_id: string;
-  step_index: number;
-  status: TouchStatus | string;
-  skip_reason: string | null;
-  run_id: string | null;
-  sent_at: string | null;
-  error: string | null;
-}
-
-export interface EnrollmentDetail extends CadenceEnrollment {
-  touches: CadenceTouch[];
-}
-
-export interface CadenceReport {
-  campaign_id: string;
-  cadence_id: string | null;
-  total_enrollments: number;
-  by_status: Record<string, number>;
-  touches_sent: number;
-  touches_skipped: number;
-  stops: Record<string, number>;
-}
-
 // ---- automation + CRM sync (settings) ----
 export interface AutomationSettings {
@@ -2621,2 +2466,12 @@ export interface RestartSuggestion {
   likelihood: ReplyLikelihood["band"];
 }
+
+/** A campaign the old engine finished, kept read-only as history (spec §13). */
+export interface LegacyCampaign {
+  id: string;
+  name: string;
+  status: "completed" | "cancelled" | "failed";
+  created_at: string;
+  targets: number;
+  sent: number;
+}
```

`frontend/src/lib/display.ts` keeps only the tone the Legacy list uses:

```diff
diff --git a/frontend/src/lib/display.ts b/frontend/src/lib/display.ts
index 7cc9f2c..48c6150 100644
--- a/frontend/src/lib/display.ts
+++ b/frontend/src/lib/display.ts
@@ -145,5 +145,5 @@ export function priorityTone(priority: number): BadgeTone {
 }
 
-/** Campaign lifecycle status → tone. */
+/** An old-engine campaign's status → tone (the read-only Legacy list, spec §13). */
 export function campaignTone(status: string): BadgeTone {
   switch (status) {
@@ -163,48 +163,2 @@ export function campaignTone(status: string): BadgeTone {
 }
 
-/** Campaign target status → tone. */
-export function targetTone(status: string): BadgeTone {
-  switch (status) {
-    case "sent":
-      return "success";
-    case "drafted":
-    case "approved":
-      return "info";
-    case "skipped":
-      return "warning";
-    case "failed":
-      return "danger";
-    default:
-      return "neutral"; // pending, drafting
-  }
-}
-
-/** Cadence enrollment status → tone. */
-export function enrollmentTone(status: string): BadgeTone {
-  switch (status) {
-    case "active":
-      return "success";
-    case "paused":
-      return "warning";
-    case "stopped":
-      return "danger";
-    default:
-      return "neutral"; // completed
-  }
-}
-
-/** Cadence touch status → tone. */
-export function touchTone(status: string): BadgeTone {
-  switch (status) {
-    case "sent":
-      return "success";
-    case "awaiting_approval":
-      return "warning";
-    case "failed":
-      return "danger";
-    case "skipped":
-      return "neutral";
-    default:
-      return "neutral";
-  }
-}
```

- [ ] **Step 5: Discovery hands its selection to the builder** — `frontend/src/components/discovery/ResultsPanel.tsx`:

```diff
diff --git a/frontend/src/components/discovery/ResultsPanel.tsx b/frontend/src/components/discovery/ResultsPanel.tsx
index 129d3ad..6e29538 100644
--- a/frontend/src/components/discovery/ResultsPanel.tsx
+++ b/frontend/src/components/discovery/ResultsPanel.tsx
@@ -92,5 +92,4 @@ export function ResultsPanel({ runId }: ResultsPanelProps) {
   const [researchingId, setResearchingId] = useState<string | null>(null);
   const [listModalOpen, setListModalOpen] = useState(false);
-  const [cadenceModalOpen, setCadenceModalOpen] = useState(false);
 
   const candidates = data?.candidates ?? [];
@@ -394,7 +393,9 @@ export function ResultsPanel({ runId }: ResultsPanelProps) {
                 size="sm"
                 iconLeft={<Icons.SendIcon />}
-                onClick={() => setCadenceModalOpen(true)}
+                onClick={() => navigate("/engagement/campaigns/new", {
+                  state: selectionForCampaign(candidates, selected),
+                })}
               >
-                Add to cadence
+                Add to a campaign
               </Button>
             )}
@@ -463,15 +464,4 @@ export function ResultsPanel({ runId }: ResultsPanelProps) {
       />
 
-      <AddToCadenceModal
-        open={cadenceModalOpen}
-        candidates={candidates}
-        selected={selected}
-        onClose={() => setCadenceModalOpen(false)}
-        onLaunched={() => {
-          setCadenceModalOpen(false);
-          setSelected(new Set());
-        }}
-      />
-
       {canImport && (
         <ImportCsvModal
@@ -584,141 +574,20 @@ function SaveListModal({
 }
 
-/* ----------------------------- add to cadence ---------------------------- */
+/* ---------------------------- add to a campaign --------------------------- */
 
-function AddToCadenceModal({
-  open,
-  candidates,
-  selected,
-  onClose,
-  onLaunched,
-}: {
-  open: boolean;
-  candidates: DiscoveryCandidate[];
-  selected: Set<string>;
-  onClose: () => void;
-  onLaunched: () => void;
-}) {
-  const api = useApiClient();
-  const toast = useToast();
-  const navigate = useNavigate();
-  const [name, setName] = useState("");
-  const [mode, setMode] = useState<"new_cadence" | "existing_cadence">("new_cadence");
-  const [cadenceId, setCadenceId] = useState("");
-  const [busy, setBusy] = useState(false);
-
-  // Pull cadences only when the user actually wants to enroll into an existing one.
-  const cadences = useApi(
-    (signal) =>
-      open && mode === "existing_cadence" ? api.listCadences(signal) : Promise.resolve([]),
-    [open, mode],
-  );
-  const cadenceList = cadences.data ?? [];
-
-  // Split the selection by entity: contacts carry their account into the campaign.
-  const { accountIds, contactIds } = useMemo(() => {
-    const a: string[] = [];
-    const c: string[] = [];
-    for (const cand of candidates) {
-      if (!selected.has(cand.id)) continue;
-      if (cand.entity === "contact") c.push(cand.id);
-      else a.push(cand.id);
-    }
-    return { accountIds: a, contactIds: c };
-  }, [candidates, selected]);
-  const targetCount = accountIds.length + contactIds.length;
-
-  const needsCadence = mode === "existing_cadence" && cadenceId === "";
-  const disabled = name.trim() === "" || targetCount === 0 || needsCadence;
-
-  async function launch() {
-    if (disabled) return;
-    setBusy(true);
-    try {
-      await api.launchFromSelection({
-        name: name.trim(),
-        account_ids: accountIds,
-        contact_ids: contactIds,
-        mode,
-        cadence_id: mode === "existing_cadence" ? cadenceId : null,
-      });
-      toast.success(
-        "Cadence drafted",
-        "Personalized emails are drafted and waiting for your approval — nothing sends until you approve.",
-      );
-      setName("");
-      onLaunched();
-      navigate("/campaigns");
-    } catch (err) {
-      toast.error(
-        "Couldn't build the cadence",
-        err instanceof ApiError ? err.detail : "Please try again.",
-      );
-    } finally {
-      setBusy(false);
-    }
+/**
+ * The selection, split the way a campaign takes it: people by id, and companies, which the server
+ * turns into everyone there with an email address. The campaign builder receives it in router
+ * state and adds them once the campaign exists; nothing is drafted or sent until the SDR reviews.
+ */
+export function selectionForCampaign(candidates: DiscoveryCandidate[], selected: Set<string>) {
+  const contactIds: string[] = [];
+  const accountIds: string[] = [];
+  for (const cand of candidates) {
+    if (!selected.has(cand.id)) continue;
+    if (cand.entity === "contact") contactIds.push(cand.id);
+    else accountIds.push(cand.id);
   }
-
-  return (
-    <Modal
-      open={open}
-      onClose={onClose}
-      title="Add to cadence"
-      description="Drafts a personalized, multi-touch email sequence for the selected prospects. Nothing sends until you approve it."
-      size="sm"
-      footer={
-        <>
-          <Button variant="secondary" onClick={onClose} disabled={busy}>
-            Cancel
-          </Button>
-          <Button onClick={launch} loading={busy} disabled={disabled}>
-            Draft cadence
-          </Button>
-        </>
-      }
-    >
-      <Field label="Campaign name">
-        <Input
-          value={name}
-          onChange={(e) => setName(e.target.value)}
-          placeholder="e.g. Fintech Q3 outreach"
-          autoFocus
-        />
-      </Field>
-      <Field label="Cadence">
-        <Select
-          value={mode}
-          onChange={(e) => setMode(e.target.value as "new_cadence" | "existing_cadence")}
-          options={[
-            { value: "new_cadence", label: "New personalized cadence (3 touches)" },
-            { value: "existing_cadence", label: "Add to an existing cadence" },
-          ]}
-        />
-      </Field>
-      {mode === "existing_cadence" && (
-        <Field
-          label="Choose cadence"
-          hint={
-            !cadences.loading && cadenceList.length === 0
-              ? "No cadences yet — create one on the Cadences page first."
-              : undefined
-          }
-        >
-          <Select
-            value={cadenceId}
-            onChange={(e) => setCadenceId(e.target.value)}
-            disabled={cadences.loading || cadenceList.length === 0}
-            options={[
-              { value: "", label: cadences.loading ? "Loading…" : "Select a cadence…" },
-              ...cadenceList.map((c) => ({ value: c.id, label: c.name })),
-            ]}
-          />
-        </Field>
-      )}
-      <p className={styles.cadenceHint}>
-        {targetCount} prospect{targetCount === 1 ? "" : "s"} selected. Drafts wait for approval
-        before any email is sent.
-      </p>
-    </Modal>
-  );
+  return { contactIds, accountIds };
 }
 
```

```diff
diff --git a/frontend/src/components/discovery/ResultsPanel.module.css b/frontend/src/components/discovery/ResultsPanel.module.css
index ee90365..9107b1a 100644
--- a/frontend/src/components/discovery/ResultsPanel.module.css
+++ b/frontend/src/components/discovery/ResultsPanel.module.css
@@ -111,10 +111,4 @@
 }
 
-.cadenceHint {
-  margin-top: var(--space-3);
-  color: var(--text-subtle);
-  font-size: var(--text-sm);
-}
-
 .fitCell {
   display: flex;
```

and the builder adds it once the campaign exists — `frontend/src/pages/engagement/CampaignBuilder.tsx`:

```diff
diff --git a/frontend/src/pages/engagement/CampaignBuilder.tsx b/frontend/src/pages/engagement/CampaignBuilder.tsx
index ab29051..4b0fcff 100644
--- a/frontend/src/pages/engagement/CampaignBuilder.tsx
+++ b/frontend/src/pages/engagement/CampaignBuilder.tsx
@@ -1,4 +1,4 @@
 import { useEffect, useMemo, useState } from "react";
-import { Link, useNavigate } from "react-router-dom";
+import { Link, useLocation, useNavigate } from "react-router-dom";
 import { PageHeader } from "@/components/layout/PageHeader";
 import { Button, Card, EmptyState, Field, Icons, Input, Select, Skeleton } from "@/components/ui";
@@ -14,8 +14,16 @@ import styles from "./Engagement.module.css";
  * Start a campaign: its name, the mailbox it sends from, and its steps (spec §9, step 2).
  *
- * Creating it adds nobody and sends nothing. The campaign page that follows is where people are
- * added, first emails drafted and read, and the campaign launched, in that order.
+ * Creating it sends nothing. The campaign page that follows is where people are added, first
+ * emails drafted and read, and the campaign launched, in that order. Arriving from a selection
+ * elsewhere (discovery's "Add to a campaign") carries that selection in router state, and those
+ * people are added the moment the campaign exists: still nothing is drafted or sent.
  */
 
+/** People handed over from another screen: by id, and companies meaning everyone there. */
+interface Handover {
+  contactIds?: string[];
+  accountIds?: string[];
+}
+
 const CUSTOM = "";
 
@@ -29,4 +37,7 @@ export function CampaignBuilder() {
   const toast = useToast();
   const navigate = useNavigate();
+  const handover = (useLocation().state ?? {}) as Handover;
+  const handedContacts = handover.contactIds ?? [];
+  const handedAccounts = handover.accountIds ?? [];
   const mailboxes = useApi<ConnectedMailbox[]>((s) => api.listConnectedMailboxes(false, s), []);
   const templates = useApi<SequenceTemplate[]>((s) => api.listSequenceTemplates(s), []);
@@ -86,5 +97,16 @@ export function CampaignBuilder() {
         timezone_mode: timezoneMode,
       });
-      toast.success("Campaign created", "Now add the people it goes to.");
+      if (handedContacts.length || handedAccounts.length) {
+        try {
+          const added = await api.addCampaignContacts(campaign.id, handedContacts, handedAccounts);
+          toast.success("Campaign created",
+            `${added.added.length} ${added.added.length === 1 ? "person" : "people"} added. Draft their first emails next.`);
+        } catch (err) {
+          toast.error("Campaign created, but nobody was added",
+            err instanceof ApiError ? err.detail : "Add people from the campaign page.");
+        }
+      } else {
+        toast.success("Campaign created", "Now add the people it goes to.");
+      }
       navigate(`/engagement/campaigns/${campaign.id}`, { replace: true });
     } catch (err) {
@@ -124,4 +146,15 @@ export function CampaignBuilder() {
       />
 
+      {(handedContacts.length > 0 || handedAccounts.length > 0) && (
+        <p className={styles.notice} role="status">
+          From your selection,{" "}
+          {[
+            handedContacts.length ? `${handedContacts.length} ${handedContacts.length === 1 ? "person" : "people"}` : "",
+            handedAccounts.length ? `everyone with an email address at ${handedAccounts.length} ${handedAccounts.length === 1 ? "company" : "companies"}` : "",
+          ].filter(Boolean).join(" and ")}{" "}
+          will be added when you create it.
+        </p>
+      )}
+
       <form
         className={styles.form}
```

- [ ] **Step 6: A campaign with no mailbox offers its owner one** — `frontend/src/pages/engagement/CampaignDetailPage.tsx`:

```diff
diff --git a/frontend/src/pages/engagement/CampaignDetailPage.tsx b/frontend/src/pages/engagement/CampaignDetailPage.tsx
index a00af59..c3e742a 100644
--- a/frontend/src/pages/engagement/CampaignDetailPage.tsx
+++ b/frontend/src/pages/engagement/CampaignDetailPage.tsx
@@ -3,6 +3,6 @@ import { Link, useParams, useSearchParams } from "react-router-dom";
 import { PageHeader } from "@/components/layout/PageHeader";
 import {
-  Badge, Button, Card, DataTable, EmptyState, ErrorState, Field, Icons, Input, Modal, Skeleton,
-  TabPanel, Tabs,
+  Badge, Button, Card, DataTable, EmptyState, ErrorState, Field, Icons, Input, Modal, Select,
+  Skeleton, TabPanel, Tabs,
 } from "@/components/ui";
 import type { Column } from "@/components/ui";
@@ -18,4 +18,5 @@ import {
 import { useApi } from "@/hooks/useApi";
 import { useApiClient } from "@/app/AuthContext";
+import { useCurrentUserId } from "@/app/useCurrentUserId";
 import { ApiError } from "@/lib/api";
 import type {
@@ -55,4 +56,5 @@ export function CampaignDetailPage() {
   );
   const mailboxes = useApi<ConnectedMailbox[]>((s) => api.listConnectedMailboxes(false, s), []);
+  const currentUserId = useCurrentUserId();
   const [search] = useSearchParams();
   // A link can ask for a tab ("Review the intro" from the reply desk); otherwise open where the work is.
@@ -146,5 +148,5 @@ export function CampaignDetailPage() {
               <span>Paused because {reasonText(c.pause_reason)}</span>
             )}
-            <span>Sends from {mailbox?.email ?? "your mailbox"}</span>
+            <span>{mailbox ? `Sends from ${mailbox.email}` : c.mailbox_connection_id ? "Sends from your mailbox" : "No sending mailbox yet"}</span>
           </span>
         }
@@ -169,9 +171,11 @@ export function CampaignDetailPage() {
         </p>
       )}
-      {mailbox && mailbox.status !== "connected" && (
-        <p className={styles.notice} role="alert">
-          {mailbox.email} needs reconnecting before this campaign can send.{" "}
-          <Link to="/mailboxes" className={styles.inlineLink}>Reconnect it on My mailboxes</Link>.
-        </p>
+      {(!c.mailbox_connection_id || (mailbox && mailbox.status !== "connected")) && (
+        <MailboxChooser
+          campaignId={c.id} current={mailbox ?? null}
+          isOwner={c.owner_user_id === currentUserId}
+          mine={(mailboxes.data ?? []).filter((m) => m.mine && m.status === "connected")}
+          onChosen={refresh}
+        />
       )}
 
@@ -497,2 +501,61 @@ function StepsPanel({ campaign, editable, onSaved }: {
 
 export default CampaignDetailPage;
+
+/**
+ * A campaign with nowhere to send from (spec §13, D14): moved from the old engine before its owner
+ * connected a mailbox, or its mailbox was disconnected. Choosing one resumes everyone who was
+ * waiting for it. A campaign sends from its OWNER's mailbox, so only the owner is offered the choice.
+ */
+function MailboxChooser({ campaignId, current, isOwner, mine, onChosen }: {
+  campaignId: string;
+  current: ConnectedMailbox | null;
+  isOwner: boolean;
+  mine: ConnectedMailbox[];
+  onChosen: () => void;
+}) {
+  const api = useApiClient();
+  const toast = useToast();
+  const [choice, setChoice] = useState("");
+  const [busy, setBusy] = useState(false);
+  const picked = choice || mine[0]?.id || "";
+
+  async function choose() {
+    setBusy(true);
+    try {
+      await api.setCampaignMailbox(campaignId, picked);
+      toast.success("Mailbox set", "Everyone waiting for it is back in sequence.");
+      onChosen();
+    } catch (err) {
+      toast.error("That didn't work", err instanceof ApiError ? err.detail : "Please try again.");
+    } finally {
+      setBusy(false);
+    }
+  }
+
+  const problem = current
+    ? `${current.email} needs reconnecting before this campaign can send.`
+    : "This campaign has no mailbox to send from, so nothing is sending.";
+  return (
+    <div className={styles.notice} role="alert">
+      <p className={styles.bodyText}>{problem}</p>
+      {!isOwner ? (
+        <p className={styles.muted}>It sends from its owner's mailbox, so the owner chooses one.</p>
+      ) : mine.length === 0 ? (
+        <p className={styles.muted}>
+          <Link to="/mailboxes" className={styles.inlineLink}>Connect a mailbox on My mailboxes</Link>,
+          then choose it here.
+        </p>
+      ) : (
+        <div className={styles.laterRow}>
+          <Field label="Send from">
+            <Select value={picked} onChange={(e) => setChoice(e.target.value)}
+              options={mine.map((m) => ({ value: m.id, label: m.email }))} />
+          </Field>
+          <Button onClick={choose} loading={busy} disabled={busy || !picked}>
+            Send from this mailbox
+          </Button>
+        </div>
+      )}
+    </div>
+  );
+}
```

- [ ] **Step 7: Earlier campaigns, read-only** — `frontend/src/pages/engagement/CampaignsPage.tsx`:

```diff
diff --git a/frontend/src/pages/engagement/CampaignsPage.tsx b/frontend/src/pages/engagement/CampaignsPage.tsx
index e7f9d37..f7255cf 100644
--- a/frontend/src/pages/engagement/CampaignsPage.tsx
+++ b/frontend/src/pages/engagement/CampaignsPage.tsx
@@ -8,5 +8,6 @@ import { useApi } from "@/hooks/useApi";
 import { useApiClient } from "@/app/AuthContext";
 import { useEngagementStatus } from "@/app/EngagementContext";
-import type { ConnectedMailbox, EngagementCampaign } from "@/lib/types";
+import { campaignTone } from "@/lib/display";
+import type { ConnectedMailbox, EngagementCampaign, LegacyCampaign } from "@/lib/types";
 import styles from "./Engagement.module.css";
 
@@ -143,7 +144,64 @@ export function EngagementCampaignsPage() {
         />
       )}
+
+      <LegacyCampaigns team={team} />
     </div>
   );
 }
 
+const LEGACY_STATUS: Record<LegacyCampaign["status"], string> = {
+  completed: "Completed",
+  cancelled: "Cancelled",
+  failed: "Failed",
+};
+
+/**
+ * Campaigns the previous engine finished, kept as they were (spec §13). Read-only: no row opens,
+ * nothing here sends, and it is not shown at all for a workspace that never used that engine. An
+ * error hides it too, because it is history beside the page rather than the page itself.
+ */
+function LegacyCampaigns({ team }: { team: boolean }) {
+  const api = useApiClient();
+  const legacy = useApi<LegacyCampaign[]>((s) => api.legacyCampaigns(team, s), [team]);
+  if (!legacy.data || legacy.data.length === 0) return null;
+
+  const columns: Column<LegacyCampaign>[] = [
+    { key: "name", header: "Campaign", render: (c) => c.name },
+    {
+      key: "status",
+      header: "Status",
+      render: (c) => <Badge tone={campaignTone(c.status)}>{LEGACY_STATUS[c.status] ?? c.status}</Badge>,
+    },
+    {
+      key: "sent",
+      header: "Sent",
+      align: "right",
+      render: (c) => <span className={styles.num}>{c.sent} of {c.targets}</span>,
+    },
+    {
+      key: "created",
+      header: "Started",
+      hideOnMobile: true,
+      render: (c) => whenDay(c.created_at),
+    },
+  ];
+
+  return (
+    <section className={styles.section} aria-labelledby="legacy-title">
+      <div>
+        <h2 id="legacy-title" className={styles.sectionTitle}>Earlier campaigns</h2>
+        <p className={styles.muted}>
+          Finished before campaigns moved to your own mailbox. Kept as they were; nothing here sends.
+        </p>
+      </div>
+      <DataTable
+        columns={columns}
+        rows={legacy.data}
+        getRowKey={(c) => c.id}
+        caption="Earlier campaigns"
+      />
+    </section>
+  );
+}
+
 export default EngagementCampaignsPage;
```

- [ ] **Step 8: Copy that named the old engine** — `ListsPage.tsx`, `CallsPage.tsx`, `SettingsPage.tsx`:

```diff
diff --git a/frontend/src/pages/ListsPage.tsx b/frontend/src/pages/ListsPage.tsx
index 56afa51..849f72d 100644
--- a/frontend/src/pages/ListsPage.tsx
+++ b/frontend/src/pages/ListsPage.tsx
@@ -316,6 +316,7 @@ function SavedLists({ refreshKey }: { refreshKey: number }) {
       <div className={styles.savedHead}>
         <h3 className={styles.savedTitle}>Saved lists</h3>
-        <Button variant="ghost" size="sm" onClick={() => navigate("/campaigns")}>
-          Launch a campaign
+        {/* A new campaign's People tab filters by saved list, so a segment is one choice away. */}
+        <Button variant="ghost" size="sm" onClick={() => navigate("/engagement/campaigns/new")}>
+          Start a campaign
         </Button>
       </div>
```

```diff
diff --git a/frontend/src/pages/CallsPage.tsx b/frontend/src/pages/CallsPage.tsx
index 28dfc94..d1b8971 100644
--- a/frontend/src/pages/CallsPage.tsx
+++ b/frontend/src/pages/CallsPage.tsx
@@ -86,5 +86,5 @@ export function CallsPage() {
                   icon={<Icons.PhoneIcon />}
                   title="No calls queued"
-                  description="Calls appear here from cadences with a call step, or from the Call button on a contact."
+                  description="Calls appear here from campaigns with a call step, or from the Call button on a contact."
                 />
               }
```

```diff
diff --git a/frontend/src/pages/SettingsPage.tsx b/frontend/src/pages/SettingsPage.tsx
index dcb1550..172dbf2 100644
--- a/frontend/src/pages/SettingsPage.tsx
+++ b/frontend/src/pages/SettingsPage.tsx
@@ -111,5 +111,5 @@ export function SettingsPage() {
                     </span>
                     <span className={styles.controlHint}>
-                      When on, the worker re-scores stale accounts and advances cadences each tick.
+                      When on, the worker re-scores stale accounts and finds new ones each tick.
                       When off, everything waits for a manual run.
                     </span>
```

- [ ] **Step 9: Build** — `npm run typecheck && npm run build` in `frontend/` with the repository mounted (the bundle lands in `nexus/web/dist`) — expected: no errors.

---

### Task 6: The operator docs

- [ ] **Step 1:** `docs/engagement/cutover-runbook.md`:

```markdown
# Cutover runbook: old Campaigns and Cadences to the engagement engine (Release B)

Spec §13 and §15. This release removes the old engines' code: after it deploys, nothing advances an
old cadence enrollment or sends an old campaign. Run the migration **straight after the deploy**, so
the gap in which in-flight sequences wait is minutes, not days. Nothing is lost in the gap: moved
enrollments keep their due times, and anything overdue goes out on the next tick, in order.

## Before the deploy

1. **Mailbox apps configured in this environment** ([oauth-environments.md](oauth-environments.md)):
   Public base URL, client ids, and the two secrets in Provider keys.
2. **SDRs connect their mailboxes** on My mailboxes (live since Release A). A campaign whose owner has
   no connected mailbox is moved **paused**, with a banner, and nothing of it sends until one is
   chosen (D14).
3. **Backup**: take the database backup (`scripts/backup_db.sh`) and tag the running release.
4. **Heads**: `alembic heads` on the release commit returns exactly one head
   (`0058_engagement_crm_log`). Another branch has planned a `0057_web_cache`: whichever merges
   second renumbers before merging.

## Deploy

1. Deploy the release. `bootstrap_db.py` runs `alembic upgrade head`, then `apply_rls.py` enrols the
   new tables. The engagement engine is **on by default** from this release
   (`engagement_campaigns_enabled`); the old Campaigns and Cadences pages now forward to the new ones.
2. **Dry run, and read it with the owner.** Inside the app container:

   ```bash
   python scripts/migrate_engagement.py --dry-run
   ```

   Per workspace it prints: sequence templates to create, sequences to move and how many will pause
   for lack of a mailbox, opening emails moving to review, old emails found and not found in Sent
   folders (not found means that person's next follow-up starts a new thread), call tasks linked,
   and finished campaigns kept read-only. It writes nothing: it is the real run, rolled back.
   `--json` prints the same for a spreadsheet; `--tenant <id>` limits it to one workspace.
3. **Run it:**

   ```bash
   python scripts/migrate_engagement.py
   ```

   One transaction per workspace: a workspace that fails is reported on stderr, left untouched, and
   the rest still move. The exit code is non-zero if any failed.
4. **Verify the counts** against the dry run, then open Campaigns as an SDR whose sequence moved:
   the campaign is there, its people are at the same step, and **Earlier campaigns** lists the
   finished ones read-only.

## After

- **Re-run the script whenever SDRs connect mailboxes** in the following days. It is idempotent:
  it moves nothing twice, attaches the owner's newly connected mailbox to a campaign that had none,
  resumes its people, and imports the old emails' history (thread recovery from the Sent folder)
  that had nowhere to live the first time. The **Send from this mailbox** control on a paused
  campaign attaches a mailbox too, but only the script imports history.
- Old tables (`campaigns`, `campaign_targets`, `cadences`, `cadence_steps`, `cadence_enrollments`,
  `cadence_touches`) stay as read-only history. Nothing writes them.
- Queued `run_campaign` / `advance_cadences` jobs left over from the old release are logged as
  unknown and dropped, not retried.

## Rollback

Code: redeploy the previous tag. The old engine resumes where it stopped for anything the migration
did not touch; moved enrollments exist in both engines, so **also switch
`engagement_campaigns_enabled` off** (Control plane → Runtime settings) to stop the new engine sending
the same people. Data: nothing was deleted and the migration only added rows, so the backup is for
disaster, not for rollback.
```

- [ ] **Step 2:** `docs/engagement/oauth-environments.md`, linked from the first lines of `setup-google.md` and `setup-microsoft.md`:

```markdown
# Mailbox OAuth: redirect URIs and scopes for every environment

One Google OAuth client and one Microsoft app registration serve every environment. Register
**every row** below in both; each environment then sends the redirect built from its own **Runtime
settings → Mailboxes & engagement → Public base URL**, which must match one registered row exactly
(no trailing slash).

The path is fixed by `nexus/engagement/config.py` and must not change once registered:
`/api/engagement/mailboxes/oauth/{google|microsoft}/callback`.

| Environment | Public base URL | Gmail redirect URI | Outlook / Microsoft 365 redirect URI |
|---|---|---|---|
| Production | `https://gtm.infojoy.com` | `https://gtm.infojoy.com/api/engagement/mailboxes/oauth/google/callback` | `https://gtm.infojoy.com/api/engagement/mailboxes/oauth/microsoft/callback` |
| Staging | `https://staging-gtm.infojoy.com` | `https://staging-gtm.infojoy.com/api/engagement/mailboxes/oauth/google/callback` | `https://staging-gtm.infojoy.com/api/engagement/mailboxes/oauth/microsoft/callback` |
| Staging (Azure host) | `https://gtm-staging-app.mangowater-46a1ec4f.eastus2.azurecontainerapps.io` | `https://gtm-staging-app.mangowater-46a1ec4f.eastus2.azurecontainerapps.io/api/engagement/mailboxes/oauth/google/callback` | `https://gtm-staging-app.mangowater-46a1ec4f.eastus2.azurecontainerapps.io/api/engagement/mailboxes/oauth/microsoft/callback` |
| Local | `http://localhost:8099` | `http://localhost:8099/api/engagement/mailboxes/oauth/google/callback` | `http://localhost:8099/api/engagement/mailboxes/oauth/microsoft/callback` |
| Local (Caddy) | `https://localhost` | `https://localhost/api/engagement/mailboxes/oauth/google/callback` | `https://localhost/api/engagement/mailboxes/oauth/microsoft/callback` |

Google and Microsoft both accept plain `http` only for `localhost`. Staging has two rows because both
hosts reach it; register both and set the Public base URL to the one people actually open.

## Scopes

| Provider | Scopes | Notes |
|---|---|---|
| Google | `openid`, `email`, `https://www.googleapis.com/auth/gmail.readonly`, `https://www.googleapis.com/auth/gmail.compose` | Both Gmail scopes are **restricted**. In "Testing" only listed test users (up to 100) can connect; production needs Google verification and a CASA assessment. |
| Microsoft Graph (delegated) | `openid`, `email`, `offline_access`, `User.Read`, `Mail.ReadWrite`, `Mail.Send` | With the tenant setting `common`, the registration must accept "any organizational directory and personal Microsoft accounts"; otherwise set **Microsoft tenant** to your directory id. |

## Where each value goes

| Value | Where |
|---|---|
| Public base URL, Google client id, Microsoft client id, Microsoft tenant, Gmail notification topic, Gmail push service account | Runtime settings → Mailboxes & engagement, per environment |
| Google client secret, Microsoft client secret | Superadmin → Provider keys: `google_oauth`, `microsoft_oauth` |

Nobody pastes a secret into chat, a ticket or a file in the repo.

## Reply pickup

Staging and production: point the Gmail Pub/Sub push subscription at
`<public base URL>/api/engagement/webhooks/gmail` with authentication on, the audience set to that
same URL, and the service account entered in Runtime settings. Outlook subscribes itself to
`<public base URL>/api/engagement/webhooks/graph`. Neither can reach `localhost`: locally, replies
arrive through the periodic mailbox sync instead, a few minutes behind.
```

---

### Task 7: The tests

- [ ] **Step 1: Create** `tests/test_engagement_cutover.py`:

```python
"""The cutover from the old Campaigns and Cadences engines (spec §13, D13, D14).

Real old-engine rows through `tenant_session`, migrated by the real script's core, with the Sent
folder served by the provider double the engagement suites use (D21): it answers `search_sent` for
the emails it knows and nothing else, as a mailbox would.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from nexus.core.config import get_settings
from tests.conftest import auth, signup, tenant_session
from tests.test_engagement_sending import SentFolder
from tests.test_engagement_sequences import NOW, _world


class SentHistory(SentFolder):
    """A mailbox whose Sent folder holds some of the old engine's emails."""

    def __init__(self):
        super().__init__()
        self.known: dict[tuple[str, str], object] = {}
        self.searched: list[tuple[str, str]] = []

    def remember(self, to: str, subject: str, thread_id: str) -> None:
        from nexus.engagement.mailboxes.provider import SentRef

        self.known[(to, subject)] = SentRef(provider_message_id=f"pm-{len(self.known) + 1}",
                                            provider_thread_id=thread_id)

    async def search_sent(self, *, to: str, subject: str, around):
        self.searched.append((to, subject))
        return self.known.get((to, subject))


@pytest.fixture
def history():
    from nexus.engagement.mailboxes import registry

    box = SentHistory()
    registry.set_provider_factory(lambda _connection: box)
    yield box
    registry.set_provider_factory(None)


# ---- pure conversions ----------------------------------------------------------------------------

def test_calendar_waits_become_business_days():
    from nexus.engagement.cutover.migrate import business_days

    assert [business_days(d) for d in (0, 1, 2, 3, 7, 14, 200)] == [0, 1, 1, 2, 5, 10, 60]


# ---- a workspace mid-flight on the old engine ----------------------------------------------------

async def _old_engine(slug: str):
    """Sam (a connected mailbox) runs a three-step cadence campaign: Jane0 is on step 2 with two
    emails sent, Jane1 on step 1. Kim (no mailbox) runs one with Jane2 on step 1. A third campaign
    awaits approval with a draft for Jane3, and a fourth finished long ago."""
    from nexus.models.account import Contact
    from nexus.models.cadence import Cadence, CadenceEnrollment, CadenceStep, CadenceTouch
    from nexus.models.calling import CallTask
    from nexus.models.campaign import Campaign, CampaignTarget
    from nexus.models.identity import Membership, User
    from nexus.models.workflow import ProspectList

    tid, sam_id, mailbox_id, contacts = await _world(slug, contacts=4)
    async with tenant_session(tid) as ts:
        kim = User(email=f"kim@{slug}.com", full_name="Kim Rep", password_hash="x")
        ts.session.add(kim)
        await ts.session.flush()
        ts.add(Membership(user_id=kim.id, role="rep"))
        cadence = Cadence(name="Three touches", description="Old cadence",
                          created_by_user_id=sam_id)
        ts.add(cadence)
        await ts.flush()
        for i, (days, angle) in enumerate(((0, "Open"), (3, "Bump"), (7, "Break-up"))):
            ts.add(CadenceStep(cadence_id=cadence.id, step_index=i, delay_days=days, angle=angle))
        saved = ProspectList(name="Q3 list", owner_user_id=sam_id)
        ts.add(saved)
        await ts.flush()

        def campaign(name, status, owner, with_cadence=True):
            row = Campaign(name=name, list_id=saved.id, status=status, created_by_user_id=owner,
                           cadence_id=cadence.id if with_cadence else None)
            ts.add(row)
            return row

        running = campaign("Q3 outbound", "sending", sam_id)
        kims = campaign("Kim's outbound", "sending", kim.id)
        waiting = campaign("Q4 draft", "awaiting_approval", sam_id, with_cadence=False)
        campaign("Q2 done", "completed", sam_id)
        await ts.flush()
        account_id = (await ts.get(Contact, contacts[0])).account_id

        def enrolled(camp, contact_id, step, due):
            row = CadenceEnrollment(campaign_id=camp.id, account_id=account_id,
                                    contact_id=contact_id, cadence_id=cadence.id,
                                    current_step_index=step, status="active",
                                    next_touch_at=due, started_at=NOW - timedelta(days=10))
            ts.add(row)
            return row

        jane0 = enrolled(running, contacts[0], 2, NOW + timedelta(days=2))
        enrolled(running, contacts[1], 1, NOW + timedelta(days=1))
        enrolled(kims, contacts[2], 1, NOW + timedelta(days=1))
        await ts.flush()
        for step, subject in ((0, "Quick question"), (1, "Re: Quick question")):
            ts.add(CadenceTouch(enrollment_id=jane0.id, step_index=step, status="sent",
                                draft={"subject": subject, "body": f"Touch {step}"},
                                sent_at=NOW - timedelta(days=9 - step * 3)))
        ts.add(CallTask(account_id=account_id, contact_id=contacts[0], reason="Cadence call",
                        owner_user_id=sam_id, cadence_enrollment_id=jane0.id,
                        cadence_step_index=2))
        ts.add(CampaignTarget(campaign_id=waiting.id, account_id=account_id, status="drafted",
                              draft={"contact_id": contacts[3], "subject": "Hello Jane3",
                                     "body": "An opening email."}))
        await ts.flush()
        return tid, sam_id, kim.id, contacts, running.id, jane0.id


async def _counts(tid):
    from nexus.models.engagement import (
        EngagementCampaign,
        EngagementEnrollment,
        EngagementMessage,
        SequenceTemplate,
    )

    async with tenant_session(tid) as ts:
        return tuple([len(await ts.list(m)) for m in (SequenceTemplate, EngagementCampaign,
                                                       EngagementEnrollment, EngagementMessage)])


async def test_the_dry_run_reports_what_would_move_and_writes_nothing(history):
    from nexus.engagement.cutover.migrate import migrate, render

    tid, *_ = await _old_engine("cutdry")
    history.remember("jane0@acme.io", "Quick question", "thread-q")
    async with tenant_session(tid) as ts:
        report = await migrate(ts, dry_run=True, now=NOW)
    assert report.templates_created == 1
    assert report.totals == {"sequences_moved": 3, "paused_for_mailbox": 1, "drafts_to_review": 1,
                             "emails_found": 1, "emails_not_found": 1}
    assert report.call_tasks_linked == 1 and report.legacy_campaigns == 1
    text = render(report)
    assert "DRY RUN, nothing written" in text and "1 paused until a mailbox is connected" in text
    assert await _counts(tid) == (0, 0, 0, 0), "a dry run writes nothing"


async def test_the_real_run_moves_sequences_at_the_same_step_and_twice_moves_nothing(history):
    from nexus.engagement.cutover.migrate import migrate
    from nexus.models.calling import CallTask
    from nexus.models.engagement import (
        EngagementCampaign,
        EngagementEnrollment,
        EngagementMessage,
        EngagementThread,
        SequenceTemplate,
    )

    tid, sam_id, kim_id, contacts, running_id, jane0_old = await _old_engine("cutreal")
    history.remember("jane0@acme.io", "Quick question", "thread-q")
    async with tenant_session(tid) as ts:
        await migrate(ts, dry_run=False, now=NOW)

    async with tenant_session(tid) as ts:
        template = await ts.first(SequenceTemplate)
        assert [s["delay_business_days"] for s in template.steps] == [0, 2, 5]
        running = await ts.first(EngagementCampaign,
                                 EngagementCampaign.legacy_campaign_id == running_id)
        assert (running.status, running.owner_user_id) == ("active", sam_id)
        jane0 = await ts.first(EngagementEnrollment,
                               EngagementEnrollment.legacy_enrollment_id == jane0_old)
        # The same step, the same due time: nothing is re-sent and nothing is skipped.
        assert (jane0.status, jane0.current_step_index) == ("active", 2)
        assert jane0.next_action_at.replace(tzinfo=None) == (NOW + timedelta(days=2)).replace(
            tzinfo=None)
        sent = sorted(await ts.list(EngagementMessage,
                                    EngagementMessage.enrollment_id == jane0.id),
                      key=lambda m: m.step_index)
        assert [(m.step_index, m.status, m.subject) for m in sent] == [
            (0, "sent", "Quick question"), (1, "sent", "Re: Quick question")]
        thread = await ts.get(EngagementThread, jane0.current_thread_id)
        assert thread.provider_thread_id == "thread-q", "replies will find the found thread"
        assert sent[0].thread_id == thread.id and sent[1].thread_id is None
        call = await ts.first(CallTask, CallTask.cadence_enrollment_id == jane0_old)
        assert call.engagement_enrollment_id == jane0.id

        kims = await ts.first(EngagementCampaign, EngagementCampaign.owner_user_id == kim_id)
        assert (kims.status, kims.pause_reason, kims.mailbox_connection_id) == (
            "paused", "mailbox_disconnected", None)
        waiting = await ts.first(EngagementCampaign, EngagementCampaign.name == "Q4 draft")
        drafts = await ts.list(EngagementMessage, EngagementMessage.status == "draft")
        assert waiting.status == "reviewing" and [d.subject for d in drafts] == ["Hello Jane3"]
        assert await ts.first(EngagementCampaign, EngagementCampaign.name == "Q2 done") is None

    before = await _counts(tid)
    async with tenant_session(tid) as ts:
        again = await migrate(ts, dry_run=False, now=NOW)
    assert again.templates_created == 0 and again.totals["sequences_moved"] == 0
    assert await _counts(tid) == before, "a second run moves nothing twice"


async def test_a_sequence_waiting_for_a_mailbox_moves_on_when_one_is_connected(history):
    from nexus.engagement.cutover.migrate import migrate
    from nexus.models.engagement import EngagementCampaign, EngagementEnrollment, MailboxConnection

    tid, _sam, kim_id, *_ = await _old_engine("cutlater")
    async with tenant_session(tid) as ts:
        await migrate(ts, dry_run=False, now=NOW)
    async with tenant_session(tid) as ts:
        ts.add(MailboxConnection(owner_user_id=kim_id, provider="google", email="kim@x.com",
                                 status="connected", timezone="UTC"))
    async with tenant_session(tid) as ts:
        await migrate(ts, dry_run=False, now=NOW)
    async with tenant_session(tid) as ts:
        kims = await ts.first(EngagementCampaign, EngagementCampaign.owner_user_id == kim_id)
        person = await ts.first(EngagementEnrollment,
                                EngagementEnrollment.campaign_id == kims.id)
        assert (kims.status, kims.mailbox_connection_id is not None) == ("active", True)
        assert (person.status, person.status_reason) == ("active", None)


async def test_a_campaign_can_be_given_its_owners_mailbox_from_the_screen(client, monkeypatch):
    from nexus.core.db import utcnow
    from nexus.models.engagement import (
        EngagementCampaign,
        EngagementEnrollment,
        MailboxConnection,
    )
    from nexus.models.account import Account, Contact
    from tests.conftest import principal_from_token

    monkeypatch.setattr(get_settings(), "engagement_campaigns_enabled", True)
    token = await signup(client, slug="cutbox", email="sam@cutbox.com", company="C")
    me = principal_from_token(token)
    async with tenant_session(me.tenant_id) as ts:
        account = Account(name="Acme", domain="acme.io")
        ts.add(account)
        await ts.flush()
        contact = Contact(account_id=account.id, full_name="Jane", email="jane@acme.io")
        mine = MailboxConnection(owner_user_id=me.user_id, provider="google",
                                 email="sam@cutbox.com", status="connected", timezone="UTC")
        ts.add(contact)
        ts.add(mine)
        await ts.flush()
        campaign = EngagementCampaign(name="Moved", owner_user_id=me.user_id, status="paused",
                                      pause_reason="mailbox_disconnected")
        ts.add(campaign)
        await ts.flush()
        ts.add(EngagementEnrollment(campaign_id=campaign.id, contact_id=contact.id,
                                    account_id=account.id, status="paused",
                                    status_reason="mailbox_disconnected",
                                    next_action_at=utcnow()))
        ids = (campaign.id, mine.id)

    r = await client.put(f"/api/engagement/campaigns/{ids[0]}/mailbox", headers=auth(token),
                         json={"mailbox_id": ids[1]})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "active"
    async with tenant_session(me.tenant_id) as ts:
        person = await ts.first(EngagementEnrollment)
        assert (person.status, person.mailbox_connection_id) == ("active", ids[1])

    bad = await client.put(f"/api/engagement/campaigns/{ids[0]}/mailbox", headers=auth(token),
                           json={"mailbox_id": "nope"})
    assert bad.status_code == 409


async def test_finished_old_campaigns_are_listed_read_only(client, monkeypatch):
    from nexus.models.campaign import Campaign, CampaignTarget
    from nexus.models.account import Account
    from nexus.models.workflow import ProspectList
    from tests.conftest import principal_from_token

    monkeypatch.setattr(get_settings(), "engagement_campaigns_enabled", True)
    token = await signup(client, slug="cutlegacy", email="sam@cutlegacy.com", company="L")
    me = principal_from_token(token)
    async with tenant_session(me.tenant_id) as ts:
        saved = ProspectList(name="Old", owner_user_id=me.user_id)
        account = Account(name="Acme", domain="acme.io")
        ts.add(saved)
        ts.add(account)
        await ts.flush()
        done = Campaign(name="Q2 done", list_id=saved.id, status="completed",
                        created_by_user_id=me.user_id)
        ts.add(done)
        ts.add(Campaign(name="Still running", list_id=saved.id, status="sending",
                        created_by_user_id=me.user_id))
        await ts.flush()
        for status_ in ("sent", "sent", "skipped"):
            ts.add(CampaignTarget(campaign_id=done.id, account_id=account.id, status=status_))

    r = await client.get("/api/engagement/legacy-campaigns", headers=auth(token))
    assert r.status_code == 200, r.text
    assert [(c["name"], c["status"], c["targets"], c["sent"]) for c in r.json()] == [
        ("Q2 done", "completed", 3, 2)]


# ---- the old engine stays gone -------------------------------------------------------------------

def test_old_engine_is_gone():
    """D13: the old code paths are removed; only their tables remain, as history. An import of the
    old packages coming back would resurrect a second engine sending to the same people."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "nexus"
    assert not (root / "campaigns").exists() and not (root / "cadences").exists()
    offenders = [str(p.relative_to(root)) for p in root.rglob("*.py")
                 if "nexus.campaigns" in (text := p.read_text(encoding="utf-8"))
                 or "nexus.cadences" in text]
    assert offenders == [], f"imports of the removed engine in {offenders}"
```

- [ ] **Step 2: Delete** the old engine's tests, whose code is gone:

```bash
git rm tests/test_cadence_engine.py tests/test_campaign_engine.py tests/test_campaign_sourcing.py tests/test_launch_from_selection.py
```

- [ ] **Step 3: Rewrite what referred to it.** Tiering reads engagement enrollments — `tests/test_refresh_tiering.py`:

```diff
diff --git a/tests/test_refresh_tiering.py b/tests/test_refresh_tiering.py
index fd9ac80..e8c1ac3 100644
--- a/tests/test_refresh_tiering.py
+++ b/tests/test_refresh_tiering.py
@@ -16,4 +16,6 @@ from __future__ import annotations
 from datetime import datetime, timedelta, timezone
 
+import pytest
+
 from tests.conftest import make_tenant
 
@@ -84,11 +86,13 @@ async def test_an_old_signal_does_not_keep_it_hot(fresh_db):
 
 
-async def test_an_account_in_an_active_cadence_stays_hot(fresh_db):
-    """A rep is emailing this company today. Whatever the signal history says, it must not drop
-    to a three-day crawl cycle."""
+@pytest.mark.parametrize("status, tier", [("active", "hot"), ("snoozed", "hot"),
+                                          ("awaiting_review", "hot"), ("stopped", "cold")])
+async def test_an_account_in_a_live_sequence_stays_hot(fresh_db, status, tier):
+    """A rep is emailing this company, is about to, or promised to write back. Whatever the signal
+    history says, it must not drop to a three-day crawl cycle. A stopped sequence is not work."""
     from nexus.ingestion import tiering
-    from nexus.models.cadence import Cadence, CadenceEnrollment, ENROLL_ACTIVE
-    from nexus.models.campaign import Campaign
-    from nexus.models.workflow import ProspectList
+    from nexus.models.account import Contact
+    from nexus.models.engagement import EngagementCampaign, EngagementEnrollment
+    from nexus.models.identity import User
     from nexus.workers.tasks import tenant_session
 
@@ -96,18 +100,15 @@ async def test_an_account_in_an_active_cadence_stays_hot(fresh_db):
     async with tenant_session(tid) as ts:
         acct = await _account(ts)
-        lst = ProspectList(name="Targets")
-        cad = Cadence(name="Outbound")
-        ts.add_all([lst, cad])
-        await ts.flush()
-        camp = Campaign(name="Q3 push", list_id=lst.id)
-        ts.add(camp)
+        rep = User(email=f"rep-{status}@tier.io", full_name="Rep", password_hash="x")
+        ts.session.add(rep)
+        await ts.session.flush()
+        person = Contact(account_id=acct.id, full_name="Jane", email="jane@tier.io")
+        campaign = EngagementCampaign(name="Q3 push", owner_user_id=rep.id, status="active")
+        ts.add_all([person, campaign])
         await ts.flush()
-        ts.add(CadenceEnrollment(
-            cadence_id=cad.id, campaign_id=camp.id, account_id=acct.id, status=ENROLL_ACTIVE,
-            next_touch_at=datetime.now(timezone.utc) + timedelta(days=1),
-            started_at=datetime.now(timezone.utc),
-        ))
+        ts.add(EngagementEnrollment(campaign_id=campaign.id, contact_id=person.id,
+                                    account_id=acct.id, status=status))
         await ts.flush()
-        assert await tiering.classify(ts, acct, new_signals=[]) == tiering.HOT
+        assert await tiering.classify(ts, acct, new_signals=[]) == tier
 
 
```

The orchestrator creates a template — `tests/test_cold_calling.py`:

```diff
diff --git a/tests/test_cold_calling.py b/tests/test_cold_calling.py
index 1bdd565..552f534 100644
--- a/tests/test_cold_calling.py
+++ b/tests/test_cold_calling.py
@@ -119,6 +119,6 @@ async def _run_setup_cadence(ts, goal_input):
 
 
-async def test_orchestrator_sets_up_call_cadence_from_steps():
-    from nexus.cadences.service import get_cadence_service
+async def test_orchestrator_sets_up_a_call_sequence_template_from_steps():
+    from nexus.models.engagement import SequenceTemplate
 
     tid = await make_tenant()
@@ -130,17 +130,19 @@ async def test_orchestrator_sets_up_call_cadence_from_steps():
         })
         assert out["name"] == "Cold Call Seq"
-        steps = await get_cadence_service().list_steps(ts, out["cadence_id"])
-        assert [s.channel for s in steps] == ["email", "call"]
+        template = await ts.get(SequenceTemplate, out["template_id"])
+        assert [s["channel"] for s in template.steps] == ["email", "call"]
+        # Calendar days from an old-style brief become business days.
+        assert [s["delay_business_days"] for s in template.steps] == [0, 1]
 
 
 async def test_orchestrator_defaults_to_cold_calling_3touch():
     """No steps given -> the orchestrator picks a sensible email -> call -> email sequence."""
-    from nexus.cadences.service import get_cadence_service
+    from nexus.models.engagement import SequenceTemplate
 
     tid = await make_tenant()
     async with tenant_session(tid) as ts:
         out = await _run_setup_cadence(ts, {})
-        steps = await get_cadence_service().list_steps(ts, out["cadence_id"])
-        assert [s.channel for s in steps] == ["email", "call", "email"]
+        template = await ts.get(SequenceTemplate, out["template_id"])
+        assert [s["channel"] for s in template.steps] == ["email", "call", "email"]
 
 
```

The heartbeat no longer enqueues `advance_cadences`, and pins the engine switch rather than relying on its default — `tests/test_continuous_automation.py`:

```diff
diff --git a/tests/test_continuous_automation.py b/tests/test_continuous_automation.py
index 7f87a76..f6a981b 100644
--- a/tests/test_continuous_automation.py
+++ b/tests/test_continuous_automation.py
@@ -187,4 +187,6 @@ from nexus.workers.scheduler import _enqueue_due, run_scheduler
 async def test_enqueue_due_enqueues_both_drivers_when_enabled(monkeypatch):
     monkeypatch.setattr(get_settings(), "automation_enabled", True)
+    # The engagement engine's drivers ride their own switch; pinned off to keep this about automation.
+    monkeypatch.setattr(get_settings(), "engagement_campaigns_enabled", False)
     q = InMemoryTaskQueue()
     count = await _enqueue_due(q)
@@ -194,8 +196,8 @@ async def test_enqueue_due_enqueues_both_drivers_when_enabled(monkeypatch):
     # concerns rather than per-workspace opt-ins. +1 for refresh_mailbox_tokens, which keeps SDR
     # mailbox status honest whether or not automation is on.
-    assert count == 15
+    assert count == 14
     jobs = await _drain(q)
     assert {j.name for j in jobs} == {
-        "advance_cadences", "refresh_due_accounts", "send_daily_digests",
+        "refresh_due_accounts", "send_daily_digests",
         "discover_icp_accounts", "rollup_usage", "roll_billing_periods", "dunning_sweep",
         "billing_reconcile", "expire_trials", "alert_digests", "backfill_companies", "crawl_companies",
@@ -216,4 +218,5 @@ async def test_enqueue_due_noop_when_disabled(monkeypatch):
     switched automation on."""
     monkeypatch.setattr(get_settings(), "automation_enabled", False)
+    monkeypatch.setattr(get_settings(), "engagement_campaigns_enabled", False)
     q = InMemoryTaskQueue()
     count = await _enqueue_due(q)
```

The same in the CRM heartbeat tests, plus the engagement CRM log's two switches — `tests/test_crm_auto_sync.py`:

```diff
diff --git a/tests/test_crm_auto_sync.py b/tests/test_crm_auto_sync.py
index 8cff6b2..eeba932 100644
--- a/tests/test_crm_auto_sync.py
+++ b/tests/test_crm_auto_sync.py
@@ -355,4 +355,6 @@ async def test_scheduler_enqueues_crm_sweep_when_crm_sync_enabled(monkeypatch):
     monkeypatch.setattr(get_settings(), "automation_enabled", False)
     monkeypatch.setattr(get_settings(), "crm_sync_enabled", True)
+    # The engagement engine's drivers ride their own switch; pinned off to keep this about CRM.
+    monkeypatch.setattr(get_settings(), "engagement_campaigns_enabled", False)
     q = InMemoryTaskQueue()
     await _enqueue_due(q)
@@ -377,4 +379,5 @@ async def test_scheduler_omits_crm_sweep_when_disabled(monkeypatch):
     monkeypatch.setattr(get_settings(), "automation_enabled", True)
     monkeypatch.setattr(get_settings(), "crm_sync_enabled", False)
+    monkeypatch.setattr(get_settings(), "engagement_campaigns_enabled", False)
     q = InMemoryTaskQueue()
     await _enqueue_due(q)
@@ -382,5 +385,5 @@ async def test_scheduler_omits_crm_sweep_when_disabled(monkeypatch):
     assert "sync_crm_due_accounts" not in {j.name for j in jobs}
     assert {j.name for j in jobs} == {
-        "advance_cadences", "refresh_due_accounts", "send_daily_digests",
+        "refresh_due_accounts", "send_daily_digests",
         "discover_icp_accounts", "rollup_usage", "roll_billing_periods", "dunning_sweep",
         "billing_reconcile", "expire_trials", "alert_digests", "backfill_companies", "crawl_companies",
@@ -395,4 +398,17 @@ async def test_scheduler_omits_crm_sweep_when_disabled(monkeypatch):
 
 
+@pytest.mark.asyncio
+@pytest.mark.parametrize("crm, engine, logged", [(True, True, True), (True, False, False),
+                                                 (False, True, False)])
+async def test_the_engagement_crm_log_needs_both_switches(monkeypatch, crm, engine, logged):
+    """Sent emails, replies and meetings reach a CRM only while "Push to CRM" is on AND the
+    engagement engine is running (spec §19)."""
+    monkeypatch.setattr(get_settings(), "crm_sync_enabled", crm)
+    monkeypatch.setattr(get_settings(), "engagement_campaigns_enabled", engine)
+    q = InMemoryTaskQueue()
+    await _enqueue_due(q)
+    assert ("log_engagement_crm" in {j.name for j in await _drain(q)}) is logged
+
+
 @pytest.mark.asyncio
 async def test_app_lifespan_registers_account_scored_subscriber():
```

The runtime-config tests use `crm_sync_enabled` as their example switch, and the removed settings leave the expectations — `tests/test_runtime_config.py`, `tests/test_runtime_control_plane.py`, `tests/test_reacher_verifier.py`:

```diff
diff --git a/tests/test_runtime_control_plane.py b/tests/test_runtime_control_plane.py
index 19e3b13..10d789f 100644
--- a/tests/test_runtime_control_plane.py
+++ b/tests/test_runtime_control_plane.py
@@ -56,5 +56,4 @@ EXTRAS = {
     "email_finder_max_candidates": (EMAIL, "int", 1, 20),
     "email_reverify_cooldown_days": (EMAIL, "int", 0, 365),
-    "campaign_sourced_min_send_confidence": (OUTREACH, "float", 0, 1),
     "personalization_max_posts": ("Personalization", "int", 1, 10),
     "signal_dork_max_queries": (SIGNALS, "int", 0, 10),
@@ -73,6 +72,4 @@ EXTRAS = {
     "icp_discovery_enrich_max": (AUTOMATION, "int", 0, 200),
     "lookalike_enrich_max": (CONTACTS, "int", 0, 50),
-    "cadence_batch_size": (OUTREACH, "int", 1, 1000),
-    "cadence_max_duration_days": (OUTREACH, "int", 1, 365),
     "crm_sync_batch_size": (OUTREACH, "int", 1, 1000),
     "billing_dunning_schedule_days": ("Billing", "str", None, None),
```

```diff
diff --git a/tests/test_reacher_verifier.py b/tests/test_reacher_verifier.py
index 678dfa8..cdf152a 100644
--- a/tests/test_reacher_verifier.py
+++ b/tests/test_reacher_verifier.py
@@ -314,6 +314,4 @@ def test_contact_sourcing_settings_defaults():
     assert s.email_finder_max_candidates == 12
     assert s.contact_search_sources == "stub"
-    assert s.campaign_sourcing_enabled is True
-    assert s.campaign_sourced_min_send_confidence == 0.5
     assert s.contact_search_source_list == ["stub"]
 
```

(`tests/test_runtime_config.py`: replace every `cadence_enabled` with `crm_sync_enabled`; same shape, same default.)

The engine is on by default — `tests/test_engagement_platform_config.py` and `tests/test_engagement_sequences.py`:

```diff
diff --git a/tests/test_engagement_platform_config.py b/tests/test_engagement_platform_config.py
index f0425c6..5f9759e 100644
--- a/tests/test_engagement_platform_config.py
+++ b/tests/test_engagement_platform_config.py
@@ -83,9 +83,13 @@ def test_endpoints_are_derived_from_the_base_url(monkeypatch):
 
 
-def test_campaigns_are_off_until_switched_on():
-    from nexus.core.config import Settings
+def test_campaigns_are_on_since_the_cutover_and_off_is_the_emergency_stop(monkeypatch):
+    """The engagement engine replaced the old Campaigns and Cadences (spec §13), so it is on by
+    default; switching it off stops every campaign and every mailbox read."""
+    from nexus.core.config import Settings, get_settings
     from nexus.engagement import config
 
-    assert Settings.model_fields["engagement_campaigns_enabled"].default is False
+    assert Settings.model_fields["engagement_campaigns_enabled"].default is True
+    assert config.campaigns_enabled() is True
+    monkeypatch.setattr(get_settings(), "engagement_campaigns_enabled", False)
     assert config.campaigns_enabled() is False
 
```

```diff
diff --git a/tests/test_engagement_sequences.py b/tests/test_engagement_sequences.py
index 1961633..1519aff 100644
--- a/tests/test_engagement_sequences.py
+++ b/tests/test_engagement_sequences.py
@@ -435,5 +435,6 @@ async def test_the_worker_job_is_dark_until_the_switch_is_on(monkeypatch):
 # ---- the API ---------------------------------------------------------------------------------------
 
-async def test_the_campaign_api_is_invisible_while_the_engine_is_dark(client):
+async def test_the_campaign_api_is_invisible_while_the_engine_is_dark(client, monkeypatch):
+    monkeypatch.setattr(get_settings(), "engagement_campaigns_enabled", False)
     token = await signup(client, slug="darkapi", email="owner@darkapi.com", company="Dark")
     assert (await client.get("/api/engagement/campaigns", headers=auth(token))).status_code == 404
```

Sourcing's new path — `tests/test_contact_sourcing.py`, `tests/test_enrichment_billing.py`; the old campaign's outcome roll-up leaves `tests/test_sdr_adoption.py`:

```diff
diff --git a/tests/test_contact_sourcing.py b/tests/test_contact_sourcing.py
index f4de333..f5029ca 100644
--- a/tests/test_contact_sourcing.py
+++ b/tests/test_contact_sourcing.py
@@ -2,5 +2,5 @@
 from __future__ import annotations
 
-from nexus.campaigns.sourcing import ContactSourcingService, SourcingOutcome
+from nexus.contacts.sourcing import ContactSourcingService, SourcingOutcome
 from nexus.enrichment.providers import PatternEmailProvider
 from nexus.enrichment.waterfall import WaterfallEnricher
```

```diff
diff --git a/tests/test_sdr_adoption.py b/tests/test_sdr_adoption.py
index b64b9f9..03d1123 100644
--- a/tests/test_sdr_adoption.py
+++ b/tests/test_sdr_adoption.py
@@ -13,45 +13,7 @@ from tests.conftest import auth, signup
 
 
-async def _seed_campaign(client, token) -> tuple[str, str]:
-    """Account + saved list + campaign (drafts inline to the approval gate)."""
-    acct = await client.post(
-        "/api/accounts", headers=auth(token), json={"name": "Acme", "domain": "acme.sdr"}
-    )
-    assert acct.status_code == 201, acct.text
-    lst = await client.post(
-        "/api/lists", headers=auth(token), json={"name": "All", "filter": {}}
-    )
-    assert lst.status_code == 201, lst.text
-    camp = await client.post(
-        "/api/campaigns",
-        headers=auth(token),
-        json={"name": "Q3 push", "list_id": lst.json()["id"]},
-    )
-    assert camp.status_code == 201, camp.text
-    return acct.json()["id"], camp.json()["id"]
-
-
-@pytest.mark.asyncio
-async def test_outcome_attributes_to_campaign_and_rolls_up(client):
-    token = await signup(client, slug="attr", email="o@attr.x", company="AttrCo")
-    account_id, campaign_id = await _seed_campaign(client, token)
-
-    r = await client.post(
-        "/api/outcomes",
-        headers=auth(token),
-        json={"stage": "replied", "account_id": account_id, "campaign_id": campaign_id},
-    )
-    assert r.status_code == 201, r.text
-    assert r.json()["campaign_id"] == campaign_id
-    r = await client.post(
-        "/api/outcomes",
-        headers=auth(token),
-        json={"stage": "meeting", "account_id": account_id, "campaign_id": campaign_id},
-    )
-    assert r.status_code == 201, r.text
-
-    detail = await client.get(f"/api/campaigns/{campaign_id}", headers=auth(token))
-    assert detail.status_code == 200, detail.text
-    assert detail.json()["outcomes"] == {"replied": 1, "meeting": 1}
+# Attribution to a campaign now runs through the engagement engine: see
+# tests/test_engagement_reporting.py (`test_the_outcomes_api_takes_an_engagement_campaign`). The old
+# `campaign_id` is still accepted for history, and still refuses an id that is not one.
 
 
```

And the screens — `tests/test_engagement_screens_ui.py`:

```diff
diff --git a/tests/test_engagement_screens_ui.py b/tests/test_engagement_screens_ui.py
index 6aa372d..59e2652 100644
--- a/tests/test_engagement_screens_ui.py
+++ b/tests/test_engagement_screens_ui.py
@@ -27,16 +27,22 @@ def _nav_block(route: str) -> str:
 
 
-def test_the_new_screens_appear_only_when_the_engine_is_on():
+def test_the_engagement_screens_are_the_only_campaign_screens():
+    """Since the cutover (spec §13) the old Campaigns and Cadences are gone. Their addresses forward,
+    because bookmarks and old links still arrive at them."""
     for route in ("/engagement/campaigns", "/engagement/replies", "/engagement/templates"):
-        assert 'engine: "on"' in _nav_block(route), f"{route} must wait for the engine switch"
-    # The old engine's pages leave the menu at the same moment, so a workspace never sees both.
-    for route in ("/campaigns", "/cadences"):
-        assert 'engine: "off"' in _nav_block(route), f"{route} must leave when the engine is on"
+        assert 'engine: "on"' in _nav_block(route), f"{route} must leave when the engine is off"
+    nav = _read(NAV)
+    assert 'to: "/campaigns"' not in nav and 'to: "/cadences"' not in nav
+    app = _read(APP)
+    assert '<Route path="/campaigns" element={<Navigate to="/engagement/campaigns" replace />} />' in app
+    assert '<Route path="/cadences" element={<Navigate to="/engagement/templates" replace />} />' in app
+    for gone in ("CampaignsPage.tsx", "CadencesPage.tsx"):
+        assert not (SRC / "pages" / gone).exists(), f"pages/{gone} belongs to the old engine"
 
 
-def test_can_see_reads_the_engine_and_unknown_keeps_the_old_pages():
+def test_the_engagement_pages_leave_the_menu_only_when_the_engine_is_confirmed_off():
     source = _read(NAV)
-    assert 'item.engine === "on" && engineOn !== true' in source
-    assert 'item.engine === "off" && engineOn === true' in source
+    assert 'item.engine === "on" && engineOn === false' in source
+    assert '"off"' not in source.split("export function canSee", 1)[1].split("\n}\n", 1)[0]
     assert "canSee(item, role, isPlatformAdmin, engineOn)" in _read(
         SRC / "components" / "layout" / "Sidebar.tsx")
@@ -55,9 +61,11 @@ def test_every_engagement_route_waits_for_the_engine():
 
 
-def test_an_unreadable_engine_status_reads_as_off():
-    """The new pages 404 while the engine is dark, so a status we could not read must not offer
-    them; the old pages work either way."""
+def test_an_unreadable_engine_status_reads_as_on():
+    """After the cutover these are the only campaign screens, so an unreadable status must not
+    delete them; the server is the boundary and answers 404 if the engine really is off. Off is
+    said in place, keeping the URL, since there is no other campaign screen to send anyone to."""
     source = _read(SRC / "app" / "EngagementContext.tsx")
-    assert "state.error ? { engine_on: false" in source
+    assert "state.error ? { engine_on: true" in source
+    assert "<Navigate" not in source and "<FeatureUnavailable" in source
 
 
@@ -183,2 +191,28 @@ def test_a_tab_strip_scrolls_rather_than_widening_the_page():
     tabs_rule = css.split(".tabs {", 1)[1].split("}", 1)[0]
     assert "border-bottom" not in tabs_rule and "inset 0 -1px 0 var(--border)" in tabs_rule
+
+
+# ---- the cutover (phase 15) ----------------------------------------------------------------------
+
+def test_discovery_hands_its_selection_to_the_campaign_builder():
+    panel = _read(SRC / "components" / "discovery" / "ResultsPanel.tsx")
+    assert 'navigate("/engagement/campaigns/new", {' in panel
+    assert "state: selectionForCampaign(candidates, selected)" in panel
+    assert "launchFromSelection" not in panel and "Cadence" not in panel
+    builder = _read(PAGES / "CampaignBuilder.tsx")
+    assert "useLocation().state" in builder
+    # Added once the campaign exists; drafting and sending still wait for review.
+    assert "api.addCampaignContacts(campaign.id, handedContacts, handedAccounts)" in builder
+
+
+def test_a_campaign_without_a_mailbox_offers_its_owner_one():
+    detail = _read(PAGES / "CampaignDetailPage.tsx")
+    assert "<MailboxChooser" in detail and "api.setCampaignMailbox(" in detail
+    assert "isOwner={c.owner_user_id === currentUserId}" in detail
+
+
+def test_finished_old_campaigns_are_listed_and_never_opened():
+    page = _read(PAGES / "CampaignsPage.tsx")
+    assert "<LegacyCampaigns team={team} />" in page
+    legacy = page.split("function LegacyCampaigns", 1)[1]
+    assert "onRowClick" not in legacy and "api.legacyCampaigns(" in legacy
```

- [ ] **Step 4: Run** the full suite, `pytest -q -n 6`, and `ruff check nexus tests` — expected PASS, clean.

- [ ] **Step 5: See it.** In the isolated preview, seed old-engine rows (a cadence campaign mid-sequence whose owner's mailbox is not connected, and a finished campaign), then run the dry run, the real run and the real run again inside the container: the report, the move and the idempotence. `/campaigns` forwards to the new Campaigns, the moved campaign is paused with its reason, Earlier campaigns lists the finished one, and after reconnecting the mailbox the campaign page's "Send from this mailbox" sets it running at the same step.

- [ ] **Step 6: Commit**

```bash
git add -A frontend/src tests docs CLAUDE.md
git commit -m "feat(engagement): phase 15 - cutover"
```

---

## Release

Follow `docs/engagement/cutover-runbook.md`: backup, one alembic head, deploy, dry run read with the owner, real run, counts verified, re-run as SDRs connect mailboxes. Before any push, the full CI-equivalent suite on the exact commit, plus the live provider suite once the OAuth apps and test mailboxes exist (§14).
