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
#: The most people one add enrolls. A bigger list is added in several presses, each taking the next
#: ones not yet in the campaign; the response says how many are left.
MAX_PER_ADD = 2000


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
    """Add contacts. Skips what cannot be emailed; warns about what someone else is emailing.

    A fixed number of reads whatever the count (2026-10-22). It read the contact, their company,
    their do-not-contact rows and their other campaigns one person at a time, about eight reads
    each, so "Add everyone on it" for a 2,000-person list was some 16,000 reads in one request."""
    from sqlalchemy import select

    from nexus.engagement.suppression.service import active_reasons, normalize_email
    from nexus.prospector.hold import HOLD_REASON, held_until_verified
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
    result = EnrollResult()
    wanted = list(dict.fromkeys(contact_ids))
    if not wanted:
        return result
    mailbox = await ts.get(MailboxConnection, campaign.mailbox_connection_id)
    existing = set((await ts.session.scalars(
        select(EngagementEnrollment.contact_id)
        .where(EngagementEnrollment.tenant_id == ts.tenant_id,
               EngagementEnrollment.campaign_id == campaign.id))).all())
    contacts = {c.id: c for c in await ts.list(Contact, Contact.id.in_(wanted))}
    accounts = {a.id: a for a in await ts.list(
        Account, Account.id.in_({c.account_id for c in contacts.values()}))} if contacts else {}
    blocked = await active_reasons(ts, [c.email or "" for c in contacts.values()])

    for contact_id in wanted:
        contact = contacts.get(contact_id)
        if contact is None or getattr(contact, "deleted_at", None) is not None:
            result.skipped.append({"contact_id": contact_id, "reason": "not_found"})
            continue
        if contact_id in existing:
            result.skipped.append({"contact_id": contact_id, "reason": "already_enrolled"})
            continue
        if not (contact.email or "").strip():
            result.skipped.append({"contact_id": contact_id, "reason": "no_email"})
            continue
        if held_until_verified(contact):
            # A Prospector email is the provider's, unverified: held out of bulk sending until a
            # verifier calls it deliverable (D19). A one-off email is not held.
            result.skipped.append({"contact_id": contact_id, "reason": HOLD_REASON})
            continue
        reason = blocked.get(normalize_email(contact.email))
        if reason is not None:
            result.skipped.append({"contact_id": contact_id, "reason": f"do_not_contact:{reason}"})
            continue
        account = accounts.get(contact.account_id)
        zone = resolve_zone(
            contact_tz=(contact.custom_fields or {}).get("timezone"),
            account_country=getattr(account, "country", None),
            account_region=getattr(account, "region", None),
            sdr_tz=getattr(mailbox, "timezone", None))
        ts.add(EngagementEnrollment(
            campaign_id=campaign.id, contact_id=contact.id, account_id=contact.account_id,
            mailbox_connection_id=campaign.mailbox_connection_id, status="awaiting_review",
            current_step_index=0, contact_timezone=zone.name))
        existing.add(contact_id)
        result.added.append(contact.id)

    if result.added:
        # The duplicate-outreach guard (§19), for everyone added, in three reads.
        others = await ts.list(
            EngagementEnrollment, EngagementEnrollment.contact_id.in_(result.added),
            EngagementEnrollment.campaign_id != campaign.id,
            EngagementEnrollment.status.in_(("active", "awaiting_review", "paused", "snoozed")))
        campaigns = {c.id: c for c in await ts.list(
            EngagementCampaign, EngagementCampaign.id.in_({o.campaign_id for o in others}))} \
            if others else {}
        owner_ids = {c.owner_user_id for c in campaigns.values() if c.owner_user_id}
        owners = {u.id: u for u in (await ts.session.scalars(
            select(User).where(User.id.in_(owner_ids)))).all()} if owner_ids else {}
        order = {cid: i for i, cid in enumerate(result.added)}
        for other in sorted(others, key=lambda o: order.get(o.contact_id, 0)):
            other_campaign = campaigns.get(other.campaign_id)
            owner = owners.get(getattr(other_campaign, "owner_user_id", None))
            result.warnings.append({
                "contact_id": other.contact_id, "campaign_id": other.campaign_id,
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
                             limit: int | None = None, only: set[str] | None = None) -> dict:
    """Draft every step-0 email that has no draft yet. Idempotent: a second run drafts nothing.

    ``only`` limits it to those enrollments: a referral adds one person to a running campaign and
    must not pay to draft anyone else who happens to be waiting."""
    from sqlalchemy import select

    from nexus.engagement.drafting.drafter import draft
    from nexus.models.account import Account, Contact
    from nexus.models.engagement import (
        EngagementEnrollment,
        EngagementMessage,
        MailboxConnection,
    )

    steps = await steps_of(ts, campaign)
    if not steps:
        # Every campaign is created with a step, but a hand-built or migrated one may have none,
        # and an opening email has nothing to be written from.
        raise CampaignError("This campaign has no steps yet. Add one before drafting.")
    mailbox = await ts.get(MailboxConnection, campaign.mailbox_connection_id)
    pending = await ts.list(EngagementEnrollment, EngagementEnrollment.campaign_id == campaign.id,
                            EngagementEnrollment.status == "awaiting_review")
    # Who already has an opener, in one read. It was one read per person on every pass, and the
    # screen drafts in passes of ten, so a list of 2,000 re-read up to 2,000 rows per pass.
    has_opener = set((await ts.session.scalars(
        select(EngagementMessage.enrollment_id)
        .where(EngagementMessage.tenant_id == ts.tenant_id,
               EngagementMessage.enrollment_id.in_([e.id for e in pending]),
               EngagementMessage.step_index == 0, EngagementMessage.direction == "out")
    )).all()) if pending else set()
    drafted = failed = 0
    errors: list[dict] = []
    # Oldest first, the order Review shows them in.
    for enrollment in sorted(pending, key=lambda e: e.created_at):
        if only is not None and enrollment.id not in only:
            continue
        if limit is not None and drafted >= limit:
            break
        if enrollment.id in has_opener:
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


async def current_messages(ts, enrollments) -> dict:
    """``{enrollment_id: outbound row at the step it is on}``, in one read for any number."""
    from nexus.models.engagement import EngagementMessage

    if not enrollments:
        return {}
    step_of = {e.id: e.current_step_index for e in enrollments}
    out: dict = {}
    for row in await ts.list(EngagementMessage, EngagementMessage.enrollment_id.in_(list(step_of)),
                             EngagementMessage.direction == "out"):
        if row.step_index == step_of.get(row.enrollment_id):
            out.setdefault(row.enrollment_id, row)
    return out


async def review_queue(ts, campaign) -> list[tuple]:
    """``[(enrollment, draft_row_or_None)]`` for everyone awaiting review, oldest first.

    Two reads whatever the size: it used to read once per person, on every refresh of Review."""
    from nexus.models.engagement import EngagementEnrollment

    waiting = sorted(await ts.list(EngagementEnrollment,
                                   EngagementEnrollment.campaign_id == campaign.id,
                                   EngagementEnrollment.status == "awaiting_review"),
                     key=lambda e: e.created_at)
    current = await current_messages(ts, waiting)
    return [(enrollment, current.get(enrollment.id)) for enrollment in waiting]


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


# ---- the sending mailbox ------------------------------------------------------------------------

async def attach_mailbox(ts, campaign, mailbox) -> int:
    """Send this campaign from ``mailbox``, and resume whoever was waiting for one.

    A campaign moved from the old engine whose owner had no connected mailbox is paused as
    `mailbox_disconnected` (D14), and so is anyone the advance loop found with a disconnected one.
    Nothing brought them back: reconnecting a mailbox or choosing another left them paused for
    good. Returns how many people resumed. Their due times are kept, so anyone overdue goes out on
    the next tick, in order.
    """
    from nexus.models.engagement import EngagementEnrollment

    if mailbox is None or mailbox.status != "connected":
        raise CampaignError("Choose a connected mailbox.")
    if mailbox.owner_user_id != campaign.owner_user_id:
        raise CampaignError("A campaign sends from its owner's own mailbox.")
    campaign.mailbox_connection_id = mailbox.id
    resumed = 0
    for enrollment in await ts.list(EngagementEnrollment,
                                    EngagementEnrollment.campaign_id == campaign.id,
                                    EngagementEnrollment.status.notin_(("stopped", "completed"))):
        enrollment.mailbox_connection_id = mailbox.id
        if enrollment.status == "paused" and enrollment.status_reason == "mailbox_disconnected":
            enrollment.status, enrollment.status_reason = "active", None
            resumed += 1
    if campaign.status == "paused" and campaign.pause_reason == "mailbox_disconnected":
        campaign.status, campaign.pause_reason = "active", None
    await ts.flush()
    return resumed


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
    waiting = await ts.list(EngagementEnrollment, EngagementEnrollment.campaign_id == campaign.id,
                            EngagementEnrollment.status == "awaiting_review")
    # The message at the step each person is ON, not step 0: a follow-up held for review (D17, or
    # review every touch) and approved while the campaign was paused was looked for at step 0,
    # whose row had already been sent, so that person never went out again.
    current = await current_messages(ts, waiting)
    for enrollment in waiting:
        row = current.get(enrollment.id)
        if row is not None and row.status == "approved":
            await _activate(ts, campaign, enrollment, row, row.approved_at or now, user_id)
    # The person whose email hit the credit wall was paused with the campaign (D18). Resuming the
    # campaign left them paused for good, under a notice promising it "continues from where it
    # stopped"; nothing else ever resumes an `out_of_credits` pause.
    for enrollment in await ts.list(EngagementEnrollment,
                                    EngagementEnrollment.campaign_id == campaign.id,
                                    EngagementEnrollment.status == "paused",
                                    EngagementEnrollment.status_reason == "out_of_credits"):
        await _resume_after_credits(ts, enrollment, now, user_id)
    return result


async def _resume_after_credits(ts, enrollment, now, user_id) -> None:
    """Back to what it was doing when the charge failed. A re-engagement email still owed (its
    snooze date had passed) stays snoozed, so it is that email which goes, not the next step."""
    if enrollment.snoozed_until is not None:
        await set_status(ts, enrollment, "snoozed", None, user_id=user_id)
        return
    if enrollment.next_action_at is None or enrollment.next_action_at < now:
        enrollment.next_action_at = now
    await set_status(ts, enrollment, "active", None, user_id=user_id)


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
