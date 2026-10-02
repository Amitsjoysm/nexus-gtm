#!/usr/bin/env python
"""Clear LinkedIn URLs that are not what the field claims, and set account pages LinkedIn proved.

Since 2026-10-02 a contact can only be given a person's own profile (``linkedin.com/in/...``) and an
account only a company page, by every path that writes one. Values stored before that were taken
as typed, uploaded or read off a search snippet, so a contact may hold a company page or a post,
and an account a person's profile.

Dry run by default: it reports what it would change. ``--apply`` writes. Nothing is lost: a cleared
value moves to ``custom_fields["rejected_linkedin_url"]`` on the same row. Where the account's
shared company carries a page a LinkedIn read proved (its website is the account's domain), that
page fills a blank and replaces a different one. Safe to run repeatedly.

**Run it where the database is**, as `repair_feed_text.py` explains; the target is printed first.

    docker cp scripts/clean_linkedin_urls.py nexus-gtm-app-1:/tmp/clean_li.py
    docker exec nexus-gtm-app-1 python /tmp/clean_li.py            # report only
    docker exec nexus-gtm-app-1 python /tmp/clean_li.py --apply    # write
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

REJECTED = "rejected_linkedin_url"


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write the changes (default: report only)")
    args = ap.parse_args()

    from sqlalchemy import select

    from nexus.core.config import get_settings
    from nexus.core.db import get_platform_sessionmaker
    from nexus.enrichment.linkedin import company_page_url, person_profile_url
    from nexus.models.account import Account, Contact
    from nexus.models.company import Company

    settings = get_settings()
    dsn = getattr(settings, "db_owner_url", "") or settings.database_url
    print("database:", re.sub(r"://([^:/@]+):[^@]*@", r"://\1:***@", dsn))
    print("mode:", "APPLY" if args.apply else "dry run (pass --apply to write)")

    bad_contacts = bad_accounts = replaced = filled = 0
    async with get_platform_sessionmaker()() as session:
        contacts = (await session.scalars(
            select(Contact).where(Contact.linkedin_url.isnot(None)))).all()
        for contact in contacts:
            value = (contact.linkedin_url or "").strip()
            if not value or person_profile_url(value):
                continue
            bad_contacts += 1
            if args.apply:
                contact.custom_fields = {**(contact.custom_fields or {}), REJECTED: value}
                contact.linkedin_url = None

        proven = {c.id: c.linkedin_url for c in (await session.scalars(
            select(Company).where(Company.linkedin_fetched_at.isnot(None))
            .where(Company.linkedin_url.isnot(None)))).all()}
        for account in (await session.scalars(select(Account))).all():
            cf = dict(account.custom_fields or {})
            value = str(cf.get("linkedin_url") or "").strip()
            page = company_page_url(value) if value else None
            if value and page is None:
                bad_accounts += 1
                cf[REJECTED] = value
                cf.pop("linkedin_url", None)
            truth = company_page_url(proven.get(account.company_id or ""))
            if truth and (page or "").lower() != truth.lower():
                if page:
                    replaced += 1
                    cf[REJECTED] = page
                else:
                    filled += 1
                cf["linkedin_url"] = truth
            if args.apply and cf != (account.custom_fields or {}):
                account.custom_fields = cf
        if args.apply:
            await session.commit()

    print(f"{bad_contacts} contact(s) whose LinkedIn URL is not a person's profile; "
          f"{bad_accounts} account(s) whose page is not a company page; "
          f"{replaced} account page(s) replaced and {filled} filled by the page LinkedIn proved"
          + ("" if args.apply else " (nothing written)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
