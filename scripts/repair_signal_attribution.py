#!/usr/bin/env python
"""Delete stored signals that are not about the account they are filed under.

`WebNewsSource` and `DorkedSearchSource` now refuse these at ingest. That does nothing for the rows
already in the table, and those are what a rep is looking at — which is what prompted the report
that signals "are not matching with the company at all". Measured on the live database:

    29  directory / data-vendor profile pages, 15 of them classified `funding`
    32  headlines naming a DIFFERENT company than the account they were filed under
     1  directory page from the dork source

    The D. E. Shaw Group  <- "Arcesium - Wikipedia"
    Allscripts            <- "Netsmart Technologies | Private Equity"          (funding)
    PCC Ltd               <- "PulseSync Pte Ltd - LinkedIn"
    LGI Healthcare        <- "LGI Healthcare 2026 Company Profile: ... Funding"

**The predicates are imported from the ingest path, never re-implemented.** A second copy of
"is this a directory page" would drift from the one that guards live traffic, and then this script
would be cleaning up against a different rule than the one preventing new rows — the worst possible
place for the two to disagree.

DELETES rather than flags. A PitchBook profile page and a story about a different company have no
value to any rep, so there is nothing to preserve; and every query that reads `signal_events` would
otherwise have to learn about a suppression flag or the rows leak straight back into the UI.

Scoped to the SEARCH-BACKED sources only. `rss`, `ats` and `public_api` fetch the account's own
feed, board or filings, so their attribution comes from the source itself rather than from matching
a name in a search result — a different question, and not this script's business.

Idempotent: a second run finds nothing, because the rows are gone.

    python scripts/repair_signal_attribution.py            # dry run, prints what it would delete
    python scripts/repair_signal_attribution.py --apply    # actually delete
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter

# The two predicates that guard live ingestion. Imported, not copied.
from nexus.ingestion.sources import is_profile_page, names_account

# Only the sources that attribute by matching a search result to an account.
_SEARCH_BACKED = ("web_news", "dork")


async def _scan(apply: bool) -> int:
    from sqlalchemy import select

    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.account import Account
    from nexus.models.signal import SignalEvent

    # Platform session: this walks every tenant's signals, and a cross-tenant read under the
    # RLS-bound app role returns ZERO ROWS rather than raising — the script would report a clean
    # database and delete nothing, which is indistinguishable from success.
    sessionmaker = get_platform_sessionmaker()

    doomed: list[tuple[SignalEvent, str, str]] = []
    async with sessionmaker() as session:
        rows = (
            await session.scalars(
                select(SignalEvent).where(SignalEvent.source.in_(_SEARCH_BACKED))
            )
        ).all()
        accounts = {
            a.id: a
            for a in (
                await session.scalars(
                    select(Account).where(Account.id.in_({r.account_id for r in rows}))
                )
            ).all()
        }

        for row in rows:
            account = accounts.get(row.account_id)
            if account is None:
                # An orphan cannot be checked against anything, and cannot be shown either.
                doomed.append((row, "orphaned", ""))
                continue
            if is_profile_page(row.url or ""):
                doomed.append((row, "directory page", account.name or ""))
            elif not names_account(row.title or "", account):
                doomed.append((row, "names another company", account.name or ""))

        print(f"scanned {len(rows)} signals from {', '.join(_SEARCH_BACKED)}")
        print(f"{len(doomed)} would be deleted\n")

        by_reason = Counter(reason for _, reason, _ in doomed)
        for reason, n in by_reason.most_common():
            print(f"  {n:>4}  {reason}")
        print()

        for row, reason, account_name in doomed[:40]:
            print(f"  [{reason:<22}] {account_name[:24]:<24} <- {(row.title or '')[:52]}")
        if len(doomed) > 40:
            print(f"  ... and {len(doomed) - 40} more")

        if not apply:
            print("\nDRY RUN — nothing was deleted. Re-run with --apply.")
            return len(doomed)

        for row, _, _ in doomed:
            await session.delete(row)
        await session.commit()
        print(f"\ndeleted {len(doomed)} signals.")
    return len(doomed)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true",
        help="actually delete. Without it the script only reports.",
    )
    args = parser.parse_args()
    return 0 if asyncio.run(_scan(args.apply)) >= 0 else 1


if __name__ == "__main__":
    sys.exit(main())
