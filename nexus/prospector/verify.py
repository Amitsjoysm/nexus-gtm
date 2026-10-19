"""Verify selected contacts' email addresses (decisions D19, D20).

The release for the campaign hold: a Prospector contact's provider email is kept unverified and held
out of campaigns until a verifier calls it deliverable.

* **Free for addresses Prospector delivered** (``enrichment_source == "sourcing:prospector"``), the
  owner's correction: the email credit already paid for it. Any other contact charges
  ``verify.email`` at its rate card, preflighted for that subset before anything is checked.
* **Up to 25 inline**, more as a durable background job (``prospector_verify_contacts``).
* **A fresh verifier** (``reverify.fresh_verify``), never the registry cache: the person asked for a
  new answer. Verifier calls are network-only and gathered with bounded concurrency; the verdicts
  are applied after the gather, because an AsyncSession is not safe for concurrent use.
* **The save policy decides what is kept** (``reverify.apply_verdict``): valid, catch-all and risky
  keep the address and release the hold; invalid removes it and remembers it; unknown keeps it,
  still held (D19 keeps vendor emails).
"""
from __future__ import annotations

import asyncio
import logging

from nexus.models.account import Contact
from nexus.prospector.hold import SOURCE

logger = logging.getLogger("nexus.prospector.verify")

INLINE_MAX = 25
CONCURRENCY = 5
CAPABILITY = "verify.email"


async def load_contacts(ts, contact_ids: list[str]) -> list[Contact]:
    unique = list(dict.fromkeys(i for i in contact_ids if i))
    if not unique:
        return []
    rows = await ts.list(Contact, Contact.id.in_(unique), Contact.deleted_at.is_(None))
    return [c for c in rows if (c.email or "").strip()]


def _is_free(contact: Contact) -> bool:
    return (contact.enrichment_source or "") == SOURCE


async def check(ts, contacts: list[Contact], *, user_id: str | None) -> dict:
    """Verify ``contacts`` now and charge the non-Prospector ones. Shared by the request and job."""
    from nexus.billing.entitlements import preflight
    from nexus.billing.meter import metered
    from nexus.enrichment import reverify

    paid = [c for c in contacts if not _is_free(c)]
    if paid:
        (await preflight(ts, CAPABILITY, quantity=len(paid))).raise_if_blocked()
    verify = reverify.fresh_verify()
    gate = asyncio.Semaphore(CONCURRENCY)

    async def _one(email: str):
        async with gate:
            try:
                return await verify(email)
            except Exception:
                logger.warning("verifier failed for one address", exc_info=True)
                return None

    verdicts = await asyncio.gather(*(_one(c.email.strip()) for c in contacts))
    results = []
    for contact, verdict in zip(contacts, verdicts):
        if verdict is not None and verdict.status:
            reverify.apply_verdict(contact, verdict)
        status = "invalid" if contact.email is None else (contact.email_status or "unknown")
        results.append({"contact_id": contact.id, "status": status,
                        "email_kept": contact.email is not None})
    await ts.flush()
    if paid:
        async with metered(ts, CAPABILITY, quantity=len(paid), user_id=user_id,
                           attrs={"prospector_verify": True}):
            pass
    return {"results": results, "charged": len(paid), "checked": len(contacts)}


async def verify_contacts(ts, *, user_id: str, contact_ids: list[str]) -> dict:
    contacts = await load_contacts(ts, contact_ids)
    if len(contacts) > INLINE_MAX:
        from nexus.billing.entitlements import preflight
        from nexus.workers import tasks

        paid = sum(1 for c in contacts if not _is_free(c))
        if paid:
            (await preflight(ts, CAPABILITY, quantity=paid)).raise_if_blocked()
        await tasks.enqueue_prospector_verify(ts.tenant_id, user_id, [c.id for c in contacts])
        return {"mode": "queued", "count": len(contacts), "charged": 0, "results": []}
    out = await check(ts, contacts, user_id=user_id)
    return {"mode": "inline", "count": len(contacts), "charged": out["charged"],
            "results": out["results"]}
