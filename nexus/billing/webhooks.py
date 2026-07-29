# nexus/billing/webhooks.py
"""Payment-provider webhooks: verify, deduplicate, then act.

A webhook endpoint is a public, unauthenticated URL. Anyone who learns it can POST to it, so
every guarantee has to come from the request itself:

1. **Signature.** HMAC-SHA256 over ``{timestamp}.{raw_body}`` with the endpoint secret, compared
   in constant time. An unsigned or wrongly-signed request is refused before the body is parsed
   as anything meaningful.
2. **Freshness.** A signature is valid forever unless the timestamp is checked, so a captured
   request could be replayed indefinitely. Events older than the tolerance are refused.
3. **Exactly-once.** Providers retry deliberately, and a retry of
   ``payment_intent.succeeded`` must not mark a second invoice paid. The provider's event id is
   the primary key of ``billing_webhook_events``, so replay protection is a database constraint,
   not an application check that two concurrent deliveries could race.

Verification is done on the RAW body. Re-serializing JSON changes bytes and breaks the HMAC,
which is the single most common way webhook verification is silently defeated.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass

logger = logging.getLogger("nexus.billing.webhooks")

# Stripe's own recommendation. Long enough to survive clock skew and slow networks, short enough
# that a captured request stops being useful quickly.
DEFAULT_TOLERANCE_S = 300


class WebhookError(Exception):
    """Base class for a rejected webhook."""


class SignatureError(WebhookError):
    """Missing, malformed, or invalid signature."""


class StaleWebhookError(WebhookError):
    """Signature is valid but the event is outside the replay window."""


@dataclass
class VerifiedEvent:
    event_id: str
    event_type: str
    payload: dict
    digest: str


def _parse_signature_header(header: str) -> tuple[int, list[str]]:
    """Pull the timestamp and v1 signatures out of ``t=...,v1=...,v1=...``.

    Multiple v1 entries are legitimate during a secret rotation; any one matching is enough.
    """
    timestamp = 0
    signatures: list[str] = []
    for part in (header or "").split(","):
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        key = key.strip()
        if key == "t":
            try:
                timestamp = int(value.strip())
            except ValueError:
                raise SignatureError("malformed timestamp in signature header") from None
        elif key == "v1":
            signatures.append(value.strip())
    if not timestamp or not signatures:
        raise SignatureError("signature header missing timestamp or v1 signature")
    return timestamp, signatures


def verify_stripe_signature(
    raw_body: bytes,
    signature_header: str,
    secret: str,
    *,
    tolerance_s: int = DEFAULT_TOLERANCE_S,
    now: float | None = None,
) -> VerifiedEvent:
    """Verify and parse a Stripe webhook. Raises on anything suspicious."""
    if not secret:
        # Refusing beats accepting: an endpoint with no configured secret can verify nothing,
        # and treating that as "allow" would make the whole check decorative.
        raise SignatureError("no webhook secret configured; refusing to trust this request")
    if not raw_body:
        raise SignatureError("empty request body")

    timestamp, signatures = _parse_signature_header(signature_header)

    current = time.time() if now is None else now
    if abs(current - timestamp) > tolerance_s:
        raise StaleWebhookError(
            f"event timestamp is outside the {tolerance_s}s tolerance; treating as a replay"
        )

    signed_payload = f"{timestamp}.".encode() + raw_body
    expected = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    # compare_digest, not ==: a short-circuiting comparison leaks the signature one byte at a
    # time to anyone who can measure response latency.
    if not any(hmac.compare_digest(expected, candidate) for candidate in signatures):
        raise SignatureError("signature mismatch")

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise WebhookError(f"body is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise WebhookError("event payload must be a JSON object")

    event_id = str(payload.get("id") or "")
    event_type = str(payload.get("type") or "")
    if not event_id or not event_type:
        raise WebhookError("event is missing id or type")

    return VerifiedEvent(
        event_id=event_id,
        event_type=event_type,
        payload=payload,
        digest=hashlib.sha256(raw_body).hexdigest(),
    )


async def already_processed(session, event_id: str) -> bool:
    from nexus.models.billing import BillingWebhookEvent

    return await session.get(BillingWebhookEvent, event_id) is not None


async def mark_processed(
    session,
    event: VerifiedEvent,
    *,
    status: str = "processed",
    subject_tenant_id: str | None = None,
    note: str = "",
) -> None:
    from nexus.models.billing import BillingWebhookEvent

    session.add(
        BillingWebhookEvent(
            id=event.event_id,
            provider="stripe",
            event_type=event.event_type,
            status=status,
            subject_tenant_id=subject_tenant_id,
            payload_digest=event.digest,
            note=note[:500],
        )
    )
    await session.flush()


def _object(event: VerifiedEvent) -> dict:
    data = event.payload.get("data") or {}
    obj = data.get("object") or {}
    return obj if isinstance(obj, dict) else {}


async def handle_event(session, event: VerifiedEvent) -> dict:
    """Apply a verified, not-yet-seen event.

    Only events that change money state are acted on; everything else is recorded and ignored,
    so an expanded Stripe event list can never break the endpoint.
    """
    from sqlalchemy import select

    from nexus.core.db import utcnow
    from nexus.models.billing import BillingInvoice

    obj = _object(event)
    reference = str(obj.get("id") or "")
    outcome: dict = {"event": event.event_type, "reference": reference, "applied": False}

    if event.event_type not in (
        "payment_intent.succeeded",
        "payment_intent.payment_failed",
        "charge.refunded",
        "charge.dispute.created",
    ):
        return outcome

    # Find the invoice this event concerns by the reference we stored when collecting. Reading
    # across tenants is correct here: the event arrives with no tenant context, and the
    # provider reference is globally unique.
    invoices = (
        await session.scalars(
            select(BillingInvoice).where(
                BillingInvoice.status.in_(("finalized", "paid"))
            )
        )
    ).all()
    invoice = next(
        (i for i in invoices if (i.meta or {}).get("psp_reference") == reference), None
    )
    if invoice is None:
        outcome["note"] = "no matching invoice"
        return outcome

    outcome["invoice_id"] = invoice.id
    outcome["tenant_id"] = invoice.tenant_id
    meta = dict(invoice.meta or {})

    if event.event_type == "payment_intent.succeeded":
        invoice.status = "paid"
        meta["confirmed_by_webhook_at"] = utcnow().isoformat()
    elif event.event_type == "payment_intent.payment_failed":
        # Stays finalized, not "failed": the money state is "not collected yet". Dunning owns
        # what happens next; silently voiding a real debt would be worse than leaving it open.
        invoice.status = "finalized"
        meta["last_payment_error"] = str(
            (obj.get("last_payment_error") or {}).get("message", "")
        )[:300]
        meta["payment_failed_at"] = utcnow().isoformat()
    elif event.event_type == "charge.refunded":
        meta["refunded_at"] = utcnow().isoformat()
        meta["amount_refunded"] = obj.get("amount_refunded")
    elif event.event_type == "charge.dispute.created":
        meta["disputed_at"] = utcnow().isoformat()
        meta["dispute_reason"] = str(obj.get("reason", ""))[:120]

    invoice.meta = meta
    await session.flush()
    outcome["applied"] = True
    outcome["status"] = invoice.status
    return outcome
