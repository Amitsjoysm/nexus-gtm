"""Remove addresses the product should never have kept.

Two kinds, both written before the save policy existed (2026-09-22):

* **invalid** — the verifier proved the mailbox does not exist and the address was kept anyway,
  merely labelled. Removed and remembered on the contact, so the finder never guesses it again.
* **unverified guesses** — `first.last@domain` produced by the finder or scraped from search and
  saved with no verdict or `unknown`. Removed but NOT remembered: nothing proved them wrong, so a
  later search may legitimately find one of them again and prove it.

Customer-supplied addresses (imported, CRM, typed) are left alone unless they verified invalid: they
are the customer's data, not our guess.

Dry run by default — it prints what it would do and writes nothing:

    python scripts/clean_invalid_emails.py
    python scripts/clean_invalid_emails.py --apply
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter

from sqlalchemy import or_, select

#: Sources that mean "we generated this address", as opposed to the customer supplying it.
GUESS_SOURCES = ("pattern", "pattern_verified", "search")


async def sweep(*, apply: bool = False, session=None) -> dict:
    """Clear what should not have been saved. Returns counts; writes only when ``apply``."""
    from nexus.core.db import get_platform_sessionmaker
    from nexus.enrichment.policy import forget_email, remember_rejected
    from nexus.models.account import Contact
    from nexus.verification import STATUS_INVALID, STATUS_UNKNOWN

    async def run(s) -> dict:
        # Cross-tenant maintenance: the platform sessionmaker (owner role) is required, or RLS
        # returns zero rows and this reports a clean estate it never actually looked at.
        rows = (await s.execute(
            select(Contact).where(
                Contact.email.isnot(None),
                Contact.deleted_at.is_(None),
                or_(
                    Contact.email_status == STATUS_INVALID,
                    Contact.enrichment_source.in_(GUESS_SOURCES),
                ),
            )
        )).scalars().all()

        counts: Counter[str] = Counter()
        for contact in rows:
            proven_dead = contact.email_status == STATUS_INVALID
            unverified_guess = (
                contact.enrichment_source in GUESS_SOURCES
                and (contact.email_status or STATUS_UNKNOWN) in ("", STATUS_UNKNOWN)
            )
            if not proven_dead and not unverified_guess:
                continue
            counts["invalid" if proven_dead else "unverified_guess"] += 1
            if not apply:
                continue
            if proven_dead:
                remember_rejected(contact, [contact.email])
            forget_email(contact)
        if apply:
            await s.commit()
        return {"scanned": len(rows), "removed": sum(counts.values()), **counts}

    if session is not None:
        return await run(session)
    async with get_platform_sessionmaker()() as own:
        return await run(own)


async def main(apply: bool) -> None:
    result = await sweep(apply=apply)
    verb = "Removed" if apply else "Would remove"
    print(f"Scanned {result['scanned']} addresses this tool could act on.")
    print(f"  {verb} {result.get('invalid', 0)} proven invalid (remembered, never guessed again)")
    print(f"  {verb} {result.get('unverified_guess', 0)} unverified guesses")
    if not apply:
        print("\nDry run — nothing was written. Re-run with --apply to make these changes.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write the changes")
    asyncio.run(main(parser.parse_args().apply))
