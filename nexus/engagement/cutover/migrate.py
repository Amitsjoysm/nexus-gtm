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
