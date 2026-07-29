# nexus/api/routers/billing_webhooks.py
"""Public payment-provider webhook endpoint.

Deliberately unauthenticated in the usual sense — the provider cannot present a bearer token.
Trust comes entirely from the HMAC signature, the freshness window, and the exactly-once table.
See nexus/billing/webhooks.py for why each of those three is load-bearing.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Header, Request, Response, status

from nexus.core.db import get_platform_sessionmaker

logger = logging.getLogger("nexus.billing.webhooks")

router = APIRouter(prefix="/billing/webhooks", tags=["billing"])


@router.post("/stripe")
async def stripe_webhook(
    request: Request,
    response: Response,
    stripe_signature: str = Header(default="", alias="Stripe-Signature"),
) -> dict:
    """Verify, deduplicate, and apply one Stripe event.

    Status codes matter here because they drive the provider's retry behaviour:
      * 400 — bad signature or malformed body. Never retry; it will never become valid.
      * 200 — accepted, or a duplicate we have already applied. Stop retrying.
      * 500 — our fault. Please retry.
    """
    from nexus.billing.webhooks import (
        SignatureError,
        StaleWebhookError,
        WebhookError,
        already_processed,
        handle_event,
        mark_processed,
        verify_stripe_signature,
    )
    from nexus.core.config import get_settings

    # The RAW bytes. Parsing and re-serializing changes them and breaks the HMAC — the most
    # common way webhook verification is silently defeated.
    raw = await request.body()

    try:
        event = verify_stripe_signature(
            raw, stripe_signature, get_settings().stripe_webhook_secret
        )
    except (SignatureError, StaleWebhookError, WebhookError) as exc:
        # Log the reason, return a generic message: a precise error tells someone probing the
        # endpoint exactly which check they still need to defeat.
        logger.warning("rejected stripe webhook: %s", exc)
        response.status_code = status.HTTP_400_BAD_REQUEST
        return {"error": "invalid_webhook"}

    # A payment event arrives with no tenant context; the invoice it refers to could belong
    # to any workspace. Under the RLS-bound role the lookup silently matches nothing.
    async with get_platform_sessionmaker()() as session:
        if await already_processed(session, event.event_id):
            # Idempotent success. The provider retries aggressively and a replay must not
            # re-apply the effect, but it also should not look like a failure.
            return {"received": True, "duplicate": True, "event": event.event_type}

        try:
            outcome = await handle_event(session, event)
            await mark_processed(
                session, event,
                subject_tenant_id=outcome.get("tenant_id"),
                note=str(outcome.get("note", "")),
            )
            await session.commit()
        except Exception:
            await session.rollback()
            # 500 so the provider retries. Recording it as processed here would silently drop a
            # real payment event.
            logger.exception("failed to apply stripe webhook %s", event.event_id)
            response.status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
            return {"error": "processing_failed"}

    return {"received": True, "duplicate": False, **outcome}
