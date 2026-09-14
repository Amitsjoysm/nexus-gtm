"""Re-verify the deliverability of contacts' *existing* emails.

The enrichment waterfall attaches a verdict at write time, but contacts enriched while the
verifier was unreachable (or before ``email_verify_provider`` was configured) keep a guessed
address with no status — which the Contacts UI shows as "unverified". This re-runs the verifier
against the address already on file and persists the verdict. It never re-guesses the address;
it only scores what's there.

Used by the ``POST /contacts/reverify`` endpoint (per tenant, on demand) and by
``scripts/reverify_contacts.py`` (cross-tenant backfill).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Awaitable, Callable

from sqlalchemy import or_, select

from nexus.core.db import utcnow
from nexus.core.tenancy import TenantSession
from nexus.models.account import Contact
from nexus.verification import STATUS_INVALID, STATUS_UNKNOWN, STATUS_VALID, EmailVerification

logger = logging.getLogger("nexus.enrichment.reverify")

Verify = Callable[[str], Awaitable[EmailVerification]]

# Bounded fan-out: a re-verify pass over a workspace runs the verifier concurrently so a few
# hundred contacts finish in seconds, without opening an unbounded number of SMTP probes.
_CONCURRENCY = 10


def fresh_verify() -> Verify:
    """A verifier that bypasses the registry's verdict cache, so a re-verify always hits the
    live service. The cache can hold stale ``unknown`` verdicts from when the verifier was down;
    a deliberate re-verify must ignore them."""
    from nexus.core.config import get_settings
    from nexus.verification.provider import build_email_verifier

    return build_email_verifier(get_settings().email_verify_provider).verify_one


async def reverify_contact(contact: Contact, verify: Verify) -> bool:
    """Re-score ``contact.email`` and persist the verdict. Returns True if anything changed.

    Sets ``email_status`` from the live verdict. Only *raises* the stored confidence (on a
    confirmed ``valid``) — a catch-all "risky" verdict must not clobber a high web-sourced
    confidence that the address is the right one. Never mutates the address itself.
    """
    email = (contact.email or "").strip()
    if not email:
        return False
    verdict = await verify(email)
    if not verdict or not verdict.status:
        return False
    return apply_verdict(contact, verdict)


def apply_verdict(contact: Contact, verdict: EmailVerification) -> bool:
    """Persist a verdict about ``contact.email``. Returns True if anything changed.

    Only *raises* the stored confidence, and only on a confirmed ``valid``: a catch-all verdict must
    not clobber a high web-sourced confidence that the address is the right one.
    """
    changed = verdict.status != contact.email_status
    contact.email_status = verdict.status
    # Stamp the check time even when the verdict didn't move (unknown→unknown): the SDR sees
    # "Checked <date>" instead of an apparent no-op, and the cool-down has an anchor.
    contact.email_checked_at = utcnow()
    # Persist the detected ESP (gsuite/office365/…) so the UI can show it. JSON column: reassign
    # (don't mutate in place) so SQLAlchemy sees the change.
    if verdict.provider_type:
        cf = dict(contact.custom_fields or {})
        if cf.get("email_provider") != verdict.provider_type:
            cf["email_provider"] = verdict.provider_type
            contact.custom_fields = cf
            changed = True
    if verdict.status == STATUS_VALID and verdict.confidence > (contact.email_confidence or 0.0):
        contact.email_confidence = verdict.confidence
        changed = True
    return changed


async def reverify_contacts(
    ts: TenantSession, *, verify: Verify | None = None, only_unverified: bool = True,
) -> dict:
    """Re-verify the tenant's contact emails. ``only_unverified`` (default) limits the pass to
    contacts whose status is null/blank/unknown — the ones that read as "unverified"."""
    verify = verify or fresh_verify()
    stmt = select(Contact).where(
        Contact.tenant_id == ts.tenant_id, Contact.email.isnot(None)
    )
    if only_unverified:
        stmt = stmt.where(
            or_(
                Contact.email_status.is_(None),
                Contact.email_status == "",
                Contact.email_status == STATUS_UNKNOWN,
            )
        )
    else:
        # Full pass still honours the cool-down: a confirmed-valid address checked within the
        # window is skipped (its verdict can't have decayed; re-probing just burns quota).
        from nexus.core.config import get_settings

        cutoff = utcnow() - timedelta(days=get_settings().email_reverify_cooldown_days)
        stmt = stmt.where(
            or_(
                Contact.email_status.is_(None),
                Contact.email_status != STATUS_VALID,
                Contact.email_checked_at.is_(None),
                Contact.email_checked_at < cutoff,
            )
        )
    contacts = (await ts.session.execute(stmt)).scalars().all()

    sem = asyncio.Semaphore(_CONCURRENCY)

    async def _one(contact: Contact) -> bool:
        async with sem:
            try:
                return await reverify_contact(contact, verify)
            except Exception as exc:  # provider isolation — one bad row can't fail the sweep
                logger.warning("reverify failed for contact %s: %r", contact.id, exc)
                return False

    results = await asyncio.gather(*(_one(c) for c in contacts))
    await ts.flush()

    tally: dict[str, int] = {}
    for c in contacts:
        key = c.email_status or "none"
        tally[key] = tally.get(key, 0) + 1
    return {"checked": len(contacts), "updated": sum(1 for r in results if r), "statuses": tally}


#: Re-check verdicts that send a single-contact re-verify on to the pattern search. `risky` and
#: `catch_all` are answers about the saved address and keep it. `invalid` proves it wrong, `unknown`
#: means the verifier could not say, and in both the search is the only route to a better address.
_SEARCH_AGAIN = frozenset({STATUS_INVALID, STATUS_UNKNOWN})


@dataclass(slots=True)
class ReverifyOutcome:
    #: "rechecked": the saved address held up and was kept. "searched": the pattern search ran.
    action: str
    previous_email: str | None
    previous_status: str | None


async def reverify_or_find(
    ts: TenantSession,
    contact: Contact,
    *,
    user_id: str | None = None,
    verify: Verify | None = None,
    enricher=None,
) -> ReverifyOutcome:
    """A person asked to re-verify one contact. Charged ONCE, for whichever it turned out to be.

    Decided with the product owner 2026-09-14. Re-check the saved address against the live verifier.
    If it holds up, keep it and charge one email check (`verify.email`). If it is invalid or unknown,
    or there is no address, run the pattern search (first.last, then first, ... stopping at the first
    valid) and charge one contact enrichment (`enrich.contact`) INSTEAD. The search is what the user
    received; charging the re-check as well would bill one click twice.

    The re-check verdict is written only inside the meter, so a refused charge (402) leaves the
    contact exactly as it was. Always a fresh verifier, never the registry cache: the user is asking
    for a new answer.
    """
    # Imported here, not at module level: `metered` is the seam tests record, and the waterfall
    # pulls in every enrichment provider.
    from nexus.billing.meter import metered
    from nexus.enrichment.waterfall import get_enricher

    previous_email = contact.email or None
    previous_status = contact.email_status
    email = (contact.email or "").strip()
    verdict = await (verify or fresh_verify())(email) if email else None

    if verdict is not None and verdict.status and verdict.status not in _SEARCH_AGAIN:
        async with metered(
            ts, "verify.email", quantity=1, user_id=user_id, attrs={"single": True},
        ):
            apply_verdict(contact, verdict)
        await ts.flush()
        return ReverifyOutcome("rechecked", previous_email, previous_status)

    if verdict is not None and verdict.status == STATUS_INVALID:
        # Proven wrong. Its old confidence must not outrank what the search finds, or the waterfall
        # (which only replaces an address with an equally or more confident one) keeps the dead one.
        contact.email_status = STATUS_INVALID
        contact.email_checked_at = utcnow()
        contact.email_confidence = 0.0
    await (enricher or get_enricher()).enrich_contact(
        ts, contact, user_id=user_id, raise_on_block=True
    )
    return ReverifyOutcome("searched", previous_email, previous_status)
