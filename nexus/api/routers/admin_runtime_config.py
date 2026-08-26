# nexus/api/routers/admin_runtime_config.py
"""Runtime configuration, and the payment webhook an operator has to wire up by hand.

Gated on ``PRICING_WRITE``, which the ``superadmin`` and ``billing`` presets carry. Several of these
toggles decide whether the platform spends money unattended, so they sit behind the same permission
that sets prices rather than a general read.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from nexus.api.deps import Principal, require_platform_permission
from nexus.billing.audit import record_admin_action
from nexus.billing.permissions import PRICING_WRITE
from nexus.core.db import get_platform_sessionmaker

router = APIRouter(prefix="/admin/runtime", tags=["admin-runtime"])


class SettingIn(BaseModel):
    model_config = {"extra": "forbid"}

    value: object
    # Why. Several of these cost money when switched on; six months later "who turned this on and
    # what did they think it did" is the only question that matters.
    note: str = ""


async def _audit(principal: Principal, action: str, target: str,
                 before: dict, after: dict, note: str = "") -> None:
    async with get_platform_sessionmaker()() as session:
        await record_admin_action(
            session, actor=principal.user_id, action=action, target=target,
            before=before, after=after, note=note,
        )
        await session.commit()


@router.get("/settings", response_model=list[dict])
async def list_runtime_settings(
    _: Principal = Depends(require_platform_permission(PRICING_WRITE)),
) -> list[dict]:
    """Every runtime-settable option with its live value, effect and warning.

    Returns only what the catalog allows. The 150-odd other fields on ``Settings`` are deploy-time
    and are not listed, because listing something the panel cannot change would be a worse answer
    than not listing it.
    """
    from nexus.runtime_config.service import current_values

    return await current_values()


@router.put("/settings/{key}", response_model=dict)
async def set_runtime_setting(
    key: str,
    body: SettingIn,
    principal: Principal = Depends(require_platform_permission(PRICING_WRITE)),
) -> dict:
    """Override one setting. Takes effect on this process at once, everywhere else within 30s."""
    from nexus.core.config import get_settings
    from nexus.runtime_config.service import UnknownSetting, set_override

    before = getattr(get_settings(), key, None)
    try:
        applied = await set_override(key, body.value, note=body.note, user_id=principal.user_id)
    except UnknownSetting as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    await _audit(principal, "runtime_setting.set", key,
                 {"value": before}, {"value": applied}, body.note)
    return {"key": key, "value": applied, "overridden": True,
            "note": "Live on this process now; other processes within 30 seconds."}


@router.delete("/settings/{key}", response_model=dict)
async def clear_runtime_setting(
    key: str,
    principal: Principal = Depends(require_platform_permission(PRICING_WRITE)),
) -> dict:
    """Drop the override so the deployment's environment value applies again."""
    from nexus.core.config import get_settings
    from nexus.runtime_config.service import UnknownSetting, clear_override

    before = getattr(get_settings(), key, None)
    try:
        removed = await clear_override(key)
    except UnknownSetting as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    if not removed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"'{key}' has no override to clear")

    await _audit(principal, "runtime_setting.clear", key, {"value": before}, {})
    return {"key": key, "overridden": False,
            "note": "The environment value applies again from the next refresh."}


# ---- the payment webhook ---------------------------------------------------------------------
# The URL is not ours to set — it is pasted into the Stripe dashboard, and nothing in this
# application can do that for you. What the panel CAN do is stop you guessing it: show the exact
# string to paste, say whether a signing secret is configured, and prove the endpoint is reachable
# and verifying. Before this, an operator had a secret field with no indication of where it goes.


class WebhookProbeIn(BaseModel):
    model_config = {"extra": "forbid"}

    # Defaults to the request's own origin, which is right far more often than not.
    base_url: str = ""


@router.get("/webhook", response_model=dict)
async def webhook_info(
    _: Principal = Depends(require_platform_permission(PRICING_WRITE)),
) -> dict:
    """What to paste into Stripe, and whether our side is ready to receive it."""
    from nexus.billing.credentials import active_credential, resolve_stripe_secrets
    from nexus.billing.webhooks import INVOICE_EVENTS, SUBSCRIPTION_EVENTS
    from nexus.core.config import get_settings

    settings = get_settings()
    _secret, webhook_secret, _pub = await resolve_stripe_secrets()
    cred = await active_credential("stripe")

    return {
        "path": "/api/billing/webhooks/stripe",
        "provider": settings.payment_provider,
        # Which of the two sources the secret came from. An operator who typed one into the panel
        # and is still being served the environment's needs to know that immediately.
        "signing_secret_configured": bool(webhook_secret),
        "signing_secret_source": (
            "control plane" if cred is not None and cred.webhook_secret_encrypted else
            "environment" if webhook_secret else "not set"
        ),
        "stripe_account": (cred.account_id if cred is not None else ""),
        "livemode": bool(cred.livemode) if cred is not None else False,
        "events_handled": sorted(SUBSCRIPTION_EVENTS + INVOICE_EVENTS),
        # Ordered as the operator will actually do it, and each step says where the thing it names
        # lives. "Paste the URL below" was wrong the moment the URL box rendered above the list.
        "instructions": [
            "In the Stripe dashboard: Developers -> Webhooks -> Add endpoint.",
            "Paste the endpoint URL shown above.",
            "Select the events listed here — Stripe sends everything otherwise, and every "
            "unhandled type is a rejected delivery sitting in your dashboard looking like a fault.",
            "Copy the signing secret Stripe then shows you into the Payments tab.",
            "Come back and press Test connection: it posts a correctly signed event at this "
            "endpoint and confirms we accept it.",
        ],
    }


@router.post("/webhook/test", response_model=dict)
async def test_webhook(
    body: WebhookProbeIn | None = None,
    _: Principal = Depends(require_platform_permission(PRICING_WRITE)),
) -> dict:
    """Post a correctly signed event at our own endpoint and report what happened.

    This proves the half we control: that the signing secret in force verifies a real signature and
    the route accepts it. It cannot prove Stripe can *reach* us — that depends on DNS and firewalls
    outside this process — so it does not claim to. A green result here plus a delivery showing in
    the Stripe dashboard is the complete picture.

    The event id is unique per probe, because the webhook table dedupes on it: a fixed id would
    verify once and then return "already processed" forever, which reads as success while proving
    nothing.
    """
    import hashlib
    import hmac
    import json
    import time
    import uuid

    import httpx

    from nexus.billing.credentials import resolve_stripe_secrets

    _sk, webhook_secret, _pub = await resolve_stripe_secrets()
    if not webhook_secret:
        return {
            "ok": False,
            "detail": "No signing secret is configured, so nothing could be verified. Add one on "
                      "the Payments tab first.",
        }

    base = (body.base_url if body else "") or "http://127.0.0.1:8000"
    url = base.rstrip("/") + "/api/billing/webhooks/stripe"

    # `invoice.finalized` on purpose: it is handled, and it carries no state change for an invoice
    # id that does not exist, so a probe cannot alter a real customer's billing.
    payload = json.dumps({
        "id": f"evt_probe_{uuid.uuid4().hex[:16]}",
        "type": "invoice.finalized",
        "data": {"object": {"id": f"in_probe_{uuid.uuid4().hex[:12]}", "metadata": {}}},
    }).encode()
    ts = str(int(time.time()))
    sig = hmac.new(webhook_secret.encode(), f"{ts}.".encode() + payload,
                   hashlib.sha256).hexdigest()

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                url, content=payload,
                headers={"Stripe-Signature": f"t={ts},v1={sig}",
                         "Content-Type": "application/json"},
            )
    except Exception as exc:
        return {"ok": False, "url": url,
                "detail": f"Could not reach our own endpoint at {url}: {exc!r}"}

    ok = resp.status_code == 200
    return {
        "ok": ok,
        "url": url,
        "http_status": resp.status_code,
        "detail": (
            "A correctly signed event was accepted. The signing secret in force is valid and the "
            "route is live. This does not prove Stripe can reach this host — check for a delivery "
            "in the Stripe dashboard for that."
            if ok else
            f"The endpoint refused a correctly signed event ({resp.status_code}). The signing "
            f"secret in force does not match the one this probe signed with, or the route is not "
            f"mounted where expected."
        ),
    }
