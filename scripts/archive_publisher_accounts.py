"""Archive accounts that discovery added and that are news publishers, not ICP accounts.

Until 2026-10-15 an ICP saying "Technology" mapped to LinkedIn's parent "Technology, Information and
Media", whose children include newspapers and broadcasters, and LinkedIn candidates skip the fit
threshold. Measured on the local deploy: daily discovery added CNN, The Washington Post, The
Economist and TIME as ICP accounts. The mapping is fixed for new discovery; this clears what it
already added.

Only accounts discovery added (`auto_discovery`, `discovery`) are considered, never one a person
added, imported or pulled from a CRM, and only by the LinkedIn industry stored on the account.
Archived, never deleted, through `Account.set_archived`, so Unarchive on the account page undoes
it. Accounts filed under "Online Audio and Video Media" are listed for review and left alone: that
industry also holds streaming and podcast software.

Dry run by default: it prints what it would archive and writes nothing.

    python scripts/archive_publisher_accounts.py
    python scripts/archive_publisher_accounts.py --apply
"""
from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import select

PUBLISHER_INDUSTRIES = frozenset({
    "Newspaper Publishing", "Book and Periodical Publishing",
    "Broadcast Media Production and Distribution", "Internet News", "Blogs",
})
REVIEW_INDUSTRIES = frozenset({"Online Audio and Video Media"})
DISCOVERY_SOURCES = ("auto_discovery", "discovery")
REASON = "news publisher, not an ICP account (2026-10-15 clean-up)"


def _already_archived(account) -> bool:
    return account.is_archived or bool((account.custom_fields or {}).get("archived"))


async def sweep(*, apply: bool = False, session=None) -> dict:
    """Archive discovered publisher accounts. Returns what it found; writes only when ``apply``."""
    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.account import Account

    async def run(s) -> dict:
        rows = (await s.execute(
            select(Account).where(
                Account.source.in_(DISCOVERY_SOURCES),
                Account.industry.in_(PUBLISHER_INDUSTRIES | REVIEW_INDUSTRIES),
            )
        )).scalars().all()
        archive, review = [], []
        for account in rows:
            if _already_archived(account):
                continue
            line = f"{account.domain or account.name} ({account.industry})"
            if account.industry in REVIEW_INDUSTRIES:
                review.append(line)
                continue
            archive.append(line)
            if apply:
                account.set_archived(True, reason=REASON)
        if apply:
            await s.commit()
        return {"archived": sorted(archive), "review": sorted(review)}

    if session is not None:
        return await run(session)
    async with get_platform_sessionmaker()() as own:
        return await run(own)


async def main(apply: bool) -> None:
    result = await sweep(apply=apply)
    verb = "Archived" if apply else "Would archive"
    print(f"{verb} {len(result['archived'])} discovered publisher account(s):")
    for line in result["archived"]:
        print(f"    {line}")
    if result["review"]:
        print(f"Left for review ({len(result['review'])}, not changed):")
        for line in result["review"]:
            print(f"    {line}")
    if not apply:
        print("\nDry run: nothing was written. Re-run with --apply to make these changes.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write the changes")
    asyncio.run(main(parser.parse_args().apply))
