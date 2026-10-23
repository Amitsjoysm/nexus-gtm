#!/usr/bin/env python
"""Take Stripe's word on local copies of Stripe invoices written before 2026-10-23.

A declined plan switch voided its invoice at Stripe, and the app, which did not handle
`invoice.voided` until then, kept listing it to the customer as unpaid. Older copies also lack the
issue date, service period and lines the invoice list now shows. See
`nexus/billing/invoice_repair.py`.

DRY RUN BY DEFAULT. Run it where the database and the Stripe key are, as the other repair scripts:

    docker cp scripts/repair_provider_invoices.py nexus-gtm-app-1:/tmp/repair_invoices.py
    docker exec nexus-gtm-app-1 python /tmp/repair_invoices.py            # report only
    docker exec nexus-gtm-app-1 python /tmp/repair_invoices.py --apply    # write

Uses the Stripe credential in force (the Payments tab's active one, else the environment's).
Prints counts only, never a key or a customer.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write the changes (default: report)")
    ap.add_argument("--limit", type=int, default=0, help="cap invoices examined (0 = all)")
    args = ap.parse_args()

    from nexus.billing.invoice_repair import repair_provider_invoices
    from nexus.billing.payments import resolve_payment_provider

    provider = await resolve_payment_provider()
    if provider.name != "stripe" or not getattr(provider, "configured", False):
        print("No Stripe credential is in force; nothing to ask.")
        return 1
    report = await repair_provider_invoices(provider, apply=args.apply, limit=args.limit)
    print(("APPLIED " if args.apply else "DRY RUN ") + str(report.as_dict()))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
