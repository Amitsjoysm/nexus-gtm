# Phase 14: SDR Enhancements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Three day-to-day SDR problems from spec §19, solved with parts the engine already has. "Talk to Jane" becomes an intro to Jane waiting for review. Sent emails, replies and booked meetings are logged in the workspace's own CRM. And when something happens at a company whose people went quiet or asked for later, the SDR is told and handed an email to send.

**Architecture:** `nexus/engagement/enhancements/` holds one module per enhancement, each a pure core and a thin shell. `referral.py` reads who a reply names (`extract`, pure), then finds or enriches that person, adds them to the referrer's campaign and drafts the intro through the existing first-draft path. `crm_log.py` lists what is not yet in the CRM and writes it through the connector the account sync already resolves; a worker sweep runs it, and migration `0058` adds the marks that make each write happen once. `signal_reengage.py` decides who is fair to write to again and drafts and sends in the same thread through `engagement.sending.send`. The desk router gets two referral routes and a new `engagement_restart` router serves the suggestions. The reply desk gets a referral panel and a "Write again" tab, and Today gets one line.

**Tech Stack:** FastAPI, async SQLAlchemy, Alembic, React + TypeScript, CSS Modules.

**Roadmap:** [00-roadmap.md](00-roadmap.md). **Spec:** §19 (referral follow-through, CRM activity logging, signal re-engagement), D22 (nothing sends without a person), D21 (no mocks). **Depends on:** phases 10 (the desk), 12 (Today), 13 (likelihood).

**Verified:** implemented on `feat/sdr-engagement` on top of phases 01–13. In the CI image: `tests/test_engagement_enhancements.py` (11 passed): who a reply names, from the Cc line, the text and the phrases people use; addresses on other domains, the sender, departments and "me" named as nobody; a referral becoming an intro in the review queue with the referrer in the prompt and their words kept out; "already in" and "add their surname" refusals; a campaign with no steps refused before anyone is created; sends, replies and meetings reaching the CRM once, with an account not yet in the CRM waiting and an out-of-office left out; the CRM log waiting for both switches; who is and is not suggested (quiet, a far "later", but not declined, a near "later", do-not-contact, news from before they went quiet, or someone another campaign is about to email); a drafted email in the same thread sent once; the routes dark with the engine. Also three new structural checks in `tests/test_engagement_screens_ui.py`, and every engagement, migration-replay, metering-coverage, CRM, job-durability, RLS-guard, worker, credential-leak and plan-gated-nav test together (407 passed before the last three fixes, and the affected suites again after them); `ruff`; `tsc --noEmit`; `npm run build`. In the browser against the isolated preview, at phone width: a referral reply showed "They pointed you to someone" with Dev Lee (copied on the reply) chosen; Draft intro added him to the campaign and showed the done state; "Review the intro" opened the campaign on its Review tab with his draft there; the Write again tab listed Priya Shah (went quiet, "May reply", the Series B headline) and drafted a "Re:" email in her thread; Today listed "Write again to 1 person"; no horizontal overflow.

---

## Decisions this phase makes

- **A referral is read, never guessed.** An email address is an identity; a name is a lead. Addresses come from the Cc line first (they looped the person in) and then the reply's own words, only on the account's domain or the sender's, and never the sender's or ours. Names come from the phrases people use to pass you on. Quoted history is cut off first: our own earlier email names people too.
- **A name is matched only inside the account.** The referrer's colleagues are the only candidates; a first name alone only when exactly one contact there has it. A lone unknown first name stops and asks for a surname or an address, because an intro to the wrong Jane is worse than none. The cross-tenant rule ("a name is not an identity") is why this never looks beyond the account.
- **The intro goes where every opening email goes: the review queue.** The person joins the referrer's campaign awaiting review, and `draft_first_emails(..., only={enrollment})` drafts just them, so a running campaign does not pay to draft anyone else who happens to be waiting. Spending is the SDR's decision: reading who was named is free, and Draft intro spends an enrichment credit (only when the address is blank) and a draft credit.
- **Who made the introduction travels on the contact.** `custom_fields["referred_by"]`, not the draft, so a regenerated draft and every follow-up still know it. Only the referrer's name and title reach the prompt, never their words, which were written to us. The name is also added to the pack's facts, so the specific-fact check (D17) accepts an intro that mentions it.
- **CRM logging uses the account sync's connector and its records.** An activity is written only against an account the sync has already put in that CRM (`crm_id`, with `crm_source` naming the same CRM). An account the CRM does not know waits, and follows once it is there while inside the seven-day look-back. Creating a CRM company here would be a second sync path; writing against a `crm_id` from a CRM the workspace has left would attach the note in the wrong system.
- **A mark per row, not a watermark.** Migration `0058` adds `crm_logged_at` to `engagement_messages` and `reply_classifications`. A watermark that read past a row committed late would skip it forever, and one kept in `tenants.email_settings` would race an admin saving settings. A row is marked only when the CRM accepted it.
- **Behind the same switches as every CRM write, and dark with the engine.** `crm_sync_enabled` ("Push to CRM") and the workspace's `automation_enabled`, plus `engagement_campaigns_enabled`. The connector is resolved once per workspace inside its own session, for the reason the account sync gives.
- **Signal re-engagement is a suggestion (D22).** Eligible: a sequence that finished without a reply, or a "later" more than two weeks from its date. Never declined, unsubscribed or do-not-contact, never anyone emailed in the last 14 days, and never anyone another campaign is about to email. The last rule came from the preview, where the seeded Priya was both quiet in one campaign and waiting for review in another. The news must be recent, above the alert floor, and after they went quiet, or after their last reply for a "later": `updated_at` moves on any edit, so it is not the anchor.
- **One email in the same thread, once per piece of news.** The draft opens with the news ("signal" kind: never pretend they replied; say you are early if they asked for later). The send's idempotency key is the enrollment and the signal, so a second press or a retry cannot send twice. The enrollment does not restart; a reply reaches the desk like any other.
- **Two fixes the browser found.** `draft_first_emails` indexed `steps[0]` with no steps and returned a 500 from Draft intro on a campaign built without one; it now refuses in words (409), and the referral path checks before creating anyone. Shared grids asked for a 16–20rem minimum column and pushed forms past a phone-width pane, and the `Tabs` strip clipped its fourth tab. Both are fixed at the source: `minmax(min(Xrem, 100%), 1fr)`, and a tab strip that scrolls sideways with its divider drawn as an inset shadow, so the selected underline is not clipped.

---

## Files

| Action | Path | Responsibility |
|---|---|---|
| Create | `migrations/versions/0058_engagement_crm_log.py` | the two `crm_logged_at` marks |
| Modify | `nexus/models/engagement.py` | the columns |
| Create | `nexus/engagement/enhancements/__init__.py`, `referral.py`, `crm_log.py`, `signal_reengage.py` | the three enhancements |
| Modify | `nexus/engagement/drafting/context.py`, `drafter.py` | the referral block and the "signal" kind |
| Modify | `nexus/engagement/sequences/service.py` | `only=` and the no-steps refusal |
| Modify | `nexus/workers/tasks.py`, `scheduler.py` | the CRM log sweep |
| Modify | `nexus/engagement/reports/today.py` | "Write again" on Today; review links open the Review tab |
| Modify | `nexus/api/routers/engagement_desk.py`, `engagement_campaigns.py`, `__init__.py`; create `engagement_restart.py` | routes |
| Modify | `frontend/src/lib/types.ts`, `api.ts` | types and client |
| Create | `frontend/src/pages/engagement/ReferralPanel.tsx`, `WriteAgain.tsx` | the screens |
| Modify | `ReplyDeskPage.tsx`, `CampaignDetailPage.tsx`, `InsightBadge.tsx`, `TodayPlan.tsx`, `Engagement.module.css`, `MailboxesPage.module.css`, `ReportsPanel.module.css`, `components/ui/Tabs.module.css` | where they appear, and the layout fixes |
| Create/Modify | `tests/test_engagement_enhancements.py`, `tests/test_engagement_screens_ui.py` | tests |

---

### Task 1: The CRM log marks

**Files:** Create `migrations/versions/0058_engagement_crm_log.py`; modify `nexus/models/engagement.py`.

- [ ] **Step 1: Write the migration:**

```python
"""engagement CRM activity log: what has already been written to the workspace's CRM

Two nullable timestamps, one per thing the CRM log writes (spec §19, "CRM activity logging"):

* ``engagement_messages.crm_logged_at`` — a sent email or a received reply, logged once.
* ``reply_classifications.crm_logged_at`` — a meeting booked from the reply desk, logged once. The
  reply the meeting came from is a different activity and carries its own mark on its message.

A mark rather than a per-workspace watermark: a watermark read past a row that committed late would
skip it forever, and one stored in ``tenants.email_settings`` would race an admin saving settings.
NULL for every existing row; the sweep only looks back seven days, so nothing old is backfilled.

Revision ID: 0058_engagement_crm_log
Revises: 0057_engagement
Create Date: 2026-09-24
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0058_engagement_crm_log"
down_revision = "0057_engagement"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("engagement_messages") as batch:
        batch.add_column(sa.Column("crm_logged_at", sa.DateTime(timezone=True), nullable=True))
    with op.batch_alter_table("reply_classifications") as batch:
        batch.add_column(sa.Column("crm_logged_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("reply_classifications") as batch:
        batch.drop_column("crm_logged_at")
    with op.batch_alter_table("engagement_messages") as batch:
        batch.drop_column("crm_logged_at")
```

- [ ] **Step 2: The columns** — apply to `nexus/models/engagement.py`:

```diff
diff --git a/nexus/models/engagement.py b/nexus/models/engagement.py
index 03c88a1..2eaef00 100644
--- a/nexus/models/engagement.py
+++ b/nexus/models/engagement.py
@@ -277,4 +277,6 @@ class EngagementMessage(IdMixin, TimestampMixin, TenantScoped, Base):
     received_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
     error: Mapped[str | None] = mapped_column(Text, nullable=True)
+    # When this email or reply was written to the workspace's CRM as an activity (§19).
+    crm_logged_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
 
 
@@ -311,4 +313,6 @@ class ReplyClassification(IdMixin, TimestampMixin, TenantScoped, Base):
     reminded_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
     status: Mapped[str] = mapped_column(String(8), default="open")
+    # When a meeting decided here was written to the workspace's CRM as an activity (§19).
+    crm_logged_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
 
 
```

- [ ] **Step 3: Run** `pytest tests/test_migrations_replay.py -n0 -q` — expected PASS (one head, replayed schema equals `Base.metadata`).

---

### Task 2: Referral follow-through

**Files:** Create `nexus/engagement/enhancements/__init__.py`, `referral.py`; modify `drafting/context.py`, `sequences/service.py`.

- [ ] **Step 1: Write the failing tests** — create `tests/test_engagement_enhancements.py` from Task 7. Run `pytest tests/test_engagement_enhancements.py -n0 -q -k "referral or address or name_is or no_steps"` — expected FAIL: `No module named 'nexus.engagement.enhancements'`.

- [ ] **Step 2: Implement** `nexus/engagement/enhancements/__init__.py`:

```python
"""SDR enhancements built from parts the engine already has (spec §19): referral follow-through,
CRM activity logging, and signal re-engagement."""
```

and `nexus/engagement/enhancements/referral.py`:

```python
"""Referral follow-through (spec §19): "talk to Jane" becomes an intro to Jane, drafted for review.

Three steps, and only the first runs without a person asking:

1. **Read who was named** (`extract`, pure). An email address is an identity; a name is only a
   lead. Addresses come from the reply's new text (quoted history cut off) and its Cc line, only on
   the account's own domain or the sender's, because a referral points to a colleague, and never the
   sender's own address or ours. Names come after the phrases people use to pass you on ("talk to",
   "reach out to", "loop in") or before "is the right person".
2. **Find or enrich** (`follow_through`, when the SDR presses Draft intro). An address is looked up
   in the workspace, then created at the account. A name is matched only among that account's own
   contacts, never across accounts, and a first name only when exactly one contact there has it. A
   full name nobody has is created and run through the email finder, charged as `enrich.contact`
   exactly like the Enrich button. A lone first name with no address stops and asks for more: an
   intro sent to the wrong Jane is worse than one not sent.
3. **Draft for review.** The person joins the referrer's campaign awaiting review, with an opening
   email that says who suggested the conversation. Nothing is sent: the review queue is where every
   opening email is approved, and a referral is no exception.

The referral is recorded on the new contact (`custom_fields["referred_by"]`), not on the draft, so a
regenerated draft and every follow-up still know who made the introduction. Only the referrer's name
and title reach the prompt, never their words: those were written to us, not to Jane.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass

EMAIL = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_NAME = r"([A-Z][a-zA-Z'’\-]+(?:[ \t]+[A-Z][a-zA-Z'’\-]+){0,2})"
_BEFORE = re.compile(
    r"(?i:\b(?:talk(?:ing)?|speak(?:ing)?|chat)\s+(?:to|with)|\breach(?:ing)?\s+out\s+to|"
    r"\bget\s+in\s+touch\s+with|\bcontact(?:ing)?|\bconnect(?:ing)?\s+(?:you\s+)?with|"
    r"\bloop(?:ing)?\s+in|\bcc'?(?:ing|'?d)|\bintroduc(?:e|ing)\s+you\s+to|"
    r"\bforward(?:ing|ed)?\s+(?:this|you)\s+to)[ \t]+" + _NAME)
_AFTER = re.compile(
    _NAME + r"(?i:[ \t]+(?:is|would\s+be)\s+(?:the\s+)?(?:right|best|better)\s+(?:person|contact)"
    r"|[ \t]+(?:handles|owns|runs|leads|looks\s+after)\b)")
#: Capitalised words that follow a cue without being anybody.
_NOT_A_NAME = frozenset("""
i we our us the this that these those thanks thank please hi hello regards best cheers team sales
marketing engineering finance procurement legal support hr operations me him her them you my your
someone somebody anyone everyone whoever it they he she ceo cto cfo coo vp head
""".split())


@dataclass(slots=True)
class Referred:
    name: str
    email: str
    evidence: str          # "copied on the reply" | "named in the reply"

    def as_dict(self) -> dict:
        return asdict(self)


def _clean_name(raw: str) -> str:
    words = [w for w in raw.split() if w]
    while words and words[-1].lower().strip("'’") in _NOT_A_NAME:
        words.pop()
    if not words or words[0].lower() in _NOT_A_NAME:
        return ""
    return " ".join(words)


def name_from_email(email: str) -> str:
    """``jane.doe@`` reads as Jane Doe; ``jdoe@`` does not read as anybody."""
    local = email.split("@", 1)[0]
    parts = [p for p in re.split(r"[._\-]+", local) if p.isalpha()]
    return " ".join(p.capitalize() for p in parts) if len(parts) >= 2 else ""


def _domain(email: str) -> str:
    return email.rsplit("@", 1)[-1].lower() if "@" in email else ""


def extract(text: str, *, cc: list[str] | tuple = (), sender: str = "", ours: str = "",
            account_domain: str = "", exclude_names: tuple[str, ...] = ()) -> list[Referred]:
    """Who a reply points to, strongest evidence first. Pure."""
    allowed = {d for d in (account_domain.lower().removeprefix("www."), _domain(sender)) if d}
    skip = {sender.lower(), ours.lower()}
    excluded = {n.lower() for n in exclude_names if n}
    excluded |= {n.split()[0] for n in excluded if n.split()}
    found: list[Referred] = []
    seen: set[str] = set()

    def add_email(address: str, evidence: str) -> None:
        address = address.strip().strip(".,;:<>()[]").lower()
        if not address or address in skip or address in seen:
            return
        if allowed and _domain(address) not in allowed:
            return
        seen.add(address)
        found.append(Referred(name=name_from_email(address), email=address, evidence=evidence))

    for address in cc or ():
        add_email(address, "copied on the reply")
    for match in EMAIL.finditer(text or ""):
        add_email(match.group(0), "named in the reply")

    names: list[str] = []
    for pattern in (_BEFORE, _AFTER):
        for match in pattern.finditer(text or ""):
            name = _clean_name(match.group(match.lastindex))
            if name and name.lower() not in excluded and name.lower() not in {n.lower() for n in names}:
                names.append(name)
    for name in names:
        first = name.split()[0].lower()
        # "Talk to Jane Doe, jane.doe@acme.io" is one person, not two.
        owner = next((r for r in found if first in re.split(r"[._\-]+", r.email.split("@")[0])),
                     None)
        if owner is not None:
            if len(name.split()) >= len(owner.name.split()):
                owner.name = name
            continue
        found.append(Referred(name=name, email="", evidence="named in the reply"))
    return found


# ---- the shell --------------------------------------------------------------------------------

class ReferralError(ValueError):
    """Why an intro cannot be drafted, in words the SDR can act on."""


async def _referral_context(ts, classification):
    from nexus.engagement.replies.parse import strip_quoted
    from nexus.models.account import Account, Contact
    from nexus.models.engagement import EngagementMessage, MailboxConnection

    message = await ts.get(EngagementMessage, classification.message_id)
    referrer = await ts.get(Contact, classification.contact_id) \
        if classification.contact_id else None
    account_id = classification.account_id or getattr(referrer, "account_id", None)
    account = await ts.get(Account, account_id) if account_id else None
    mailbox = await ts.get(MailboxConnection, classification.mailbox_connection_id)
    text = strip_quoted(getattr(message, "body_text", "") or "")
    return message, referrer, account, mailbox, text


async def find_existing(ts, account, *, name: str, email: str, exclude_id: str | None = None):
    """The contact this referral means, if the workspace already has them. Never a guess."""
    from sqlalchemy import func

    from nexus.models.account import Contact

    if email:
        return await ts.first(Contact, func.lower(Contact.email) == email.lower(),
                              Contact.deleted_at.is_(None))
    if account is None or not name:
        return None
    people = [c for c in await ts.list(Contact, Contact.account_id == account.id,
                                       Contact.deleted_at.is_(None)) if c.id != exclude_id]
    wanted = " ".join(name.lower().split())
    exact = [c for c in people if " ".join((c.full_name or "").lower().split()) == wanted]
    if len(exact) == 1:
        return exact[0]
    if len(wanted.split()) == 1:
        firsts = [c for c in people if (c.full_name or "").lower().split()[:1] == [wanted]]
        if len(firsts) == 1:
            return firsts[0]
    return None


async def candidates(ts, classification) -> list[dict]:
    """Who the reply names, and whether each is already a contact."""
    message, referrer, account, mailbox, text = await _referral_context(ts, classification)
    if message is None:
        return []
    named = extract(text, cc=list(message.cc_addrs or []), sender=message.from_addr or "",
                    ours=getattr(mailbox, "email", "") or "",
                    account_domain=getattr(account, "domain", "") or "",
                    exclude_names=(getattr(referrer, "full_name", "") or "",
                                   getattr(mailbox, "display_name", "") or ""))
    out = []
    for person in named:
        existing = await find_existing(ts, account, name=person.name, email=person.email,
                                       exclude_id=getattr(referrer, "id", None))
        out.append({**person.as_dict(), "contact_id": getattr(existing, "id", None),
                    "contact_name": getattr(existing, "full_name", None),
                    "contact_email": getattr(existing, "email", None)})
    return out


async def _campaign_for(ts, classification, referrer):
    from nexus.models.engagement import EngagementCampaign, EngagementEnrollment, EngagementMessage
    from nexus.models.engagement import EngagementThread

    enrollment_id = classification.enrollment_id
    if enrollment_id is None:
        message = await ts.get(EngagementMessage, classification.message_id)
        thread = await ts.get(EngagementThread, message.thread_id) \
            if message is not None and message.thread_id else None
        enrollment_id = getattr(thread, "enrollment_id", None)
    enrollment = await ts.get(EngagementEnrollment, enrollment_id) if enrollment_id else None
    if enrollment is None and referrer is not None:
        theirs = await ts.list(EngagementEnrollment, EngagementEnrollment.contact_id == referrer.id)
        enrollment = max(theirs, key=lambda e: e.created_at) if theirs else None
    return await ts.get(EngagementCampaign, enrollment.campaign_id) if enrollment else None


async def follow_through(ts, classification, *, name: str, email: str, user_id: str) -> dict:
    """Find or enrich the person, add them to the referrer's campaign, and draft the intro."""
    from nexus.core.db import utcnow
    from nexus.enrichment.waterfall import get_enricher
    from nexus.engagement.sequences.service import (
        CampaignError,
        draft_first_emails,
        enroll,
        steps_of,
    )
    from nexus.models.account import Contact
    from nexus.models.engagement import EngagementEnrollment

    name = " ".join((name or "").split())[:200]
    email = (email or "").strip().lower()
    if email and not EMAIL.fullmatch(email):
        raise ReferralError("That email address does not look right.")
    if not name and not email:
        raise ReferralError("Give the person's name or email address.")
    _message, referrer, account, _mailbox, _text = await _referral_context(ts, classification)
    if account is None:
        raise ReferralError("This reply is not linked to an account, so there is nowhere to add "
                            "the person it names.")
    campaign = await _campaign_for(ts, classification, referrer)
    if campaign is None:
        raise ReferralError("The person who replied is not in a campaign. Add the person they "
                            "named to one from Campaigns.")
    if not await steps_of(ts, campaign):
        raise ReferralError(f"{campaign.name} has no steps, so there is no intro to write. Add "
                            "a step to it first.")

    contact = await find_existing(ts, account, name=name, email=email,
                                  exclude_id=getattr(referrer, "id", None))
    if contact is None and not email and len(name.split()) < 2:
        raise ReferralError(f"Add {name}'s surname or email address, so the intro reaches the "
                            "right person.")
    if contact is None:
        contact = Contact(account_id=account.id, full_name=name or name_from_email(email) or email,
                          email=email or None)
        ts.add(contact)
        await ts.flush()
    if not (contact.email or "").strip():
        await get_enricher().enrich_contact(ts, contact, account, user_id=user_id,
                                            raise_on_block=True)
        if not (contact.email or "").strip():
            raise ReferralError(f"No email address was found for {contact.full_name}. Add it and "
                                "try again.")

    fields = dict(contact.custom_fields or {})
    fields["referred_by"] = {
        "contact_id": getattr(referrer, "id", None),
        "name": getattr(referrer, "full_name", "") or "",
        "title": getattr(referrer, "title", "") or "",
        "classification_id": classification.id,
        "at": utcnow().isoformat(),
    }
    contact.custom_fields = fields
    await ts.flush()

    try:
        added = await enroll(ts, campaign, [contact.id])
    except CampaignError as exc:
        raise ReferralError(str(exc)) from exc
    if not added.added:
        reason = (added.skipped[0]["reason"] if added.skipped else "")
        if reason == "already_enrolled":
            raise ReferralError(f"{contact.full_name} is already in {campaign.name}.")
        if reason.startswith("do_not_contact"):
            raise ReferralError(f"{contact.full_name} is on the do-not-contact list.")
        raise ReferralError(f"{contact.full_name} could not be added to {campaign.name}.")
    enrollment = await ts.first(EngagementEnrollment,
                                EngagementEnrollment.campaign_id == campaign.id,
                                EngagementEnrollment.contact_id == contact.id)
    drafted = await draft_first_emails(ts, campaign, user_id=user_id, only={enrollment.id})
    return {
        "contact_id": contact.id, "contact_name": contact.full_name, "email": contact.email,
        "campaign_id": campaign.id, "campaign_name": campaign.name,
        "enrollment_id": enrollment.id, "drafted": bool(drafted["drafted"]),
        "error": (drafted["errors"][0]["error"] if drafted["errors"] else ""),
        "warnings": added.warnings,
    }
```

- [ ] **Step 3: The referral in the prompt, and the signal kind** — apply to `nexus/engagement/drafting/context.py`:

```diff
diff --git a/nexus/engagement/drafting/context.py b/nexus/engagement/drafting/context.py
index d383608..80eabf8 100644
--- a/nexus/engagement/drafting/context.py
+++ b/nexus/engagement/drafting/context.py
@@ -151,4 +151,13 @@ async def build_context(ts, *, enrollment, contact, account, mailbox, step=None,
         person.append(f"- Seniority: {contact.seniority}")
     parts.append("THE PERSON\n" + "\n".join(person) + "\n")
+    referral = (contact.custom_fields or {}).get("referred_by") \
+        if isinstance(contact.custom_fields, dict) else None
+    referrer = (referral.get("name") or "").strip() if isinstance(referral, dict) else ""
+    if referrer:
+        # Who made the introduction, never what they wrote to us (spec §19).
+        title = (referral.get("title") or "").strip()
+        parts.append("REFERRAL\n- " + referrer + (f" ({title})" if title else "")
+                     + f" at {account.name} suggested we speak with this person.\n")
+        facts.append(f"{referrer.split()[0]} suggested we talk")
     parts.append("THE COMPANY\n" + account_facts(account) + "\n")
     rendered_signals = signal_facts(signals)
@@ -157,5 +166,7 @@ async def build_context(ts, *, enrollment, contact, account, mailbox, step=None,
 
     instructions = {
-        "first": "This is the FIRST email to this person.",
+        "first": "This is the FIRST email to this person." + (
+            f" Say in one short line that {referrer.split()[0]} suggested you get in touch; do "
+            "not quote or paraphrase anything else they said." if referrer else ""),
         "followup": ("This is a FOLLOW-UP in the same thread. Do not repeat the earlier email; add "
                      "one new, specific reason to reply. Never pretend they answered."),
@@ -163,4 +174,8 @@ async def build_context(ts, *, enrollment, contact, account, mailbox, step=None,
                      "one short line, and make it easy to pick the conversation back up."),
         "response": "They replied. Answer what they actually said, then propose one next step.",
+        "signal": ("Our earlier emails went unanswered, or they asked to talk later. Something new "
+                   "has happened at their company (the angle below): open with it in one line as "
+                   "the reason for writing now. Never pretend they replied, and if they asked for "
+                   "later, say you are early because of this news."),
     }.get(kind, "")
     angle = (getattr(step, "angle", "") or "").strip()
```

and to `nexus/engagement/drafting/drafter.py` (a signal email keeps the thread's subject):

```diff
diff --git a/nexus/engagement/drafting/drafter.py b/nexus/engagement/drafting/drafter.py
index 8f42d28..0fe9363 100644
--- a/nexus/engagement/drafting/drafter.py
+++ b/nexus/engagement/drafting/drafter.py
@@ -74,5 +74,5 @@ async def draft(ts, *, enrollment, contact, account, mailbox, step=None, kind: s
     subject = (output.get("subject") or "").strip()
     body = (output.get("body") or "").strip()
-    if kind in ("followup", "reengage", "response") and thread is not None:
+    if kind in ("followup", "reengage", "response", "signal") and thread is not None:
         # Exactly one "Re:", on the thread's own subject (D16): whatever the model wrote on the
         # subject line, a follow-up in a thread keeps the thread's subject.
```

- [ ] **Step 4: Draft only the new person, and refuse a campaign with no steps** — apply to `nexus/engagement/sequences/service.py`:

```diff
diff --git a/nexus/engagement/sequences/service.py b/nexus/engagement/sequences/service.py
index 4e57c0e..b81c4c6 100644
--- a/nexus/engagement/sequences/service.py
+++ b/nexus/engagement/sequences/service.py
@@ -243,6 +243,9 @@ async def set_status(ts, enrollment, status: str, reason: str | None = None, *,
 
 async def draft_first_emails(ts, campaign, *, user_id: str | None = None,
-                             limit: int | None = None) -> dict:
-    """Draft every step-0 email that has no draft yet. Idempotent: a second run drafts nothing."""
+                             limit: int | None = None, only: set[str] | None = None) -> dict:
+    """Draft every step-0 email that has no draft yet. Idempotent: a second run drafts nothing.
+
+    ``only`` limits it to those enrollments: a referral adds one person to a running campaign and
+    must not pay to draft anyone else who happens to be waiting."""
     from nexus.engagement.drafting.drafter import draft
     from nexus.models.account import Account, Contact
@@ -254,4 +257,8 @@ async def draft_first_emails(ts, campaign, *, user_id: str | None = None,
 
     steps = await steps_of(ts, campaign)
+    if not steps:
+        # Every campaign is created with a step, but a hand-built or migrated one may have none,
+        # and an opening email has nothing to be written from.
+        raise CampaignError("This campaign has no steps yet. Add one before drafting.")
     mailbox = await ts.get(MailboxConnection, campaign.mailbox_connection_id)
     pending = await ts.list(EngagementEnrollment, EngagementEnrollment.campaign_id == campaign.id,
@@ -260,4 +267,6 @@ async def draft_first_emails(ts, campaign, *, user_id: str | None = None,
     errors: list[dict] = []
     for enrollment in pending:
+        if only is not None and enrollment.id not in only:
+            continue
         if limit is not None and drafted >= limit:
             break
```

and map the refusal in `nexus/api/routers/engagement_campaigns.py`:

```diff
diff --git a/nexus/api/routers/engagement_campaigns.py b/nexus/api/routers/engagement_campaigns.py
index a9681dc..58912f7 100644
--- a/nexus/api/routers/engagement_campaigns.py
+++ b/nexus/api/routers/engagement_campaigns.py
@@ -331,9 +331,12 @@ async def draft_campaign(
 ) -> dict:
     """Draft up to ``limit`` first emails. The screen calls again until nothing is left."""
-    from nexus.engagement.sequences.service import draft_first_emails
+    from nexus.engagement.sequences.service import CampaignError, draft_first_emails
 
     campaign = await _campaign(ts, campaign_id, principal)
-    return await draft_first_emails(ts, campaign, user_id=principal.user_id,
-                                     limit=max(1, min(limit, 50)))
+    try:
+        return await draft_first_emails(ts, campaign, user_id=principal.user_id,
+                                         limit=max(1, min(limit, 50)))
+    except CampaignError as exc:
+        raise _refuse(exc) from exc
 
 
```

- [ ] **Step 5: Run** the Step 1 selection — expected PASS.

---

### Task 3: CRM activity logging

**Files:** Create `nexus/engagement/enhancements/crm_log.py`; modify `nexus/workers/tasks.py`, `scheduler.py`.

- [ ] **Step 1: Implement** `nexus/engagement/enhancements/crm_log.py`:

```python
"""CRM activity logging (spec §19): the emails an SDR sends, the replies they get and the meetings
they book, written to the workspace's HubSpot or Salesforce so nobody logs them by hand.

**The same connector, and the same records, as the account sync.** The worker resolves the
connector exactly as `handle_sync_crm_due_accounts` does, and an activity is written only against an
account that sync has already put in that CRM (`accounts.crm_id`, with `crm_source` naming the same
CRM). An account the CRM does not know yet waits, and its activities follow once it is there, while
they are still inside the look-back. Creating a CRM company here would be a second sync path with
its own idea of what a company is; writing against a `crm_id` from a CRM the workspace has since
left would attach the note to a record in the wrong system.

**Once each.** A row is marked `crm_logged_at` only when the CRM accepted it, and a failure is tried
again on the next tick. The one duplicate this cannot rule out is a push the CRM accepted whose mark
was then lost with the transaction.

**Nothing old is backfilled.** Seven days back, so switching the log on does not pour a year of
history into a customer's CRM in one sweep.

**A reply is a person writing back.** Out-of-office and other automatic answers are not activities
anyone wants in a CRM, and a bounce is a delivery failure, not a reply.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

LOOKBACK = timedelta(days=7)
BATCH = 200
NOT_A_REPLY = ("out_of_office", "other_auto")


@dataclass(slots=True)
class Activity:
    kind: str                  # email_sent | email_reply | meeting_booked
    row: object                # what gets marked: the message, or the classification for a meeting
    account_id: str | None
    contact_id: str | None
    subject: str
    at: datetime


async def pending(ts, *, now: datetime) -> list[Activity]:
    """What has happened in the look-back and is not in the CRM yet, oldest first."""
    from nexus.models.account import Contact
    from nexus.models.engagement import EngagementMessage, ReplyClassification

    since = now - LOOKBACK
    sent = await ts.list(EngagementMessage, EngagementMessage.direction == "out",
                         EngagementMessage.status == "sent",
                         EngagementMessage.crm_logged_at.is_(None),
                         EngagementMessage.sent_at >= since)
    received = await ts.list(EngagementMessage, EngagementMessage.direction == "in",
                             EngagementMessage.status == "received",
                             EngagementMessage.crm_logged_at.is_(None),
                             EngagementMessage.received_at >= since)
    readings = {c.message_id: c for c in await ts.list(
        ReplyClassification, ReplyClassification.message_id.in_([m.id for m in received]))} \
        if received else {}
    replies = [m for m in received if m.id in readings
               and (readings[m.id].corrected_category or readings[m.id].category) not in NOT_A_REPLY]
    meetings = await ts.list(ReplyClassification, ReplyClassification.decision == "meeting",
                             ReplyClassification.crm_logged_at.is_(None),
                             ReplyClassification.decided_at >= since)

    contact_ids = {m.contact_id for m in sent + replies if m.contact_id}
    accounts_of = {c.id: c.account_id for c in await ts.list(
        Contact, Contact.id.in_(contact_ids))} if contact_ids else {}
    out = [Activity("email_sent", m, accounts_of.get(m.contact_id), m.contact_id, m.subject,
                    m.sent_at) for m in sent]
    out += [Activity("email_reply", m, readings[m.id].account_id or accounts_of.get(m.contact_id),
                     m.contact_id, m.subject, m.received_at) for m in replies]
    out += [Activity("meeting_booked", c, c.account_id, c.contact_id, "", c.decided_at)
            for c in meetings]
    return sorted(out, key=lambda a: a.at)


def describe(activity: Activity, contact_name: str) -> str:
    """The line the CRM shows. The subject is the email's own; nothing else of its text leaves."""
    who = contact_name or "a contact"
    if activity.kind == "email_sent":
        return f"Email to {who}: {activity.subject}".strip()
    if activity.kind == "email_reply":
        return f"Reply from {who}: {activity.subject}".strip()
    return f"Meeting booked with {who}"


async def log_pending(ts, connector, *, now: datetime) -> dict:
    """Write what is pending through ``connector``. Never raises: a connector answers with a
    result, and a CRM that is down is tried again on the next tick."""
    from nexus.models.account import Account, Contact

    activities = (await pending(ts, now=now))[:BATCH]
    account_ids = {a.account_id for a in activities if a.account_id}
    contact_ids = {a.contact_id for a in activities if a.contact_id}
    accounts = {a.id: a for a in await ts.list(Account, Account.id.in_(account_ids))} \
        if account_ids else {}
    contacts = {c.id: c for c in await ts.list(Contact, Contact.id.in_(contact_ids))} \
        if contact_ids else {}
    logged = waiting = failed = 0
    for activity in activities:
        account = accounts.get(activity.account_id)
        if account is None or not account.crm_id or account.crm_source != connector.source:
            waiting += 1
            continue
        contact = contacts.get(activity.contact_id)
        result = await connector.push_activity(
            account_id=account.crm_id, kind=activity.kind,
            detail={"subject": describe(activity, getattr(contact, "full_name", "") or ""),
                    "email": getattr(contact, "email", "") or "",
                    "at": activity.at.isoformat() if activity.at else ""})
        if result.ok:
            activity.row.crm_logged_at = now
            logged += 1
        else:
            failed += 1
    await ts.flush()
    return {"logged": logged, "waiting": waiting, "failed": failed}
```

- [ ] **Step 2: The sweep** — apply to `nexus/workers/tasks.py`:

```diff
diff --git a/nexus/workers/tasks.py b/nexus/workers/tasks.py
index 36d1be2..6e748e3 100644
--- a/nexus/workers/tasks.py
+++ b/nexus/workers/tasks.py
@@ -1116,4 +1116,63 @@ async def handle_sync_mailboxes(payload: dict) -> dict:
 
 
+async def handle_log_engagement_crm(payload: dict) -> dict:
+    """Write sent emails, replies and booked meetings to each workspace's CRM (spec §19).
+
+    Behind the same two switches as every other CRM write: the deployment's `crm_sync_enabled`
+    ("Push to CRM") and the workspace's `automation_enabled`. Dark with the engagement engine. The
+    connector is resolved once per workspace, inside that workspace's session, for the reason
+    `handle_sync_crm_due_accounts` gives: resolving once for the sweep sent every tenant's writes
+    to whichever CRM the deployment named.
+    """
+    from sqlalchemy import select
+
+    from nexus.core.config import get_settings
+    from nexus.core.db import get_platform_sessionmaker, utcnow
+    from nexus.engagement import config
+    from nexus.engagement.enhancements.crm_log import LOOKBACK, log_pending
+    from nexus.ingestion import crm_credentials
+    from nexus.models.engagement import EngagementMessage, ReplyClassification
+    from nexus.models.identity import Tenant
+
+    if not get_settings().crm_sync_enabled:
+        return {"skipped": "crm_sync_disabled"}
+    if not config.campaigns_enabled():
+        return {"skipped": "engagement campaigns are switched off"}
+    now = utcnow()
+    since = now - LOOKBACK
+    async with get_platform_sessionmaker()() as session:
+        opted_in = select(Tenant.id).where(Tenant.automation_enabled == True)  # noqa: E712
+        tenants = set((await session.execute(
+            select(EngagementMessage.tenant_id).distinct()
+            .where(EngagementMessage.tenant_id.in_(opted_in))
+            .where(EngagementMessage.crm_logged_at.is_(None))
+            .where((EngagementMessage.sent_at >= since) | (EngagementMessage.received_at >= since))
+        )).scalars().all())
+        tenants |= set((await session.execute(
+            select(ReplyClassification.tenant_id).distinct()
+            .where(ReplyClassification.tenant_id.in_(opted_in))
+            .where(ReplyClassification.decision == "meeting")
+            .where(ReplyClassification.crm_logged_at.is_(None))
+            .where(ReplyClassification.decided_at >= since))).scalars().all())
+    totals = {"tenants": 0, "logged": 0, "waiting": 0, "failed": 0}
+    for tenant_id in sorted(tenants):
+        try:
+            async with tenant_session(tenant_id) as ts:
+                connector = await crm_credentials.resolve_crm_connector(ts)
+                result = await log_pending(ts, connector, now=now)
+        except Exception:
+            logger.warning("CRM activity log failed for tenant %s", tenant_id, exc_info=True)
+            continue
+        totals["tenants"] += 1
+        for key in ("logged", "waiting", "failed"):
+            totals[key] += result[key]
+    return totals
+
+
+async def enqueue_log_engagement_crm(*, queue: TaskQueue | None = None) -> None:
+    queue = queue or get_task_queue()
+    await queue.enqueue(Job(name="log_engagement_crm", payload={}))
+
+
 async def enqueue_sync_mailbox(tenant_id: str, mailbox_id: str, *,
                               queue: TaskQueue | None = None) -> None:
@@ -1202,4 +1261,5 @@ HANDLERS: dict[str, Handler] = {
     "sync_mailbox": handle_sync_mailbox,
     "sync_mailboxes": handle_sync_mailboxes,
+    "log_engagement_crm": handle_log_engagement_crm,
     "remind_replies": handle_remind_replies,
     "build_ledger_datasets": handle_build_ledger_datasets,
```

and schedule it in `nexus/workers/scheduler.py`, inside the engine's block and behind "Push to CRM":

```diff
diff --git a/nexus/workers/scheduler.py b/nexus/workers/scheduler.py
index f36f17f..85ee2aa 100644
--- a/nexus/workers/scheduler.py
+++ b/nexus/workers/scheduler.py
@@ -27,4 +27,5 @@ from nexus.workers.tasks import (
     enqueue_remind_replies,
     enqueue_sync_mailboxes,
+    enqueue_log_engagement_crm,
     enqueue_alert_digests,
     enqueue_expire_trials,
@@ -120,4 +121,8 @@ async def _enqueue_due(queue: TaskQueue) -> int:
                 await enqueue_remind_replies(queue=queue)
                 count += 3
+                if settings.crm_sync_enabled:
+                    # Sent emails, replies and meetings into each workspace's CRM (§19).
+                    await enqueue_log_engagement_crm(queue=queue)
+                    count += 1
             if settings.automation_enabled:
                 await enqueue_advance_cadences(queue=queue)
```

- [ ] **Step 3: Run** `pytest tests/test_engagement_enhancements.py -n0 -q -k crm` — expected PASS.

---

### Task 4: Signal re-engagement

**Files:** Create `nexus/engagement/enhancements/signal_reengage.py`; modify `nexus/engagement/reports/today.py`.

- [ ] **Step 1: Implement** `nexus/engagement/enhancements/signal_reengage.py`:

```python
"""Signal re-engagement (spec §19): when something happens at a company, suggest getting back in
touch with the people there who went quiet, or who asked for later.

**A suggestion, never a send** (D22). The SDR sees who and why, reads a drafted email in the same
thread, edits it, and presses Send.

Who is suggested, all of it decided here and nowhere else:

* A sequence that **finished with no reply** (`completed`), or a **"later" still more than two weeks
  from its date**. Nearer than that the re-engagement they asked for is about to go anyway, and two
  emails in a fortnight is one too many.
* **Never someone who declined or unsubscribed** (their enrollments are `stopped`, which is not
  eligible), never anyone on do-not-contact, never anyone emailed in the last 14 days, and never
  anyone another campaign is already going to email (a live enrollment elsewhere).
* One suggestion per person: their account's strongest recent signal. A signal counts when it is of
  a kind an SDR would open with, at or above the alert floor, and happened **after** the sequence
  ended (or after they asked for later) and within the last 14 days. News from before they went
  quiet is not a reason to write now.
* **Once per signal.** The send's idempotency key is the enrollment and the signal, so a second
  press, a retry or a colleague on the same screen cannot send it twice.

Ranked by reply likelihood band (phase 13), then by how recent the signal is.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

RELEVANT = ("funding", "hiring", "job_posting", "tech_install", "job_switch", "news",
            "website_change", "web_visit")
WINDOW = timedelta(days=14)
LATER_MARGIN = timedelta(days=14)
MAX_SUGGESTIONS = 20
#: Enrollments that will email the person on their own; a suggestion would be a second voice.
LIVE = ("active", "awaiting_review", "paused", "snoozed")
_BAND_RANK = {"high": 0, "medium": 1, "low": 2, "unknown": 3}


def idempotency_key(enrollment_id: str, signal_id: str) -> str:
    return f"signal:{enrollment_id}:{signal_id}"


@dataclass(slots=True)
class Suggestion:
    enrollment_id: str
    contact_id: str
    contact_name: str
    account_id: str
    account_name: str
    campaign_id: str
    campaign_name: str
    reason: str               # quiet | later
    signal_id: str
    signal_kind: str
    signal_title: str
    signal_at: datetime
    likelihood: str

    def as_dict(self) -> dict:
        return asdict(self)


def _aware(moment: datetime | None) -> datetime | None:
    from datetime import UTC

    if moment is None or moment.tzinfo is not None:
        return moment
    return moment.replace(tzinfo=UTC)


async def suggestions(ts, *, user_id: str, now: datetime) -> list[Suggestion]:
    from nexus.core.config import get_settings
    from nexus.engagement.insights.service import for_contacts
    from nexus.engagement.suppression.service import active_block
    from nexus.models.account import Account, Contact
    from nexus.models.engagement import (
        EngagementCampaign,
        EngagementEnrollment,
        EngagementMessage,
        MailboxConnection,
    )
    from nexus.models.signal import SignalEvent

    mailbox_ids = [m.id for m in await ts.list(MailboxConnection,
                                               MailboxConnection.owner_user_id == user_id)]
    if not mailbox_ids:
        return []
    rows = await ts.list(EngagementEnrollment,
                         EngagementEnrollment.mailbox_connection_id.in_(mailbox_ids),
                         EngagementEnrollment.status.in_(("completed", "snoozed")))
    later = [e for e in rows if e.status == "snoozed" and e.status_reason == "later"
             and e.snoozed_until is not None and _aware(e.snoozed_until) > now + LATER_MARGIN]
    # "After they asked for later" is measured from what they wrote, not from `updated_at`, which
    # any edit to the enrollment moves.
    heard: dict[str, datetime] = {}
    if later:
        for m in await ts.list(EngagementMessage, EngagementMessage.direction == "in",
                               EngagementMessage.contact_id.in_({e.contact_id for e in later})):
            at = _aware(m.received_at or m.created_at)
            if m.contact_id not in heard or at > heard[m.contact_id]:
                heard[m.contact_id] = at
    eligible = [(e, "quiet", _aware(e.finished_at)) for e in rows if e.status == "completed"]
    eligible += [(e, "later", heard.get(e.contact_id)) for e in later]
    if not eligible:
        return []

    since = now - WINDOW
    account_ids = {e.account_id for e, _, _ in eligible}
    signals = await ts.list(SignalEvent, SignalEvent.account_id.in_(account_ids),
                            SignalEvent.kind.in_(RELEVANT),
                            SignalEvent.strength >= get_settings().signal_alert_floor,
                            SignalEvent.occurred_at >= since)
    if not signals:
        return []
    by_account: dict[str, list] = {}
    for s in signals:
        by_account.setdefault(s.account_id, []).append(s)
    contact_ids = {e.contact_id for e, _, _ in eligible}
    recently = {m.contact_id for m in await ts.list(
        EngagementMessage, EngagementMessage.contact_id.in_(contact_ids),
        EngagementMessage.direction == "out", EngagementMessage.status == "sent",
        EngagementMessage.sent_at >= since)}
    # Someone another campaign is already about to email is not someone to write to again.
    eligible_ids = {e.id for e, _, _ in eligible}
    recently |= {e.contact_id for e in await ts.list(
        EngagementEnrollment, EngagementEnrollment.contact_id.in_(contact_ids),
        EngagementEnrollment.status.in_(LIVE)) if e.id not in eligible_ids}
    contacts = {c.id: c for c in await ts.list(Contact, Contact.id.in_(contact_ids))}
    accounts = {a.id: a for a in await ts.list(Account, Account.id.in_(account_ids))}
    campaigns = {c.id: c for c in await ts.list(
        EngagementCampaign,
        EngagementCampaign.id.in_({e.campaign_id for e, _, _ in eligible}))}

    picked: list[tuple] = []
    for enrollment, reason, anchor in eligible:
        contact = contacts.get(enrollment.contact_id)
        if contact is None or getattr(contact, "deleted_at", None) is not None \
                or not (contact.email or "").strip() or contact.id in recently:
            continue
        fresh = [s for s in by_account.get(enrollment.account_id, [])
                 if anchor is None or _aware(s.occurred_at) > anchor]
        if not fresh:
            continue
        if await active_block(ts, contact.email) is not None:
            continue
        best = max(fresh, key=lambda s: (s.strength, _aware(s.occurred_at)))
        picked.append((enrollment, reason, best, contact))
    if not picked:
        return []

    bands = {i.contact_id: i.likelihood.band for i in await for_contacts(
        ts, [contact.id for _, _, _, contact in picked], now=now)}
    out = [Suggestion(
        enrollment_id=e.id, contact_id=contact.id, contact_name=contact.full_name,
        account_id=e.account_id, account_name=getattr(accounts.get(e.account_id), "name", ""),
        campaign_id=e.campaign_id, campaign_name=getattr(campaigns.get(e.campaign_id), "name", ""),
        reason=reason, signal_id=s.id, signal_kind=s.kind, signal_title=s.title,
        signal_at=_aware(s.occurred_at), likelihood=bands.get(contact.id, "unknown"))
        for e, reason, s, contact in picked]
    out.sort(key=lambda x: (_BAND_RANK.get(x.likelihood, 3), -x.signal_at.timestamp()))
    return out[:MAX_SUGGESTIONS]


class RestartError(ValueError):
    """Why this suggestion cannot be acted on now."""


async def _load(ts, *, user_id: str, enrollment_id: str, signal_id: str, now: datetime):
    """The suggestion, re-derived: a stale screen must not act on someone no longer eligible."""
    from nexus.models.account import Account, Contact
    from nexus.models.engagement import EngagementEnrollment, EngagementThread, MailboxConnection
    from nexus.models.signal import SignalEvent

    current = {(s.enrollment_id, s.signal_id) for s in await suggestions(ts, user_id=user_id,
                                                                         now=now)}
    if (enrollment_id, signal_id) not in current:
        raise RestartError("This suggestion no longer applies: they may have been emailed, "
                           "replied, or asked not to be contacted.")
    enrollment = await ts.get(EngagementEnrollment, enrollment_id)
    signal = await ts.get(SignalEvent, signal_id)
    contact = await ts.get(Contact, enrollment.contact_id)
    account = await ts.get(Account, enrollment.account_id)
    mailbox = await ts.get(MailboxConnection, enrollment.mailbox_connection_id)
    thread = await ts.get(EngagementThread, enrollment.current_thread_id) \
        if enrollment.current_thread_id else None
    return enrollment, signal, contact, account, mailbox, thread


@dataclass(slots=True)
class _Angle:
    angle: str


async def draft(ts, *, user_id: str, enrollment_id: str, signal_id: str, now: datetime) -> dict:
    """An email in the same thread that opens with the news. Charged as `ai.email_draft`."""
    from nexus.engagement.drafting.drafter import draft as write

    enrollment, signal, contact, account, mailbox, thread = await _load(
        ts, user_id=user_id, enrollment_id=enrollment_id, signal_id=signal_id, now=now)
    written = await write(ts, enrollment=enrollment, contact=contact, account=account,
                          mailbox=mailbox, step=_Angle(f"The news: {signal.title}"),
                          kind="signal", thread=thread, user_id=user_id, now=now)
    if not written.ok:
        raise RestartError(f"The email could not be drafted: {written.error}")
    return {"subject": written.subject, "body": written.body,
            "quality_problems": written.problems}


async def send(ts, *, user_id: str, enrollment_id: str, signal_id: str, subject: str, body: str,
               now: datetime):
    """Send the SDR's text in the same thread. The enrollment itself does not restart: this is one
    email, and a reply to it reaches the desk like any other."""
    from nexus.engagement.sending.service import send as deliver

    if not subject.strip() or not body.strip():
        raise RestartError("Write a subject and a message first.")
    enrollment, signal, contact, _account, mailbox, thread = await _load(
        ts, user_id=user_id, enrollment_id=enrollment_id, signal_id=signal_id, now=now)
    return await deliver(ts, mailbox=mailbox, contact=contact, subject=subject.strip(),
                         body=body.strip(), thread=thread, kind="reengage", user_id=user_id,
                         idempotency_key=idempotency_key(enrollment.id, signal.id),
                         context={"signal_id": signal.id, "enrollment_id": enrollment.id})
```

- [ ] **Step 2: Today** — one line for everyone worth writing to again, between the opening emails to approve and the people returning today; review items open the campaign's Review tab. Apply to `nexus/engagement/reports/today.py`:

```diff
diff --git a/nexus/engagement/reports/today.py b/nexus/engagement/reports/today.py
index 7d1e4e4..a6fe79c 100644
--- a/nexus/engagement/reports/today.py
+++ b/nexus/engagement/reports/today.py
@@ -4,5 +4,6 @@ One ordered list per SDR, built from what the engine already knows. The order is
 if left: a buyer who said yes and is waiting goes cold fastest, then replies only a person can
 decide, then colleagues a reply paused, then calls due, then opening emails waiting for approval,
-then the people coming back today, who need nothing but are worth knowing about.
+then people worth writing to again because something happened at their company, then the people
+coming back today, who need nothing but are worth knowing about.
 
 Within a kind, ranked by reply likelihood (§18.5), and by its BAND rather than its score: people in
@@ -20,5 +21,5 @@ from datetime import UTC, datetime, timedelta
 
 #: The order kinds appear in, and what each one asks of the SDR.
-KINDS = ("reply", "decide", "colleagues", "call", "review", "returning")
+KINDS = ("reply", "decide", "colleagues", "call", "review", "restart", "returning")
 #: Likelihood bands, likeliest first. `unknown` (nothing to go on) ranks with the unrated items.
 _BAND_RANK = {"high": 0, "medium": 1, "low": 2, "unknown": 3}
@@ -152,5 +153,13 @@ async def today(ts, *, user_id: str, now: datetime) -> list[TodayItem]:
                     "review", f"Review {n} opening {'email' if n == 1 else 'emails'} in {campaign.name}",
                     "Nothing sends until you approve it.",
-                    f"/engagement/campaigns/{campaign.id}", campaign.created_at, count=n))
+                    f"/engagement/campaigns/{campaign.id}?tab=review", campaign.created_at, count=n))
+
+    restart = await _worth_writing_again(ts, user_id=user_id, now=now)
+    if restart:
+        n = len(restart)
+        items.append(TodayItem(
+            "restart", f"Write again to {n} {'person' if n == 1 else 'people'}",
+            "Something happened at their company since they went quiet or asked for later.",
+            "/engagement/replies?tab=restart", restart[0].signal_at, count=n))
 
     for e in sorted(returning, key=lambda e: _aware(e.snoozed_until)):
@@ -166,4 +175,15 @@ async def today(ts, *, user_id: str, now: datetime) -> list[TodayItem]:
 
 
+async def _worth_writing_again(ts, *, user_id: str, now: datetime) -> list:
+    """Signal re-engagement suggestions (§19). A suggestion is optional, so failing to build them
+    leaves them off the list rather than taking the list down."""
+    from nexus.engagement.enhancements.signal_reengage import suggestions
+
+    try:
+        return await suggestions(ts, user_id=user_id, now=now)
+    except Exception:  # noqa: BLE001 - optional line on a list that must load
+        return []
+
+
 async def _likelihood_bands(ts, contact_ids: list[str], now: datetime) -> dict[str, str]:
     """Each person's reply-likelihood band. The Today plan must load even when insights cannot:
```

- [ ] **Step 3: Run** `pytest tests/test_engagement_enhancements.py tests/test_engagement_reporting.py -n0 -q` — expected PASS.

---

### Task 5: The routes

- [ ] **Step 1: Referral routes on the desk** — apply to `nexus/api/routers/engagement_desk.py`:

```diff
diff --git a/nexus/api/routers/engagement_desk.py b/nexus/api/routers/engagement_desk.py
index ab1a2f5..190ea64 100644
--- a/nexus/api/routers/engagement_desk.py
+++ b/nexus/api/routers/engagement_desk.py
@@ -399,2 +399,51 @@ async def colleague(
     except (service.DeskError, ValueError) as exc:
         raise _refuse(exc) from exc
+
+
+# ---- referral follow-through (spec §19) ----------------------------------------------------------
+
+class ReferralCandidateOut(BaseModel):
+    name: str
+    email: str
+    evidence: str
+    contact_id: str | None
+    contact_name: str | None
+    contact_email: str | None
+
+
+class ReferralIn(BaseModel):
+    model_config = {"extra": "forbid"}
+
+    name: str = ""
+    email: str = ""
+
+
+@router.get("/{classification_id}/referral", response_model=list[ReferralCandidateOut])
+async def referral_candidates(
+    classification_id: str,
+    ts: TenantSession = Depends(get_tenant_session),
+    principal: Principal = Depends(require(Permission.run_engagement)),
+) -> list[ReferralCandidateOut]:
+    """Who the reply points to. Reading costs nothing; only Draft intro spends."""
+    from nexus.engagement.enhancements import referral
+
+    classification = await _classification(ts, classification_id, principal)
+    return [ReferralCandidateOut(**c) for c in await referral.candidates(ts, classification)]
+
+
+@router.post("/{classification_id}/referral")
+async def referral_follow_through(
+    classification_id: str, body: ReferralIn,
+    ts: TenantSession = Depends(get_tenant_session),
+    principal: Principal = Depends(require(Permission.run_engagement)),
+) -> dict:
+    """Find or enrich the person named, add them to the referrer's campaign, draft the intro for
+    review. A plan that cannot pay for the enrichment gets the 402 with its upsell."""
+    from nexus.engagement.enhancements import referral
+
+    classification = await _classification(ts, classification_id, principal)
+    try:
+        return await referral.follow_through(ts, classification, name=body.name,
+                                             email=body.email, user_id=principal.user_id)
+    except referral.ReferralError as exc:
+        raise _refuse(exc) from exc
```

- [ ] **Step 2: Create** `nexus/api/routers/engagement_restart.py`:

```python
"""Signal re-engagement (spec §19): people worth writing to again because something happened at
their company, a drafted email in the same thread, and the SDR's send.

Dark with the rest of the engine. Always the caller's own mailboxes: a suggestion is about a
conversation, and a conversation belongs to whoever is having it.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from nexus.api.deps import Principal, get_tenant_session, require
from nexus.api.routers.engagement_campaigns import require_campaigns_enabled
from nexus.core.rbac import Permission
from nexus.core.tenancy import TenantSession

router = APIRouter(prefix="/engagement/restart", tags=["engagement"],
                   dependencies=[Depends(require_campaigns_enabled)])


class SuggestionOut(BaseModel):
    enrollment_id: str
    contact_id: str
    contact_name: str
    account_id: str
    account_name: str
    campaign_id: str
    campaign_name: str
    reason: str
    signal_id: str
    signal_kind: str
    signal_title: str
    signal_at: datetime
    likelihood: str


class MessageIn(BaseModel):
    model_config = {"extra": "forbid"}

    subject: str
    body: str


@router.get("", response_model=list[SuggestionOut])
async def list_suggestions(
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> list[SuggestionOut]:
    from nexus.core.db import utcnow
    from nexus.engagement.enhancements.signal_reengage import suggestions

    rows = await suggestions(ts, user_id=principal.user_id, now=utcnow())
    return [SuggestionOut(**s.as_dict()) for s in rows]


@router.post("/{enrollment_id}/{signal_id}/draft")
async def draft(
    enrollment_id: str, signal_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> dict:
    from nexus.core.db import utcnow
    from nexus.engagement.enhancements import signal_reengage

    try:
        return await signal_reengage.draft(ts, user_id=principal.user_id,
                                           enrollment_id=enrollment_id, signal_id=signal_id,
                                           now=utcnow())
    except signal_reengage.RestartError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.post("/{enrollment_id}/{signal_id}/send")
async def send(
    enrollment_id: str, signal_id: str, body: MessageIn,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> dict:
    from nexus.core.db import utcnow
    from nexus.engagement.enhancements import signal_reengage

    try:
        result = await signal_reengage.send(
            ts, user_id=principal.user_id, enrollment_id=enrollment_id, signal_id=signal_id,
            subject=body.subject, body=body.body, now=utcnow())
    except signal_reengage.RestartError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return {"outcome": result.outcome, "reason": result.reason, "message_id": result.message_id}
```

and register it in `nexus/api/routers/__init__.py`:

```diff
diff --git a/nexus/api/routers/__init__.py b/nexus/api/routers/__init__.py
index cc0b3c3..3353907 100644
--- a/nexus/api/routers/__init__.py
+++ b/nexus/api/routers/__init__.py
@@ -33,4 +33,5 @@ from nexus.api.routers import (
     engagement_desk,
     engagement_insights,
+    engagement_restart,
     engagement_reports,
     engagement_settings,
@@ -91,4 +92,5 @@ all_routers = [
     engagement_desk.router,
     engagement_insights.router,
+    engagement_restart.router,
     engagement_reports.router,
     engagement_settings.router,
```

- [ ] **Step 3: Run** `pytest tests/test_engagement_enhancements.py -n0 -q` — expected PASS (11).

- [ ] **Step 4: Commit**

```bash
git add migrations/versions/0058_engagement_crm_log.py nexus tests/test_engagement_enhancements.py
git commit -m "feat(engagement): referral follow-through, CRM activity log, signal re-engagement"
```

---

### Task 6: The screens

- [ ] **Step 1: Types and client** — `frontend/src/lib/types.ts`:

```diff
diff --git a/frontend/src/lib/types.ts b/frontend/src/lib/types.ts
index cc021fc..f746680 100644
--- a/frontend/src/lib/types.ts
+++ b/frontend/src/lib/types.ts
@@ -2532,5 +2532,6 @@ export interface ResponseTimeRow {
 }
 
-export type TodayKind = "reply" | "decide" | "colleagues" | "call" | "review" | "returning";
+export type TodayKind =
+  | "reply" | "decide" | "colleagues" | "call" | "review" | "restart" | "returning";
 
 export interface TodayItem {
@@ -2580,2 +2581,42 @@ export interface ContactInsight {
   best_time: BestTimeSuggestion;
 }
+
+// ---- engagement: enhancements (phase 14) -------------------------------------------------------
+
+/** Someone a referral reply points to, and whether the workspace already has them. */
+export interface ReferralCandidate {
+  name: string;
+  email: string;
+  evidence: string;
+  contact_id: string | null;
+  contact_name: string | null;
+  contact_email: string | null;
+}
+
+export interface ReferralResult {
+  contact_id: string;
+  contact_name: string;
+  email: string;
+  campaign_id: string;
+  campaign_name: string;
+  enrollment_id: string;
+  drafted: boolean;
+  error: string;
+}
+
+/** Someone worth writing to again because something happened at their company (spec §19). */
+export interface RestartSuggestion {
+  enrollment_id: string;
+  contact_id: string;
+  contact_name: string;
+  account_id: string;
+  account_name: string;
+  campaign_id: string;
+  campaign_name: string;
+  reason: "quiet" | "later";
+  signal_id: string;
+  signal_kind: string;
+  signal_title: string;
+  signal_at: string;
+  likelihood: ReplyLikelihood["band"];
+}
```

and `frontend/src/lib/api.ts`:

```diff
diff --git a/frontend/src/lib/api.ts b/frontend/src/lib/api.ts
index 96a09b3..90e2cec 100644
--- a/frontend/src/lib/api.ts
+++ b/frontend/src/lib/api.ts
@@ -98,4 +98,7 @@ import type {
   ContactInsight,
   BestTimeSuggestion,
+  ReferralCandidate,
+  ReferralResult,
+  RestartSuggestion,
   DoNotContactEntry,
   LedgerStatus,
@@ -1430,4 +1433,27 @@ export class ApiClient {
     });
   }
+  deskReferralCandidates(id: string, signal?: AbortSignal) {
+    return this.request<ReferralCandidate[]>(`/engagement/desk/${id}/referral`, { signal });
+  }
+  deskReferral(id: string, body: { name: string; email: string }) {
+    return this.request<ReferralResult>(`/engagement/desk/${id}/referral`, {
+      method: "POST", body,
+    });
+  }
+
+  // ---- engagement: signal re-engagement ----
+  restartSuggestions(signal?: AbortSignal) {
+    return this.request<RestartSuggestion[]>("/engagement/restart", { signal });
+  }
+  restartDraft(enrollmentId: string, signalId: string) {
+    return this.request<{ subject: string; body: string; quality_problems: string[] }>(
+      `/engagement/restart/${enrollmentId}/${signalId}/draft`, { method: "POST" },
+    );
+  }
+  restartSend(enrollmentId: string, signalId: string, body: { subject: string; body: string }) {
+    return this.request<{ outcome: string; reason: string; message_id: string }>(
+      `/engagement/restart/${enrollmentId}/${signalId}/send`, { method: "POST", body },
+    );
+  }
   rescheduleScheduled(enrollmentId: string, when: string) {
     return this.request<null>(`/engagement/desk/scheduled/${enrollmentId}/reschedule`, {
```

- [ ] **Step 2: The referral panel** — `frontend/src/pages/engagement/ReferralPanel.tsx`. Once the intro is drafted the form gives way to what happened and where to approve it; pressing Draft intro again could only be refused.

```tsx
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Button, Field, Icons, Input, WorkingIndicator } from "@/components/ui";
import { useToast } from "@/components/ui/Toast";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import type { ReferralCandidate, ReferralResult } from "@/lib/types";
import styles from "./Engagement.module.css";

/**
 * Referral follow-through (spec §19): the person a reply points to, added to the same campaign
 * with an intro that says who suggested it, waiting in the review queue.
 *
 * The server reads who was named (the Cc line first, then the reply's own words) and says whether
 * each is already a contact. Nothing is spent until Draft intro: then an address left blank is
 * looked up (an enrichment credit) and the intro is drafted (a draft credit). Nothing is sent: the
 * intro is approved in the campaign like every opening email.
 */
export function ReferralPanel({ id }: { id: string }) {
  const api = useApiClient();
  const toast = useToast();
  const named = useApi<ReferralCandidate[]>((s) => api.deskReferralCandidates(id, s), [id]);
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<ReferralResult | null>(null);

  useEffect(() => {
    const first = named.data?.[0];
    if (!first) return;
    setName(first.contact_name || first.name);
    setEmail(first.contact_email || first.email);
  }, [named.data]);

  async function draftIntro() {
    setBusy(true);
    try {
      const result = await api.deskReferral(id, { name: name.trim(), email: email.trim() });
      setDone(result);
      if (!result.drafted) {
        toast.error("Added, but the intro was not drafted", result.error || "Open the campaign to draft it again.");
      }
    } catch (err) {
      toast.error("That didn't work", err instanceof ApiError ? err.detail : "Please try again.");
    } finally {
      setBusy(false);
    }
  }

  function startOver() {
    setDone(null);
    setName("");
    setEmail("");
  }

  return (
    <section className={styles.answer} aria-labelledby="referral-title">
      <h3 id="referral-title" className={styles.sectionTitle}>They pointed you to someone</h3>

      {done ? (
        <>
          <p className={styles.notice} role="status">
            {done.contact_name} is in {done.campaign_name}
            {done.drafted ? ", with an intro waiting for your review." : ". Their intro still needs drafting."}
          </p>
          <div className={styles.formActions}>
            <Button variant="ghost" onClick={startOver}>Add someone else</Button>
            <Link to={`/engagement/campaigns/${done.campaign_id}?tab=review`} className={styles.buttonLink}>
              Review the intro
            </Link>
          </div>
        </>
      ) : (
        <>
          <p className={styles.muted}>
            Add them to the same campaign with an intro that says who suggested it. Nothing is sent
            until you approve it there.
          </p>

          {named.data && named.data.length > 0 && (
            <ul className={styles.candidates} aria-label="People named in the reply">
              {named.data.map((c) => {
                const shown = c.contact_name || c.name || c.email;
                const address = c.contact_email || c.email;
                const chosen = (c.contact_name || c.name) === name && (address || "") === email;
                return (
                  <li key={`${c.email}-${c.name}`}>
                    <button
                      type="button" className={styles.candidate} aria-pressed={chosen} disabled={busy}
                      onClick={() => { setName(c.contact_name || c.name); setEmail(address || ""); }}
                    >
                      <span className={styles.personName}>{shown}</span>
                      <span className={styles.muted}>
                        {[address, c.evidence, c.contact_id ? "already a contact" : ""].filter(Boolean).join(" · ")}
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
          {named.data?.length === 0 && (
            <p className={styles.muted}>Nobody is named clearly enough to pick out. Type who they suggested.</p>
          )}

          {busy ? (
            <WorkingIndicator label="Finding them and writing the intro" hint="Looking up an address takes longer than a draft alone." />
          ) : (
            <div className={styles.fields}>
              <Field label="Name">
                <Input value={name} onChange={(e) => setName(e.target.value)} autoComplete="off" />
              </Field>
              <Field label="Email" hint="Leave it blank to look it up, which uses an enrichment credit.">
                <Input type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="off" />
              </Field>
            </div>
          )}

          <div className={styles.formActions}>
            <Button iconLeft={<Icons.SparklesIcon />} onClick={draftIntro} loading={busy}
              disabled={busy || (!name.trim() && !email.trim())}>
              Draft intro
            </Button>
          </div>
        </>
      )}
    </section>
  );
}
```

- [ ] **Step 3: Write again** — `frontend/src/pages/engagement/WriteAgain.tsx`. Each suggestion reuses the review queue's card, because it is the same kind of thing: a person and a draft to check.

```tsx
import { useState } from "react";
import { Link } from "react-router-dom";
import {
  Badge, Button, EmptyState, ErrorState, Field, Icons, Input, Skeleton, Textarea, WorkingIndicator,
} from "@/components/ui";
import { useToast } from "@/components/ui/Toast";
import { LikelihoodBadge } from "@/components/engagement/InsightBadge";
import { whenDay } from "@/components/engagement/labels";
import type { AsyncState } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import type { RestartSuggestion } from "@/lib/types";
import styles from "./Engagement.module.css";

/**
 * Signal re-engagement (spec §19): people who went quiet, or asked for later, at a company where
 * something has happened since. The server decides who is fair to write to (never someone who
 * declined or unsubscribed, never twice for one piece of news); this lists them and drafts an
 * email in the same thread. Nothing is sent until Send.
 */

const REASON: Record<RestartSuggestion["reason"], string> = {
  quiet: "Went quiet",
  later: "Asked for later",
};

export function WriteAgain({ state, onChanged }: {
  state: AsyncState<RestartSuggestion[]>;
  onChanged: () => void;
}) {
  if (state.error) {
    return <ErrorState title="Couldn't load suggestions" message={state.error.detail} onRetry={state.refetch} />;
  }
  if (!state.data) {
    return (
      <div className={styles.stack}>
        {[0, 1, 2].map((i) => <Skeleton key={i} width="100%" height={96} />)}
      </div>
    );
  }
  if (state.data.length === 0) {
    return (
      <EmptyState icon={<Icons.SignalIcon />} title="Nobody to write to again yet"
        description="When something happens at a company whose people went quiet or asked for later, they appear here with a reason to get back in touch." />
    );
  }
  return (
    <ul className={styles.suggestions} aria-label="People worth writing to again">
      {state.data.map((s) => (
        <li key={`${s.enrollment_id}-${s.signal_id}`}>
          <Suggestion s={s} onSent={onChanged} />
        </li>
      ))}
    </ul>
  );
}

function Suggestion({ s, onSent }: { s: RestartSuggestion; onSent: () => void }) {
  const api = useApiClient();
  const toast = useToast();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState<"draft" | "send" | null>(null);
  const [subject, setSubject] = useState("");
  const [body, setBody] = useState("");
  const titleId = `restart-${s.enrollment_id}`;

  async function draft() {
    setOpen(true);
    setBusy("draft");
    try {
      const fresh = await api.restartDraft(s.enrollment_id, s.signal_id);
      setSubject(fresh.subject);
      setBody(fresh.body);
      if (fresh.quality_problems.length) toast.error("Check the draft", fresh.quality_problems.join("; "));
    } catch (err) {
      toast.error("That didn't work", err instanceof ApiError ? err.detail : "Please try again.");
    } finally {
      setBusy(null);
    }
  }

  async function send() {
    setBusy("send");
    try {
      const result = await api.restartSend(s.enrollment_id, s.signal_id, { subject, body });
      if (result.outcome !== "sent") throw new ApiError(409, `Not sent: ${result.reason || result.outcome}.`);
      toast.success("Sent", `In the same thread as your earlier emails to ${s.contact_name}.`);
      onSent();
    } catch (err) {
      toast.error("That didn't work", err instanceof ApiError ? err.detail : "Please try again.");
    } finally {
      setBusy(null);
    }
  }

  return (
    <article className={styles.reviewCard} aria-labelledby={titleId}>
      <header className={styles.suggestionHead}>
        <div className={styles.person}>
          <h3 id={titleId} className={styles.personName}>{s.contact_name}</h3>
          <span className={styles.muted}>
            {s.account_name}
            {" · "}
            <Link to={`/engagement/campaigns/${s.campaign_id}`}>{s.campaign_name}</Link>
          </span>
        </div>
        <span className={styles.badgeRow}>
          <Badge tone="neutral">{REASON[s.reason]}</Badge>
          <LikelihoodBadge band={s.likelihood} />
        </span>
      </header>
      <p className={styles.signalLine}>
        <Icons.SignalIcon aria-hidden className={styles.inlineIcon} />
        <span className={styles.signalText}>
          <span>{s.signal_title}</span>
          <time className={styles.muted} dateTime={s.signal_at}>{whenDay(s.signal_at)}</time>
        </span>
      </p>

      {open && (busy === "draft" ? (
        <WorkingIndicator label="Reading the thread and writing the email" hint="Drafts usually take a few seconds." />
      ) : (
        <>
          <Field label="Subject">
            <Input value={subject} onChange={(e) => setSubject(e.target.value)} />
          </Field>
          <Field label="Email" hint="Your signature is added from your mailbox when it sends.">
            <Textarea rows={7} value={body} onChange={(e) => setBody(e.target.value)} />
          </Field>
        </>
      ))}

      <div className={styles.formActions}>
        <Button variant="secondary" iconLeft={<Icons.SparklesIcon />} onClick={draft}
          disabled={busy !== null} loading={busy === "draft"}>
          {body ? "Draft again" : "Draft email"}
        </Button>
        {open && (
          <Button iconLeft={<Icons.SendIcon />} onClick={send}
            disabled={busy !== null || !subject.trim() || !body.trim()} loading={busy === "send"}>
            Send
          </Button>
        )}
      </div>
    </article>
  );
}
```

`LikelihoodBadge` is exported from `InsightBadge.tsx` for a list that has only the band:

```diff
diff --git a/frontend/src/components/engagement/InsightBadge.tsx b/frontend/src/components/engagement/InsightBadge.tsx
index b0369b1..7b1e979 100644
--- a/frontend/src/components/engagement/InsightBadge.tsx
+++ b/frontend/src/components/engagement/InsightBadge.tsx
@@ -33,4 +33,11 @@ export function useContactInsights(contactIds: string[]): Map<string, ContactIns
 }
 
+/** The likelihood band alone, for a list that already has the band and nothing else. */
+export function LikelihoodBadge({ band }: { band: ReplyLikelihood["band"] }) {
+  if (band === "unknown") return null;
+  const like = LIKELIHOOD[band];
+  return <Badge tone={like.tone}>{like.label}</Badge>;
+}
+
 /** Whether the full (non-compact) badge would show anything for this insight. */
 export function hasInsight(insight?: ContactInsight): boolean {
```

- [ ] **Step 4: Wire them in** — `frontend/src/pages/engagement/ReplyDeskPage.tsx`:

```diff
diff --git a/frontend/src/pages/engagement/ReplyDeskPage.tsx b/frontend/src/pages/engagement/ReplyDeskPage.tsx
index 681cf83..96db906 100644
--- a/frontend/src/pages/engagement/ReplyDeskPage.tsx
+++ b/frontend/src/pages/engagement/ReplyDeskPage.tsx
@@ -18,6 +18,9 @@ import { ApiError } from "@/lib/api";
 import type {
   DeskDecision, DeskItemDetail, DeskQueueItem, DeskScheduledItem, Member, ReplyCategory,
+  RestartSuggestion,
 } from "@/lib/types";
 import styles from "./Engagement.module.css";
+import { ReferralPanel } from "./ReferralPanel";
+import { WriteAgain } from "./WriteAgain";
 
 /**
@@ -26,9 +29,10 @@ import styles from "./Engagement.module.css";
  *
  * Needs action holds what a person must answer or decide. Scheduled holds people who asked to hear
- * back on a date or are out of office; their date can be moved or cancelled. Handled is the record
- * of who declined, unsubscribed or was closed, and why.
+ * back on a date or are out of office; their date can be moved or cancelled. Write again holds people
+ * who went quiet or asked for later, at a company where something has happened since (§19). Handled
+ * is the record of who declined, unsubscribed or was closed, and why.
  */
 
-type TabKey = "needs_action" | "scheduled" | "handled";
+type TabKey = "needs_action" | "scheduled" | "restart" | "handled";
 
 function replySubject(subject: string): string {
@@ -57,4 +61,5 @@ export function ReplyDeskPage() {
   );
   const scheduled = useApi<DeskScheduledItem[]>((s) => api.deskScheduled(team, s), [team]);
+  const restart = useApi<RestartSuggestion[]>((s) => api.restartSuggestions(s), []);
 
   function go(next: Partial<{ tab: TabKey; reply: string | null }>) {
@@ -69,4 +74,5 @@ export function ReplyDeskPage() {
     { value: "needs_action", label: "Needs action", count: open.data?.length },
     { value: "scheduled", label: "Scheduled", count: scheduled.data?.length },
+    { value: "restart", label: "Write again", count: restart.data?.length },
     { value: "handled", label: "Handled" },
   ];
@@ -110,4 +116,8 @@ export function ReplyDeskPage() {
       </TabPanel>
 
+      <TabPanel id="desk-panel-restart" active={tab === "restart"}>
+        <WriteAgain state={restart} onChanged={restart.refetch} />
+      </TabPanel>
+
       <TabPanel id="desk-panel-handled" active={tab === "handled"}>
         <Queue
@@ -355,4 +365,6 @@ function ReplyDetail({ id, onDone, canAssign }: { id: string; onDone: () => void
       )}
 
+      {openItem && category === "referral" && <ReferralPanel id={id} />}
+
       {d.paused_colleagues.length > 0 && (
         <section className={styles.colleagues} aria-labelledby="colleagues-title">
```

`frontend/src/components/engagement/TodayPlan.tsx`:

```diff
diff --git a/frontend/src/components/engagement/TodayPlan.tsx b/frontend/src/components/engagement/TodayPlan.tsx
index 45fe542..5b733c9 100644
--- a/frontend/src/components/engagement/TodayPlan.tsx
+++ b/frontend/src/components/engagement/TodayPlan.tsx
@@ -21,4 +21,5 @@ const KIND: Record<TodayKind, { label: string; icon: JSX.Element }> = {
   call: { label: "Call", icon: <Icons.PhoneIcon /> },
   review: { label: "Review", icon: <Icons.CheckIcon /> },
+  restart: { label: "Write again", icon: <Icons.SignalIcon /> },
   returning: { label: "Returning", icon: <Icons.RefreshIcon /> },
 };
```

and the campaign page opens the tab a link asks for (`?tab=review`), in `frontend/src/pages/engagement/CampaignDetailPage.tsx`:

```diff
diff --git a/frontend/src/pages/engagement/CampaignDetailPage.tsx b/frontend/src/pages/engagement/CampaignDetailPage.tsx
index a28793f..a00af59 100644
--- a/frontend/src/pages/engagement/CampaignDetailPage.tsx
+++ b/frontend/src/pages/engagement/CampaignDetailPage.tsx
@@ -1,4 +1,4 @@
 import { useEffect, useMemo, useState } from "react";
-import { Link, useParams } from "react-router-dom";
+import { Link, useParams, useSearchParams } from "react-router-dom";
 import { PageHeader } from "@/components/layout/PageHeader";
 import {
@@ -55,5 +55,11 @@ export function CampaignDetailPage() {
   );
   const mailboxes = useApi<ConnectedMailbox[]>((s) => api.listConnectedMailboxes(false, s), []);
-  const [tab, setTab] = useState<TabKey | null>(null);
+  const [search] = useSearchParams();
+  // A link can ask for a tab ("Review the intro" from the reply desk); otherwise open where the work is.
+  const [tab, setTab] = useState<TabKey | null>(() => {
+    const asked = search.get("tab");
+    return asked === "people" || asked === "results" || asked === "review" || asked === "launch"
+      || asked === "steps" ? asked : null;
+  });
   const [acting, setActing] = useState<string | null>(null);
   const [lastAdd, setLastAdd] = useState<EnrollResult | null>(null);
```

- [ ] **Step 5: Styles, and the two layout fixes the browser found** — `frontend/src/pages/engagement/Engagement.module.css`:

```diff
diff --git a/frontend/src/pages/engagement/Engagement.module.css b/frontend/src/pages/engagement/Engagement.module.css
index 78ef13a..5426e6b 100644
--- a/frontend/src/pages/engagement/Engagement.module.css
+++ b/frontend/src/pages/engagement/Engagement.module.css
@@ -199,5 +199,7 @@
 .fields {
   display: grid;
-  grid-template-columns: repeat(auto-fit, minmax(16rem, 1fr));
+  /* min(): a column never asks for more than the space there is, so a narrow pane stacks the
+     fields instead of pushing them past its edge. */
+  grid-template-columns: repeat(auto-fit, minmax(min(16rem, 100%), 1fr));
   gap: var(--space-3) var(--space-4);
 }
@@ -589,5 +591,5 @@
 .correctRow {
   display: grid;
-  grid-template-columns: repeat(auto-fit, minmax(14rem, 18rem));
+  grid-template-columns: repeat(auto-fit, minmax(min(14rem, 100%), 18rem));
   gap: var(--space-3) var(--space-4);
 }
@@ -657,2 +659,94 @@
   }
 }
+
+/* ---- referral follow-through and writing again (phase 14) ---- */
+
+.candidates,
+.suggestions {
+  display: flex;
+  flex-direction: column;
+  gap: var(--space-2);
+  margin: 0;
+  padding: 0;
+  list-style: none;
+}
+
+.suggestions {
+  gap: var(--space-3);
+}
+
+.candidate {
+  display: flex;
+  flex-direction: column;
+  gap: 2px;
+  width: 100%;
+  min-height: 44px;
+  padding: var(--space-2) var(--space-3);
+  border: 1px solid var(--border);
+  border-radius: var(--radius);
+  background: var(--surface);
+  color: var(--text);
+  font: inherit;
+  text-align: left;
+  cursor: pointer;
+  overflow-wrap: anywhere;
+}
+
+.candidate:hover {
+  background: var(--surface-2);
+}
+
+.candidate[aria-pressed="true"] {
+  border-color: var(--accent);
+  background: var(--accent-quiet);
+}
+
+.candidate:focus-visible {
+  outline: 2px solid var(--ring);
+  outline-offset: 2px;
+}
+
+.candidate:disabled {
+  cursor: default;
+  opacity: 0.6;
+}
+
+.suggestionHead {
+  display: flex;
+  flex-wrap: wrap;
+  align-items: flex-start;
+  justify-content: space-between;
+  gap: var(--space-2);
+}
+
+.badgeRow {
+  display: inline-flex;
+  flex-wrap: wrap;
+  gap: var(--space-2);
+}
+
+/* The news, with its icon in a column of its own so a wrapped headline stays beside it. */
+.signalLine {
+  display: grid;
+  grid-template-columns: auto minmax(0, 1fr);
+  align-items: start;
+  gap: var(--space-2);
+  margin: 0;
+  font-size: var(--text-sm);
+  color: var(--text);
+  line-height: var(--leading);
+}
+
+.signalText {
+  display: flex;
+  flex-direction: column;
+  gap: 2px;
+  overflow-wrap: anywhere;
+}
+
+.inlineIcon {
+  width: 16px;
+  height: 16px;
+  margin-top: 2px;
+  color: var(--text-muted);
+}
```

`MailboxesPage.module.css` and `ReportsPanel.module.css` get the same `min()`:

```diff
diff --git a/frontend/src/pages/engagement/MailboxesPage.module.css b/frontend/src/pages/engagement/MailboxesPage.module.css
index 63c7127..1cd40eb 100644
--- a/frontend/src/pages/engagement/MailboxesPage.module.css
+++ b/frontend/src/pages/engagement/MailboxesPage.module.css
@@ -73,5 +73,5 @@
 .fields {
   display: grid;
-  grid-template-columns: repeat(auto-fit, minmax(16rem, 1fr));
+  grid-template-columns: repeat(auto-fit, minmax(min(16rem, 100%), 1fr));
   gap: var(--space-4);
 }
```

```diff
diff --git a/frontend/src/pages/engagement/ReportsPanel.module.css b/frontend/src/pages/engagement/ReportsPanel.module.css
index 072297e..b3b1465 100644
--- a/frontend/src/pages/engagement/ReportsPanel.module.css
+++ b/frontend/src/pages/engagement/ReportsPanel.module.css
@@ -1,5 +1,5 @@
 .panel {
   display: grid;
-  grid-template-columns: repeat(auto-fit, minmax(20rem, 1fr));
+  grid-template-columns: repeat(auto-fit, minmax(min(20rem, 100%), 1fr));
   gap: var(--space-5);
 }
```

and the `Tabs` primitive scrolls instead of overflowing — `frontend/src/components/ui/Tabs.module.css`:

```diff
diff --git a/frontend/src/components/ui/Tabs.module.css b/frontend/src/components/ui/Tabs.module.css
index 72eec93..b6ac923 100644
--- a/frontend/src/components/ui/Tabs.module.css
+++ b/frontend/src/components/ui/Tabs.module.css
@@ -1,6 +1,13 @@
+/* A strip wider than its pane scrolls sideways, rather than widening the page or being clipped by
+   it (four tabs on a phone-width reply desk did both). The divider is an inset shadow, not a border:
+   a scrolling box clips its children at the padding edge, which would cut the selected tab's
+   underline where it used to overlap the border, and a child always paints over its parent's shadow. */
 .tabs {
   display: flex;
   gap: var(--space-1);
-  border-bottom: 1px solid var(--border);
+  box-shadow: inset 0 -1px 0 var(--border);
+  overflow-x: auto;
+  overscroll-behavior-x: contain;
+  scrollbar-width: thin;
 }
 
@@ -8,4 +15,5 @@
   position: relative;
   display: inline-flex;
+  flex-shrink: 0;
   align-items: center;
   gap: var(--space-2);
@@ -14,6 +22,6 @@
   font-weight: var(--weight-medium);
   color: var(--text-muted);
+  white-space: nowrap;
   border-bottom: 2px solid transparent;
-  margin-bottom: -1px;
   transition: color var(--dur-fast) var(--ease-out);
 }
@@ -33,4 +41,5 @@
    `--surface-3` and 4.62:1 on `--bg`, so only the second keeps the unchosen label at AA. */
 .segmented {
+  box-shadow: none;
   gap: var(--space-1);
   padding: var(--space-1);
```

- [ ] **Step 6: Build** — from the repository root, with the whole tree available (the bundle is written to `nexus/web/dist`, outside `frontend/`): `cd frontend && npm run typecheck && npm run build` — expected: no errors.

---

### Task 7: The tests

- [ ] **Step 1:** `tests/test_engagement_enhancements.py`:

```python
"""The SDR enhancements (spec §19): referral follow-through, CRM activity logging, and signal
re-engagement.

Real rows through `tenant_session`, the real send path through the provider double the other
engagement suites use, and the CRM through `StubCRMConnector`, the connector's own offline double
installed through the documented `set_crm_connector` seam (D21).
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from nexus.core.config import get_settings
from tests.conftest import auth, signup, tenant_session
from tests.test_engagement_sending import SentFolder
from tests.test_engagement_sequences import NOW, _enrollment, _launched, _run, _world


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


# ---- referral: who a reply points to (pure) ------------------------------------------------------

def test_a_referral_is_read_from_the_cc_line_the_text_and_the_phrases_people_use():
    from nexus.engagement.enhancements.referral import extract

    found = extract(
        "Thanks, I'm not the right person. Please talk to Priya Shah (priya.shah@acme.io); "
        "Omar Farouk is the right person for billing. Also loop in Marketing.",
        cc=["dev.lee@acme.io", "sam@seller.io"], sender="jane@acme.io", ours="sam@seller.io",
        account_domain="acme.io", exclude_names=("Jane Buyer", "Sam Rep"))
    assert [(p.name, p.email, p.evidence) for p in found] == [
        ("Dev Lee", "dev.lee@acme.io", "copied on the reply"),
        ("Priya Shah", "priya.shah@acme.io", "named in the reply"),
        ("Omar Farouk", "", "named in the reply"),
    ]


def test_addresses_elsewhere_the_sender_and_departments_are_not_referrals():
    from nexus.engagement.enhancements.referral import extract

    found = extract("Reach out to Jane at jane@acme.io, or my friend at bob@gmail.com. "
                    "Contact Sales if not. Talk to me first.",
                    sender="jane@acme.io", account_domain="acme.io", exclude_names=("Jane Buyer",))
    assert found == [], "the sender, another domain, a department and 'me' name nobody new"


def test_a_name_is_read_from_an_address_only_when_the_address_spells_one():
    from nexus.engagement.enhancements.referral import name_from_email

    assert name_from_email("priya.shah@acme.io") == "Priya Shah"
    assert name_from_email("p_shah@acme.io") == "P Shah"
    assert name_from_email("pshah@acme.io") == ""


# ---- referral: follow-through (real rows, real enrollment, real draft) ---------------------------

async def _referral_reply(tid, *, body: str, cc: list[str]):
    from nexus.core.db import utcnow
    from nexus.models.engagement import (
        EngagementEnrollment,
        EngagementMessage,
        EngagementThread,
        ReplyClassification,
    )

    async with tenant_session(tid) as ts:
        enrollment = await ts.first(EngagementEnrollment)
        thread = await ts.first(EngagementThread)
        inbound = EngagementMessage(
            mailbox_connection_id=enrollment.mailbox_connection_id, thread_id=thread.id,
            contact_id=enrollment.contact_id, direction="in", kind="reply", status="received",
            from_addr="jane0@acme.io", cc_addrs=cc, subject="Re: Quick question",
            body_text=body, received_at=utcnow())
        ts.add(inbound)
        await ts.flush()
        reading = ReplyClassification(
            message_id=inbound.id, mailbox_connection_id=enrollment.mailbox_connection_id,
            enrollment_id=enrollment.id, contact_id=enrollment.contact_id,
            account_id=enrollment.account_id, category="referral", confidence=0.9)
        ts.add(reading)
        await ts.flush()
        return reading.id, enrollment.campaign_id


async def test_a_referral_becomes_an_intro_waiting_in_the_review_queue(folder, monkeypatch):
    from nexus.engagement.drafting.context import build_context
    from nexus.engagement.enhancements import referral
    from nexus.engagement.sequences.service import review_queue
    from nexus.models.account import Account, Contact
    from nexus.models.engagement import EngagementCampaign, MailboxConnection, ReplyClassification

    tid, _ = await _launched("referral", monkeypatch)
    enrollment = await _enrollment(tid)
    await _run(tid, enrollment.id, enrollment.next_action_at + timedelta(seconds=1))
    reading_id, campaign_id = await _referral_reply(
        tid, cc=["priya.shah@acme.io"],
        body="I'm not the right person, please talk to Priya Shah, she runs platform.\n\n"
             "On Tue, Sam Rep wrote:\n> Could we talk to Bob Smith about it?")

    async with tenant_session(tid) as ts:
        reading = await ts.get(ReplyClassification, reading_id)
        named = await referral.candidates(ts, reading)
        # The quoted history names Bob Smith; that was us, not them.
        assert [(c["name"], c["email"], c["contact_id"]) for c in named] == [
            ("Priya Shah", "priya.shah@acme.io", None)]
        result = await referral.follow_through(ts, reading, name="Priya Shah",
                                               email="priya.shah@acme.io", user_id="u1")
    assert result["campaign_id"] == campaign_id and result["drafted"], result

    async with tenant_session(tid) as ts:
        priya = await ts.get(Contact, result["contact_id"])
        assert priya.custom_fields["referred_by"]["name"] == "Jane0 Buyer"
        campaign = await ts.get(EngagementCampaign, campaign_id)
        waiting = {e.contact_id: row for e, row in await review_queue(ts, campaign)}
        assert waiting[priya.id] is not None and waiting[priya.id].status == "draft"
        pack = await build_context(
            ts, enrollment=next(e for e, _ in await review_queue(ts, campaign)
                                if e.contact_id == priya.id),
            contact=priya, account=await ts.get(Account, priya.account_id),
            mailbox=await ts.get(MailboxConnection, campaign.mailbox_connection_id), kind="first")
        assert "REFERRAL\n- Jane0 Buyer (VP Engineering) at Acme Robotics suggested" in pack.text
        assert "Jane0 suggested you get in touch" in pack.text
        # Their words never reach the prompt; only who made the introduction.
        assert "runs platform" not in pack.text

        reading = await ts.get(ReplyClassification, reading_id)
        with pytest.raises(referral.ReferralError, match="already in Q4"):
            await referral.follow_through(ts, reading, name="Priya", email="", user_id="u1")
        with pytest.raises(referral.ReferralError, match="Add Omar's surname"):
            await referral.follow_through(ts, reading, name="Omar", email="", user_id="u1")


async def test_a_campaign_with_no_steps_refuses_the_intro_before_creating_anyone(folder,
                                                                                monkeypatch):
    from nexus.engagement.enhancements import referral
    from nexus.models.account import Contact
    from nexus.models.engagement import EngagementStep, ReplyClassification

    tid, _ = await _launched("referralnosteps", monkeypatch)
    enrollment = await _enrollment(tid)
    await _run(tid, enrollment.id, enrollment.next_action_at + timedelta(seconds=1))
    reading_id, _campaign_id = await _referral_reply(tid, cc=["dev.lee@acme.io"],
                                                     body="Please talk to Dev Lee.")
    async with tenant_session(tid) as ts:
        # A hand-built or migrated campaign can reach here without a step.
        for step in await ts.list(EngagementStep):
            await ts.delete(step)
    async with tenant_session(tid) as ts:
        reading = await ts.get(ReplyClassification, reading_id)
        with pytest.raises(referral.ReferralError, match="has no steps"):
            await referral.follow_through(ts, reading, name="Dev Lee", email="dev.lee@acme.io",
                                          user_id="u1")
        assert await ts.first(Contact, Contact.email == "dev.lee@acme.io") is None


# ---- CRM activity logging ------------------------------------------------------------------------

async def test_sends_replies_and_meetings_reach_the_crm_once(monkeypatch):
    from nexus.core.db import utcnow
    from nexus.ingestion.crm import StubCRMConnector, set_crm_connector
    from nexus.models.account import Account, Contact
    from nexus.models.engagement import EngagementMessage, ReplyClassification
    from nexus.models.identity import Tenant
    from nexus.workers.tasks import handle_log_engagement_crm

    tid, _user_id, mailbox_id, (jane, ken) = await _world("crmlog", contacts=2)
    now = utcnow()
    async with tenant_session(tid) as ts:
        (await ts.session.get(Tenant, tid)).automation_enabled = True
        acme = await ts.first(Account)
        acme.crm_id, acme.crm_source = "9001", "stub"
        globex = Account(name="Globex", domain="globex.com")
        ts.add(globex)
        await ts.flush()
        stranger = Contact(account_id=globex.id, full_name="Gia Ray", email="gia@globex.com")
        ts.add(stranger)
        await ts.flush()

        def message(contact_id, direction, **fields):
            row = EngagementMessage(mailbox_connection_id=mailbox_id, contact_id=contact_id,
                                    direction=direction, subject="Quick question", **fields)
            ts.add(row)
            return row

        message(jane, "out", kind="step", status="sent", sent_at=now - timedelta(hours=2))
        reply = message(jane, "in", kind="reply", status="received",
                        received_at=now - timedelta(hours=1))
        away = message(ken, "in", kind="reply", status="received",
                       received_at=now - timedelta(minutes=30))
        message(jane, "out", kind="step", status="sent", sent_at=now - timedelta(days=9))
        message(stranger.id, "out", kind="step", status="sent", sent_at=now - timedelta(hours=3))
        await ts.flush()
        ts.add(ReplyClassification(message_id=reply.id, mailbox_connection_id=mailbox_id,
                                   contact_id=jane, account_id=acme.id, category="interested",
                                   decision="meeting", decided_at=now - timedelta(minutes=10),
                                   status="done"))
        ts.add(ReplyClassification(message_id=away.id, mailbox_connection_id=mailbox_id,
                                   contact_id=ken, account_id=acme.id,
                                   category="out_of_office"))

    monkeypatch.setattr(get_settings(), "crm_sync_enabled", True)
    monkeypatch.setattr(get_settings(), "engagement_campaigns_enabled", True)
    connector = StubCRMConnector()
    set_crm_connector(connector)
    try:
        first = await handle_log_engagement_crm({})
        # Globex is not in the CRM yet, so its email waits; the nine-day-old send is not backfilled;
        # the out-of-office is not a reply.
        assert first == {"tenants": 1, "logged": 3, "waiting": 1, "failed": 0}
        assert [a["kind"] for a in connector.pushed_activities] == \
            ["email_sent", "email_reply", "meeting_booked"]
        assert {a["account_id"] for a in connector.pushed_activities} == {"9001"}
        assert connector.pushed_activities[0]["detail"]["subject"] == \
            "Email to Jane0 Buyer: Quick question"
        assert connector.pushed_activities[2]["detail"]["subject"] == \
            "Meeting booked with Jane0 Buyer"

        again = await handle_log_engagement_crm({})
        assert again["logged"] == 0 and len(connector.pushed_activities) == 3
    finally:
        set_crm_connector(None)


async def test_the_crm_log_waits_for_its_switches(monkeypatch):
    from nexus.workers.tasks import handle_log_engagement_crm

    monkeypatch.setattr(get_settings(), "engagement_campaigns_enabled", True)
    monkeypatch.setattr(get_settings(), "crm_sync_enabled", False)
    assert await handle_log_engagement_crm({}) == {"skipped": "crm_sync_disabled"}
    monkeypatch.setattr(get_settings(), "crm_sync_enabled", True)
    monkeypatch.setattr(get_settings(), "engagement_campaigns_enabled", False)
    assert "skipped" in await handle_log_engagement_crm({})


# ---- signal re-engagement ------------------------------------------------------------------------

async def test_news_suggests_writing_again_only_to_people_it_is_fair_to_write_to():
    from nexus.engagement.enhancements.signal_reengage import suggestions
    from nexus.engagement.sequences.service import create_campaign, enroll
    from nexus.engagement.suppression.service import suppress
    from nexus.models.account import Contact
    from nexus.models.engagement import EngagementEnrollment
    from tests.test_engagement_sequences import STEPS

    tid, user_id, mailbox_id, ids = await _world("restartrules", contacts=7)
    # The world's signal: "Acme Robotics raises $40M Series B", three days before NOW.
    async with tenant_session(tid) as ts:
        campaign = await create_campaign(ts, name="Q4", owner_user_id=user_id,
                                         mailbox_id=mailbox_id, steps=STEPS)
        await enroll(ts, campaign, ids)
        rows = {e.contact_id: e for e in await ts.list(EngagementEnrollment)}
        quiet, declined, soon, far, blocked, recent, elsewhere = (rows[i] for i in ids)
        # Went quiet here, but another campaign is about to email them anyway.
        other = await create_campaign(ts, name="Q1", owner_user_id=user_id,
                                      mailbox_id=mailbox_id, steps=STEPS)
        await enroll(ts, other, [elsewhere.contact_id])
        for e in (quiet, blocked, elsewhere):
            e.status, e.finished_at = "completed", NOW - timedelta(days=10)
        declined.status, declined.status_reason = "stopped", "declined"
        soon.status, soon.status_reason = "snoozed", "later"
        soon.snoozed_until = NOW + timedelta(days=5)
        far.status, far.status_reason = "snoozed", "later"
        far.snoozed_until = NOW + timedelta(days=60)
        # Went quiet AFTER the news: the news is not a new reason to write.
        recent.status, recent.finished_at = "completed", NOW - timedelta(days=1)
        await suppress(ts, email=(await ts.get(Contact, blocked.contact_id)).email,
                       reason="manual")

    async with tenant_session(tid) as ts:
        found = await suggestions(ts, user_id=user_id, now=NOW)
    assert {(s.contact_id, s.reason) for s in found} == {(quiet.contact_id, "quiet"),
                                                        (far.contact_id, "later")}
    assert {s.signal_title for s in found} == {"Acme Robotics raises $40M Series B"}


async def test_writing_again_drafts_in_the_thread_and_sends_once(folder, monkeypatch):
    from email import message_from_bytes

    from nexus.core.db import utcnow
    from nexus.engagement.enhancements import signal_reengage
    from nexus.models.engagement import EngagementEnrollment, EngagementMessage, MailboxConnection
    from nexus.models.signal import SignalEvent

    tid, _ = await _launched("restartsend", monkeypatch)
    enrollment = await _enrollment(tid)
    await _run(tid, enrollment.id, enrollment.next_action_at + timedelta(seconds=1))
    now = utcnow()
    async with tenant_session(tid) as ts:
        row = await ts.get(EngagementEnrollment, enrollment.id)
        row.status, row.finished_at = "completed", now - timedelta(days=18)
        for sent in await ts.list(EngagementMessage):
            sent.sent_at = now - timedelta(days=20)
        news = SignalEvent(account_id=row.account_id, kind="hiring", source="ats",
                           title="Acme Robotics is hiring 12 platform engineers", strength=0.95,
                           occurred_at=now - timedelta(days=2), dedupe_key="restartsend-hiring")
        ts.add(news)
        await ts.flush()
        user_id = (await ts.get(MailboxConnection, row.mailbox_connection_id)).owner_user_id
        signal_id = news.id

    async with tenant_session(tid) as ts:
        found = await signal_reengage.suggestions(ts, user_id=user_id, now=now)
        assert [(s.enrollment_id, s.signal_id) for s in found] == [(enrollment.id, signal_id)]
        written = await signal_reengage.draft(ts, user_id=user_id, enrollment_id=enrollment.id,
                                              signal_id=signal_id, now=now)
    assert written["subject"].startswith("Re: ") and written["body"]

    async with tenant_session(tid) as ts:
        result = await signal_reengage.send(ts, user_id=user_id, enrollment_id=enrollment.id,
                                            signal_id=signal_id, subject=written["subject"],
                                            body=written["body"], now=now)
    assert result.sent, result
    first, again = (message_from_bytes(raw) for raw in folder.delivered)
    assert again["In-Reply-To"] == first["Message-ID"], "in the same thread"

    async with tenant_session(tid) as ts:
        assert await signal_reengage.suggestions(ts, user_id=user_id, now=now) == []
        with pytest.raises(signal_reengage.RestartError, match="no longer applies"):
            await signal_reengage.send(ts, user_id=user_id, enrollment_id=enrollment.id,
                                       signal_id=signal_id, subject="x", body="y", now=now)
    assert len(folder.delivered) == 2


# ---- the routes ----------------------------------------------------------------------------------

async def test_the_enhancements_are_dark_with_the_engine_and_answer_with_it(client, monkeypatch):
    token = await signup(client, slug="enhdark", email="sam@enhdark.com", company="E")
    monkeypatch.setattr(get_settings(), "engagement_campaigns_enabled", False)
    assert (await client.get("/api/engagement/restart", headers=auth(token))).status_code == 404
    assert (await client.get("/api/engagement/desk/nope/referral",
                             headers=auth(token))).status_code == 404
    monkeypatch.setattr(get_settings(), "engagement_campaigns_enabled", True)
    r = await client.get("/api/engagement/restart", headers=auth(token))
    assert r.status_code == 200 and r.json() == []
    assert (await client.get("/api/engagement/desk/nope/referral",
                             headers=auth(token))).status_code == 404
```

- [ ] **Step 2:** The structural checks appended to `tests/test_engagement_screens_ui.py`:

```diff
diff --git a/tests/test_engagement_screens_ui.py b/tests/test_engagement_screens_ui.py
index e94d1e4..6aa372d 100644
--- a/tests/test_engagement_screens_ui.py
+++ b/tests/test_engagement_screens_ui.py
@@ -154,2 +154,31 @@ def test_a_set_time_step_offers_the_best_time():
     # Moving one person converts THEIR clock into the viewer's local input.
     assert "zonedClockToLocalInput(" in detail and "moving.contact_timezone" in detail
+
+
+# ---- enhancements (phase 14) ---------------------------------------------------------------------
+
+def test_a_referral_reply_offers_the_intro_and_nothing_else_does():
+    desk = _read(PAGES / "ReplyDeskPage.tsx")
+    assert '{openItem && category === "referral" && <ReferralPanel id={id} />}' in desk
+    panel = _read(PAGES / "ReferralPanel.tsx")
+    # Spending is announced where it happens: a blank address is looked up, and that costs.
+    assert "uses an enrichment credit" in panel
+    assert "?tab=review" in panel, "the done state goes straight to where the intro is approved"
+
+
+def test_writing_again_is_its_own_tab_and_sends_only_on_send():
+    desk = _read(PAGES / "ReplyDeskPage.tsx")
+    assert '{ value: "restart", label: "Write again"' in desk
+    again = _read(PAGES / "WriteAgain.tsx")
+    assert again.count("api.restartSend(") == 1
+    assert "onClick={send}" in again
+    today = _read(COMPONENTS / "TodayPlan.tsx")
+    assert 'restart: { label: "Write again"' in today
+
+
+def test_a_tab_strip_scrolls_rather_than_widening_the_page():
+    css = _read(SRC / "components" / "ui" / "Tabs.module.css")
+    assert "overflow-x: auto;" in css and "white-space: nowrap;" in css
+    # The divider cannot be a border: a scrolling box would clip the selected tab's underline.
+    tabs_rule = css.split(".tabs {", 1)[1].split("}", 1)[0]
+    assert "border-bottom" not in tabs_rule and "inset 0 -1px 0 var(--border)" in tabs_rule
```

- [ ] **Step 3: Run** `pytest tests/test_migrations_replay.py tests/test_engagement_*.py tests/test_billing_metering_coverage.py tests/test_crm_auto_sync.py tests/test_crm_push.py tests/test_crm_two_way.py tests/test_job_durability.py tests/test_rls_binding_guard.py tests/test_worker_concurrency.py tests/test_credential_leaks.py tests/test_plan_gated_nav.py tests/test_login_copy.py -q -n 6` — expected PASS; `ruff check nexus tests migrations` — clean.

- [ ] **Step 4: See it.** In the isolated preview (phase 11, Task 7), with a referral reply that Cc's a colleague and a quiet enrollment at an account with fresh news: the referral panel picks the colleague, Draft intro shows the done state, and "Review the intro" opens the Review tab with the draft; Write again lists the quiet person with the headline and drafts a "Re:" email; Today shows "Write again to 1 person"; at phone width nothing is wider than the pane and every tab is reachable.

- [ ] **Step 5: Commit**

```bash
git add -A frontend/src nexus tests migrations docs/superpowers/plans/2026-09-17-sdr-engagement/14-enhancements.md CLAUDE.md
git commit -m "feat(engagement): phase 14 - referrals, CRM activity log, signal re-engagement"
```

---

## What phase 15 depends on

The CRM log reads engagement rows only, so the cutover's migrated messages are logged once they fall inside the look-back, and not before; `scripts/migrate_engagement.py` should leave `crm_logged_at` NULL for history it imports older than seven days, which the sweep then never reads. Signal re-engagement treats migrated `completed` enrollments like any other, so the first week after the cutover may suggest people the old engine finished with: that is intended, and bounded by the 14-day rule.
