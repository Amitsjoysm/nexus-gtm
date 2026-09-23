"""Gmail and Graph "something changed" webhooks (spec §6, §12).

Both are public by necessity and do the same small thing: prove the notification is genuine, find
the mailbox it is about, and enqueue a sync of that mailbox. They never read mail themselves — the
worker does, from the stored cursor — so a replayed or duplicated notification costs one extra sync
that finds nothing new.

Both answer 2xx for anything they choose to ignore (an unknown mailbox, the engine switched off):
Pub/Sub and Graph retry a non-2xx for days, and retrying cannot make an ignored notification
relevant. A notification that fails verification is 401/403, which is what those services expect.
"""
from __future__ import annotations

import base64
import json
import logging

from fastapi import APIRouter, HTTPException, Request, Response, status

logger = logging.getLogger("nexus.engagement.replies")

router = APIRouter(prefix="/engagement/webhooks", tags=["engagement-webhooks"])


async def _enqueue_sync(tenant_id: str, mailbox_id: str) -> None:
    from nexus.workers.tasks import enqueue_sync_mailbox

    await enqueue_sync_mailbox(tenant_id, mailbox_id)


async def _mailbox_where(*where):
    """Cross-tenant lookup by address or subscription id — the notification names no tenant."""
    from sqlalchemy import select

    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.engagement import MailboxConnection

    async with get_platform_sessionmaker()() as session:
        row = (await session.execute(
            select(MailboxConnection.tenant_id, MailboxConnection.id)
            .where(MailboxConnection.status == "connected", *where).limit(1))).first()
    return row


@router.post("/gmail", status_code=204, response_model=None)
async def gmail_push(request: Request) -> Response:
    from nexus.engagement import config
    from nexus.engagement.replies.notifications import (
        PushRejected,
        google_jwks,
        verify_push_token,
    )
    from nexus.models.engagement import MailboxConnection

    header = request.headers.get("authorization", "")
    token = header[7:] if header.lower().startswith("bearer ") else ""
    try:
        verify_push_token(token, jwks=await google_jwks(), audience=config.gmail_push_audience(),
                          service_account=config.gmail_push_service_account())
    except PushRejected as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "push not verified") from exc
    if not config.campaigns_enabled():
        return Response(status_code=204)
    try:
        envelope = await request.json()
        data = json.loads(base64.b64decode(envelope["message"]["data"]).decode("utf-8"))
        address = str(data.get("emailAddress") or "").strip().lower()
    except Exception:
        return Response(status_code=204)     # malformed: acknowledge, never retry forever
    found = await _mailbox_where(MailboxConnection.email == address) if address else None
    if found is not None:
        await _enqueue_sync(found[0], found[1])
    return Response(status_code=204)


@router.post("/graph", response_model=None)
async def graph_notification(request: Request) -> Response:
    from nexus.engagement import config
    from nexus.engagement.replies.notifications import valid_client_state
    from nexus.models.engagement import MailboxConnection

    token = request.query_params.get("validationToken")
    if token is not None:
        # The subscription handshake: echo the token as plain text within ten seconds.
        return Response(content=token, media_type="text/plain", status_code=200)
    try:
        payload = await request.json()
    except Exception:
        return Response(status_code=202)
    queued: set[tuple[str, str]] = set()
    for item in payload.get("value", []) or []:
        subscription_id = str(item.get("subscriptionId") or "")
        if not valid_client_state(subscription_id, str(item.get("clientState") or "")):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "clientState does not verify")
        if not config.campaigns_enabled():
            continue
        found = await _mailbox_where(
            MailboxConnection.notification_subscription_id == subscription_id)
        if found is not None and tuple(found) not in queued:
            queued.add(tuple(found))
            await _enqueue_sync(found[0], found[1])
    return Response(status_code=202)
