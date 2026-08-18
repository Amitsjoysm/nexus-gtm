#!/usr/bin/env python
"""Prove the Stripe webhook path works, without waiting for Stripe to call us.

Stripe DRIVES subscription state here: checkout completes at the provider and the subscription
only reaches this database when the webhook arrives. So "is billing working?" has two independent
answers — *can we verify and apply an event*, and *is Stripe actually delivering one* — and they
fail for completely different reasons. Measured on the dev stack: the integration had created 2
products, 2 prices and 4 customers, and had **0 subscriptions**, because zero webhook endpoints
were registered. Nothing in the app could tell you that.

This script answers the first question. It signs a realistic event with the configured
``NEXUS_STRIPE_WEBHOOK_SECRET`` and POSTs it exactly as Stripe would, so a pass means the whole
chain — signature, freshness, dedupe, tenant resolution, status mapping — is sound and only
*delivery* is missing.

    python scripts/verify_stripe_webhook.py --url http://localhost:8080 --tenant <tenant_id>

It also checks the negative cases, because a webhook endpoint that accepts anything is worse than
one that is never called: a bad signature, a stale timestamp and a replay must all be refused.

**This writes to the tenant you name.** It goes through the real handler, so a passing run stamps
``psp_subscription_id`` / ``psp_customer_id`` on that tenant's subscription — which is the whole
point (an event that verified but applied nothing would prove far less). The ids are prefixed
``sub_verify_`` / ``cus_verify_`` so they are unmistakable, and the script prints the exact SQL to
undo it. Point it at a throwaway tenant if you have one.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SUB_ID = "sub_verify_webhook_probe"


def read_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"^([A-Z0-9_]+)=(.*)$", line.strip())
        if m:
            out[m.group(1)] = m.group(2)
    return out


def sign(payload: bytes, secret: str, timestamp: int) -> str:
    """Stripe's scheme: HMAC-SHA256 over `<timestamp>.<raw body>`, hex, as `t=...,v1=...`."""
    signed = f"{timestamp}.".encode() + payload
    digest = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def event_body(event_id: str, tenant_id: str, status: str = "active") -> bytes:
    """A `customer.subscription.updated` shaped like the real thing.

    `metadata.tenant_id` is what lets the handler find the row: the event arrives with no tenant
    context, and on a fresh subscription there is no `psp_subscription_id` to match on yet.
    """
    now = int(time.time())
    return json.dumps({
        "id": event_id,
        "object": "event",
        "type": "customer.subscription.updated",
        "created": now,
        "data": {"object": {
            "id": SUB_ID,
            "object": "subscription",
            "customer": "cus_verify_probe",
            "status": status,
            "current_period_start": now,
            "current_period_end": now + 30 * 86400,
            "cancel_at_period_end": False,
            "metadata": {"tenant_id": tenant_id},
        }},
    }, separators=(",", ":")).encode()


def post(url: str, body: bytes, signature: str) -> tuple[int, str]:
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json", "Stripe-Signature": signature},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode()[:200]
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()[:200]
    except Exception as exc:  # connection refused, DNS, ...
        return 0, f"{type(exc).__name__}: {exc}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8080", help="app base URL")
    ap.add_argument("--tenant", required=True, help="tenant id the event should resolve to")
    ap.add_argument("--env", default="deploy/.env", help="file holding the webhook secret")
    args = ap.parse_args()

    secret = read_env(Path(args.env)).get("NEXUS_STRIPE_WEBHOOK_SECRET", "")
    if not secret:
        print(f"FAIL: no NEXUS_STRIPE_WEBHOOK_SECRET in {args.env}")
        return 2
    endpoint = args.url.rstrip("/") + "/api/billing/webhooks/stripe"
    print(f"endpoint : {endpoint}")
    print(f"secret   : {secret[:8]}…{secret[-4:]}")
    print(f"tenant   : {args.tenant}\n")

    now = int(time.time())
    checks: list[tuple[str, bool, str]] = []

    # 1. The real thing. 200 means signature, freshness, dedupe and tenant resolution all worked.
    eid = f"evt_verify_{now}"
    body = event_body(eid, args.tenant)
    code, text = post(endpoint, body, sign(body, secret, now))
    checks.append(("a correctly signed event is accepted", code == 200, f"HTTP {code} {text}"))

    # 2. Replay. The dedupe table is keyed on the provider event id, so the SAME id must be
    #    accepted (200, stop retrying) without applying twice.
    code2, text2 = post(endpoint, body, sign(body, secret, now))
    checks.append(("a replay of the same event id is absorbed", code2 == 200, f"HTTP {code2} {text2}"))

    # 3. Wrong secret. Must be 400 and must NOT be retried by Stripe.
    bad = event_body(f"evt_verify_bad_{now}", args.tenant)
    code3, _ = post(endpoint, bad, sign(bad, "whsec_wrong_secret", now))
    checks.append(("a bad signature is refused", code3 == 400, f"HTTP {code3}"))

    # 4. Stale timestamp — the replay window. A captured request must not work forever.
    old = event_body(f"evt_verify_old_{now}", args.tenant)
    code4, _ = post(endpoint, old, sign(old, secret, now - 86_400))
    checks.append(("a stale timestamp is refused", code4 == 400, f"HTTP {code4}"))

    # 5. Tampered body against a valid signature — the HMAC covers the raw bytes.
    tampered = body.replace(b'"status":"active"', b'"status":"canceled"')
    code5, _ = post(endpoint, tampered, sign(body, secret, now))
    checks.append(("a tampered body is refused", code5 == 400, f"HTTP {code5}"))

    print(f"{'check':<46}{'result':<8}detail")
    print("-" * 92)
    ok = True
    for name, passed, detail in checks:
        ok &= passed
        print(f"{name:<46}{'PASS' if passed else 'FAIL':<8}{detail}")

    print()
    print("This run WROTE to the tenant above. Undo with:")
    print("  UPDATE billing_subscriptions SET psp_subscription_id=NULL, psp_customer_id=NULL")
    print(f"   WHERE psp_subscription_id='{SUB_ID}';")
    print(f"  DELETE FROM billing_webhook_events WHERE id LIKE 'evt_verify_%';")
    print("  -- run each statement separately: psql -c runs a multi-statement block as ONE")
    print("  -- transaction, so a failure in the second silently rolls back the first.")
    print()
    if ok:
        print("RESULT: the webhook path is sound. If subscriptions are still not appearing, the")
        print("        gap is DELIVERY — check that a webhook endpoint is registered in Stripe")
        print("        (Developers -> Webhooks) and reachable from the internet. For local work:")
        print("        stripe listen --forward-to localhost:8080/api/billing/webhooks/stripe")
    else:
        print("RESULT: the webhook path itself is broken — fix this before looking at delivery.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
