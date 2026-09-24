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
