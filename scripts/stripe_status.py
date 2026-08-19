#!/usr/bin/env python
"""Is Stripe actually able to take a payment, and will it tell us when it does?

Read-only. Every check here answers a question that "the key is valid" does not:

* A valid key on an account with ``charges_enabled=False`` creates products and prices happily and
  fails on the first charge. Measured on this deployment: 2 prices published, 0 subscriptions ever.
* **Zero registered webhook endpoints is silent.** Checkout completes at Stripe, the customer is
  charged, and our subscription row never changes — there is no error anywhere, because nothing
  was ever sent.
* A missing **billing-portal configuration** fails `POST /billing/portal` even when charges work,
  and the error comes back from Stripe rather than from us, so it reads as a bug in our code.

    python scripts/stripe_status.py

Run inside the app container if the key is only in the container's environment:

    docker exec nexus-gtm-app-1 python /app/scripts/stripe_status.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The events `nexus/billing/webhooks.py` actually handles. Subscribing to more only adds noise to
# the dedupe table; subscribing to fewer loses subscription state silently.
REQUIRED_EVENTS = {
    "checkout.session.completed",
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
    "invoice.paid",
    "invoice.payment_failed",
    "invoice.finalized",
}

WEBHOOK_PATH = "/api/billing/webhooks/stripe"


async def main() -> int:
    import httpx

    from nexus.core.config import get_settings

    settings = get_settings()
    if settings.payment_provider != "stripe":
        print(f"payment_provider is {settings.payment_provider!r} — Stripe is not in use.")
        return 0
    key = settings.stripe_secret_key
    if not key:
        print("NEXUS_STRIPE_SECRET_KEY is not set — the provider is inert.")
        return 2

    problems: list[str] = []
    async with httpx.AsyncClient(timeout=30) as client:
        auth = (key, "")

        r = await client.get("https://api.stripe.com/v1/account", auth=auth)
        if r.status_code != 200:
            print(f"key rejected ({r.status_code}): {r.text[:160]}")
            return 2
        acct = r.json()
        mode = "LIVE" if acct.get("livemode") else "test"
        print(f"account {acct.get('id')}  ({mode} mode)")
        for flag in ("charges_enabled", "payouts_enabled", "details_submitted"):
            ok = bool(acct.get(flag))
            print(f"  {flag:<20}{'yes' if ok else 'NO'}")
            if not ok and flag != "payouts_enabled":
                problems.append(f"{flag} is false — finish onboarding in the Stripe dashboard")

        r = await client.get(
            "https://api.stripe.com/v1/webhook_endpoints", auth=auth, params={"limit": 50}
        )
        endpoints = r.json().get("data", []) if r.status_code == 200 else []
        print(f"\nwebhook endpoints: {len(endpoints)}")
        if not endpoints:
            problems.append(
                f"no webhook endpoint — register one at <public-host>{WEBHOOK_PATH}. "
                "Without it a completed Checkout never reaches us, silently."
            )
        for ep in endpoints:
            enabled = set(ep.get("enabled_events") or [])
            missing = REQUIRED_EVENTS - enabled
            # "*" means every event, which covers the required set.
            if "*" in enabled:
                missing = set()
            print(f"  {ep.get('url')}  [{ep.get('status')}]  {len(enabled)} events")
            if not (ep.get("url") or "").endswith(WEBHOOK_PATH):
                print(f"     ! does not end in {WEBHOOK_PATH}")
            if missing:
                print(f"     ! not subscribed to: {', '.join(sorted(missing))}")
                problems.append(f"{ep.get('url')} is missing {len(missing)} required event(s)")

        r = await client.get(
            "https://api.stripe.com/v1/billing_portal/configurations",
            auth=auth, params={"limit": 5},
        )
        configs = r.json().get("data", []) if r.status_code == 200 else []
        print(f"\nbilling-portal configurations: {len(configs)}")
        if not configs:
            problems.append(
                "no customer-portal configuration — POST /billing/portal will fail even once "
                "charges are enabled"
            )

        for label, path in (("products", "products"), ("prices", "prices"),
                            ("customers", "customers"), ("subscriptions", "subscriptions")):
            r = await client.get(
                f"https://api.stripe.com/v1/{path}", auth=auth, params={"limit": 100}
            )
            count = len(r.json().get("data", [])) if r.status_code == 200 else "?"
            print(f"{label:<16}{count}")

    print()
    if problems:
        print(f"{len(problems)} thing(s) stop this account taking a payment end to end:")
        for p in problems:
            print(f"  - {p}")
        print("\nSee docs/billing/stripe-go-live.md for the fix for each.")
        return 1
    print("Stripe is configured end to end.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
