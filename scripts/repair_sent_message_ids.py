"""Correct the Message-IDs stored for campaign emails sent before 2026-11-05.

Gmail replaces the Message-ID of an email sent through its API, and the app kept the one it wrote,
so every follow-up named a message the recipient never had and opened a new thread in their inbox.
Sends from now on store the right id (`engagement/sending/service._mark_sent`); this corrects the
emails already sent, so sequences already running thread for their recipients from the next email.

Asks each connected mailbox's provider (read only) for every sent email's Message-ID.

    python scripts/repair_sent_message_ids.py            # dry run: report only
    python scripts/repair_sent_message_ids.py --apply    # write the corrections
"""
from __future__ import annotations

import argparse
import asyncio


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write the corrections (default: report)")
    ap.add_argument("--limit", type=int, default=2000, help="sent emails checked per mailbox")
    args = ap.parse_args()

    from sqlalchemy import select

    from nexus.core.db import get_platform_sessionmaker
    from nexus.engagement.mailboxes.registry import open_provider
    from nexus.engagement.sending.message_ids import repair_mailbox
    from nexus.models.engagement import MailboxConnection
    from nexus.workers.tasks import tenant_session

    print("mode:", "APPLY" if args.apply else "dry run (pass --apply to write)")
    async with get_platform_sessionmaker()() as session:
        boxes = (await session.execute(
            select(MailboxConnection.tenant_id, MailboxConnection.id, MailboxConnection.email)
            .where(MailboxConnection.status == "connected"))).all()
    totals = {"checked": 0, "changed": 0, "unreadable": 0, "headers": 0}
    for tenant_id, mailbox_id, email in boxes:
        try:
            async with tenant_session(tenant_id) as ts:
                box = await ts.get(MailboxConnection, mailbox_id)
                provider = await open_provider(ts, box)
                result = await repair_mailbox(ts, box, provider, apply=args.apply,
                                              limit=args.limit)
        except Exception as exc:  # noqa: BLE001 - one mailbox failing must not stop the rest
            print(f"  {email}: could not be checked ({type(exc).__name__}: {exc})")
            continue
        totals["checked"] += result.checked
        totals["changed"] += len(result.changed)
        totals["unreadable"] += result.unreadable
        totals["headers"] += result.headers_rewritten
        print(f"  {email}: {result.checked} sent, {len(result.changed)} with another id, "
              f"{result.headers_rewritten} reply headers to correct, {result.unreadable} unreadable")
    verb = "corrected" if args.apply else "would correct"
    print(f"{verb} {totals['changed']} of {totals['checked']} sent emails' ids and "
          f"{totals['headers']} reply headers; {totals['unreadable']} could not be read"
          + ("" if args.apply else " (nothing written)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
