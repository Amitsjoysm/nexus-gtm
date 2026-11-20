"""Seal every SMTP mailbox app password still stored as plain text (2026-11-20).

Until 2026-11-20 a workspace's app-password mailboxes kept their password in plain JSON in
``tenants.email_settings``. The app now writes only ``password_sealed`` and reads either, and seals
the rest in the background at boot; this does the same on demand and says how many it found.

Dry run by default: it prints the count and writes nothing.

    python scripts/seal_mailbox_passwords.py
    python scripts/seal_mailbox_passwords.py --apply
"""
from __future__ import annotations

import argparse
import asyncio


async def main(apply: bool) -> dict:
    from nexus.integrations.mailbox_secrets import seal_all_workspaces

    return await seal_all_workspaces(dry_run=not apply)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write the sealed passwords")
    args = parser.parse_args()
    report = asyncio.run(main(args.apply))
    verb = "sealed" if args.apply else "would seal"
    print(f"{verb} {report['passwords']} password(s) in {report['workspaces']} workspace(s)")
