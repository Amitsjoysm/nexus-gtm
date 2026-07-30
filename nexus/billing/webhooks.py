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
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # annotation-only; the runtime import stays function-local
    from datetime import datetime

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


# ---- provider state -> our state ---------------------------------------------------------------

# Stripe's subscription statuses mapped onto SUBSCRIPTION_STATUSES. No new status is invented:
# a vocabulary that grows every time a provider adds a state is a vocabulary rating and
# entitlements can no longer reason about.
#
#   unpaid            -> past_due. Stripe means "retries exhausted, still owed". That is exactly
#                        what our own dunning escalates to, and it is the state that keeps the
#                        debt visible. Mapping it to `suspended` would imply we cut off access,
#                        which is a product decision no webhook should make on its own.
#   incomplete        -> deliberately ABSENT. The subscription never started; there is nothing to
#                        mirror, and guessing would either cancel a live customer or activate one
#                        who has not paid. Unmapped statuses leave our row untouched.
STRIPE_SUBSCRIPTION_STATUS = {
    "active": "active",
    "trialing": "trialing",
    "past_due": "past_due",
    "unpaid": "past_due",
    "canceled": "canceled",
    "incomplete_expired": "canceled",
    "paused": "suspended",
}

SUBSCRIPTION_EVENTS = (
    "checkout.session.completed",
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
)

INVOICE_EVENTS = (
    "invoice.paid",
    "invoice.payment_failed",
    "invoice.finalized",
)


def _epoch(value) -> "datetime | None":
    """Stripe timestamps are unix seconds. Anything else is ignored rather than guessed at."""
    from datetime import datetime, timezone

    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


def _metadata(obj: dict) -> dict:
    meta = obj.get("metadata")
    return meta if isinstance(meta, dict) else {}


def _str(value) -> str:
    """Stripe expands some references into objects; keep only the id either way."""
    if isinstance(value, dict):
        return str(value.get("id") or "")
    return str(value or "")


async def _resolve_subscription(session, obj: dict, event: VerifiedEvent):
    """Find the ``billing_subscriptions`` row this provider object concerns.

    Three keys, most specific first. ``metadata.tenant_id`` is stamped by
    ``create_checkout_session`` onto both the session and the subscription, so it is present for
    anything we originated; the id lookups cover subscriptions created in the Stripe dashboard
    and anything predating that stamping.

    Reading across tenants is correct here and is why the endpoint uses the platform session: the
    event arrives with no tenant context at all.
    """
    from sqlalchemy import select

    from nexus.models.billing import BillingSubscription

    meta = _metadata(obj)
    is_subscription_object = event.event_type.startswith("customer.subscription.")
    psp_sub_id = _str(obj.get("id")) if is_subscription_object else _str(obj.get("subscription"))
    psp_cus_id = _str(obj.get("customer"))
    tenant_id = str(meta.get("tenant_id") or obj.get("client_reference_id") or "")

    if psp_sub_id:
        row = (
            await session.scalars(
                select(BillingSubscription).where(
                    BillingSubscription.psp_subscription_id == psp_sub_id
                )
            )
        ).first()
        if row is not None:
            return row, tenant_id or row.tenant_id

    if tenant_id:
        rows = list(
            (
                await session.scalars(
                    select(BillingSubscription)
                    .where(BillingSubscription.tenant_id == tenant_id)
                    .order_by(BillingSubscription.created_at.desc())
                )
            ).all()
        )
        if rows:
            # Prefer the row already linked to this provider subscription, then any live one.
            return (
                next((r for r in rows if r.psp_subscription_id == psp_sub_id), None)
                or next(
                    (r for r in rows if r.status in ("trialing", "active", "past_due")), rows[0]
                ),
                tenant_id,
            )

    if psp_cus_id:
        row = (
            await session.scalars(
                select(BillingSubscription).where(
                    BillingSubscription.psp_customer_id == psp_cus_id
                )
            )
        ).first()
        if row is not None:
            return row, row.tenant_id

    return None, tenant_id


async def _apply_subscription_event(session, event: VerifiedEvent, obj: dict, outcome: dict) -> dict:
    """Mirror a Checkout completion or a subscription lifecycle change into our own row.

    Every write here is an ABSOLUTE assignment — ``status = mapped``, not ``status += 1`` — so a
    redelivery of the same event, or an out-of-order pair, converges on the provider's view
    instead of accumulating. That is what makes replay a genuine no-op rather than a no-op only
    because the event-id primary key happened to catch it first.
    """
    from nexus.core.db import utcnow
    from nexus.core.tenancy import apply_rls
    from nexus.models.billing import BillingPlan, BillingSubscription

    meta_in = _metadata(obj)
    sub, tenant_id = await _resolve_subscription(session, obj, event)

    if sub is None and tenant_id:
        # A tenant that completed Checkout but somehow has no subscription row (the boot-time
        # backfill guarantees one, so this is belt-and-braces). Only creatable when the plan is
        # one we actually know: a subscription pointing at a plan that does not exist would
        # break the entitlement chain for that workspace.
        plan_id = str(meta_in.get("plan_id") or "")
        if plan_id and await session.get(BillingPlan, plan_id) is not None:
            await apply_rls(session, tenant_id)
            sub = BillingSubscription(tenant_id=tenant_id, plan_id=plan_id, status="active")
            session.add(sub)
            await session.flush()
            outcome["created"] = True

    if sub is None:
        outcome["note"] = "no matching subscription"
        return outcome

    # Bind the tenant GUC before writing. Under the app's RLS-bound role an unbound UPDATE is
    # rejected outright; the platform session runs as the owner, but binding costs nothing and
    # keeps this correct if the webhook is ever moved off the owner role.
    await apply_rls(session, sub.tenant_id)

    outcome["tenant_id"] = sub.tenant_id
    outcome["subscription_id"] = sub.id
    meta = dict(sub.meta or {})

    if event.event_type == "checkout.session.completed":
        # A completed Checkout says "this customer now has a subscription at the provider".
        # Record the linkage; the authoritative status/period arrive in the
        # customer.subscription.created event that follows.
        psp_sub_id = _str(obj.get("subscription"))
        psp_cus_id = _str(obj.get("customer"))
        if psp_sub_id:
            sub.psp_subscription_id = psp_sub_id
        if psp_cus_id:
            sub.psp_customer_id = psp_cus_id
        plan_id = str(meta_in.get("plan_id") or "")
        if plan_id and await session.get(BillingPlan, plan_id) is not None:
            sub.plan_id = plan_id
            # Taking a new plan means taking its terms — the same rule change_plan applies.
            sub.grandfathered = False
        if str(obj.get("payment_status") or "") in ("paid", "no_payment_required"):
            sub.status = "active"
            sub.cancel_at_period_end = False
        meta["checkout_session_id"] = _str(obj.get("id"))
    elif event.event_type == "customer.subscription.deleted":
        # Terminal, and terminal in one direction only. Stripe has stopped billing; leaving us
        # "active" would keep entitlements open for a customer who is no longer paying.
        sub.status = "canceled"
        sub.cancel_at_period_end = False
        meta["canceled_by_provider_at"] = utcnow().isoformat()
    else:  # customer.subscription.created | updated
        sub.psp_subscription_id = _str(obj.get("id")) or sub.psp_subscription_id
        sub.psp_customer_id = _str(obj.get("customer")) or sub.psp_customer_id
        raw_status = str(obj.get("status") or "")
        mapped = STRIPE_SUBSCRIPTION_STATUS.get(raw_status)
        if mapped:
            sub.status = mapped
        elif raw_status:
            # Unknown to us: record it, change nothing. A provider adding a status must never
            # silently move a customer into or out of a paying state.
            meta["unmapped_provider_status"] = raw_status[:40]
        sub.cancel_at_period_end = bool(obj.get("cancel_at_period_end"))
        start = _epoch(obj.get("current_period_start"))
        end = _epoch(obj.get("current_period_end"))
        if start is not None:
            sub.current_period_start = start
        if end is not None:
            sub.current_period_end = end
        trial_end = _epoch(obj.get("trial_end"))
        if trial_end is not None:
            sub.trial_end = trial_end
        plan_id = str(meta_in.get("plan_id") or "")
        if plan_id and plan_id != sub.plan_id and await session.get(BillingPlan, plan_id):
            sub.plan_id = plan_id
            sub.grandfathered = False
        outcome["provider_status"] = raw_status

    meta["psp_synced_from_event"] = event.event_id
    sub.meta = meta
    await session.flush()
    outcome["applied"] = True
    outcome["status"] = sub.status
    outcome["plan_id"] = sub.plan_id
    return outcome


async def _apply_invoice_event(session, event: VerifiedEvent, obj: dict, outcome: dict) -> dict:
    """Drive our invoice row (and therefore dunning) from a provider invoice event.

    Deliberately does NOT touch subscription status. ``customer.subscription.updated`` is the
    provider's own statement about that, and two handlers writing the same field from different
    events is how a customer ends up flapping between ``active`` and ``past_due``.
    """
    from sqlalchemy import select

    from nexus.core.db import utcnow
    from nexus.core.tenancy import apply_rls
    from nexus.models.billing import BillingInvoice

    psp_invoice_id = _str(obj.get("id"))
    payment_intent = _str(obj.get("payment_intent"))

    sub, _tenant_id = await _resolve_subscription(session, obj, event)
    if sub is not None:
        outcome["tenant_id"] = sub.tenant_id

    # Match our own invoice by either reference we could have stored: the PaymentIntent written
    # by collect_invoice, or the provider invoice id written by a previous event in this family.
    candidates = list(
        (
            await session.scalars(
                select(BillingInvoice).where(
                    BillingInvoice.status.in_(("finalized", "paid"))
                )
            )
        ).all()
    )
    invoice = next(
        (
            i for i in candidates
            if (payment_intent and (i.meta or {}).get("psp_reference") == payment_intent)
            or (psp_invoice_id and (i.meta or {}).get("psp_invoice_id") == psp_invoice_id)
        ),
        None,
    )

    if invoice is None:
        # Common and harmless: Stripe issues its own invoices for a hosted subscription, and we
        # have no row for them. Record the reference against the subscription so an operator can
        # still trace the money, but claim nothing was applied to an invoice.
        if sub is None:
            outcome["note"] = "no matching invoice or subscription"
            return outcome
        await apply_rls(session, sub.tenant_id)
        sub.meta = {
            **(sub.meta or {}),
            "psp_latest_invoice_id": psp_invoice_id,
            "psp_latest_invoice_status": str(obj.get("status") or "")[:40],
            "psp_latest_invoice_event": event.event_type,
        }
        await session.flush()
        outcome["note"] = "recorded on subscription; no local invoice"
        outcome["applied"] = True
        return outcome

    await apply_rls(session, invoice.tenant_id)
    outcome["invoice_id"] = invoice.id
    outcome["tenant_id"] = invoice.tenant_id
    meta = dict(invoice.meta or {})
    meta["psp_invoice_id"] = psp_invoice_id or meta.get("psp_invoice_id", "")

    if event.event_type == "invoice.paid":
        invoice.status = "paid"
        meta["confirmed_by_webhook_at"] = utcnow().isoformat()
        # A recovered invoice must leave the dunning queue, or the next sweep charges a card for
        # money the provider has already taken.
        meta.pop("next_attempt_at", None)
    elif event.event_type == "invoice.payment_failed":
        # Stays finalized. The debt is real; dunning decides what happens next, and it keys off
        # payment_failed_at (nexus/billing/dunning.py::is_due).
        invoice.status = "finalized"
        meta["payment_failed_at"] = utcnow().isoformat()
        meta["last_payment_error"] = str(
            ((obj.get("last_finalization_error") or {}) or {}).get("message", "")
            or "provider reported invoice.payment_failed"
        )[:300]
    elif event.event_type == "invoice.finalized":
        # Informational: the provider has issued the invoice. Our own finalization is our own
        # (nexus/billing/rating.py) and is not overridden here.
        meta["psp_finalized_at"] = utcnow().isoformat()
        meta["psp_amount_due_cents"] = obj.get("amount_due")

    invoice.meta = meta
    await session.flush()
    outcome["applied"] = True
    outcome["status"] = invoice.status
    return outcome


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

    # Subscription and invoice lifecycle (M12). Without these, a cancellation or a card change
    # made in the provider's own portal never reaches our database, and the two diverge — which
    # in billing is a legal problem, not a bug.
    if event.event_type in SUBSCRIPTION_EVENTS:
        return await _apply_subscription_event(session, event, obj, outcome)
    if event.event_type in INVOICE_EVENTS:
        return await _apply_invoice_event(session, event, obj, outcome)

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
