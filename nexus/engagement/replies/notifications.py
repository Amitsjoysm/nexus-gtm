"""Keeping "something changed" notifications alive, and trusting the ones that arrive (spec §6, §12).

**Subscriptions expire, so they are renewed.** A Gmail `users.watch` lasts seven days; a Graph
subscription on messages at most a few days. `renew_due` re-subscribes anything expiring within a
day. A lapsed one costs latency, not replies: the heartbeat polls every mailbox as the fallback.

**Trust, per provider — neither webhook has a shared secret in its URL:**

* **Gmail** pushes through Pub/Sub with a Google-signed OIDC token. `verify_push_token` checks it
  against Google's published keys, this deployment's audience and the configured push service
  account. With no service account configured the webhook refuses everything rather than accept an
  unauthenticated push.
* **Graph** echoes a validation token once, at subscription time, and then posts notifications that
  carry the `clientState` we chose: an HMAC of the subscription id under `secret_key`, so a forged
  notification cannot name a subscription and pass.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
from datetime import datetime, timedelta

logger = logging.getLogger("nexus.engagement.replies")

GOOGLE_CERTS = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")
RENEW_WITHIN = timedelta(hours=24)
_CERTS_TTL = 3600.0
_certs_cache: tuple[float, dict] | None = None


class PushRejected(PermissionError):
    """The notification could not be proven to come from the provider."""


def client_state(subscription_id: str) -> str:
    from nexus.core.config import get_settings

    key = get_settings().secret_key.encode()
    return hmac.new(key, f"engagement:graph:{subscription_id}".encode(),
                    hashlib.sha256).hexdigest()[:64]


def client_state_seed() -> str:
    """The clientState for a subscription whose id we do not know yet (it is assigned on create):
    keyed on the deployment, and replaced by the id-keyed value on the first renewal."""
    return client_state("new")


def valid_client_state(subscription_id: str, value: str) -> bool:
    return any(hmac.compare_digest(value or "", candidate)
               for candidate in (client_state(subscription_id), client_state_seed()))


def verify_push_token(token: str, *, jwks: dict, audience: str, service_account: str,
                      now: float | None = None) -> dict:
    """The claims of a valid Google push token, or `PushRejected`. Pure given the key set."""
    from jose import JWTError, jwt

    if not service_account:
        raise PushRejected("no push service account is configured")
    if not token:
        raise PushRejected("no bearer token")
    try:
        claims = jwt.decode(token, jwks, algorithms=["RS256"], audience=audience,
                            options={"verify_at_hash": False})
    except JWTError as exc:
        raise PushRejected(f"invalid token: {exc}") from exc
    if claims.get("iss") not in GOOGLE_ISSUERS:
        raise PushRejected("the token was not issued by Google")
    if (claims.get("email") or "").lower() != service_account.lower():
        raise PushRejected("the token is for another service account")
    if claims.get("email_verified") is not True:
        raise PushRejected("the service account email is not verified")
    if now is not None and float(claims.get("exp", 0)) < now:
        raise PushRejected("the token has expired")
    return claims


async def google_jwks() -> dict:
    """Google's current signing keys, cached for an hour (they rotate, slowly)."""
    import httpx

    global _certs_cache
    if _certs_cache is not None and time.monotonic() - _certs_cache[0] < _CERTS_TTL:
        return _certs_cache[1]
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(GOOGLE_CERTS)
        resp.raise_for_status()
        keys = resp.json()
    _certs_cache = (time.monotonic(), keys)
    return keys


async def renew(ts, mailbox, *, now: datetime) -> str:
    """(Re)subscribe one mailbox. Returns what happened; never raises."""
    from nexus.engagement import config
    from nexus.engagement.mailboxes.registry import open_provider

    try:
        provider = await open_provider(ts, mailbox)
        if mailbox.provider == "google":
            topic = config.gmail_pubsub_topic()
            if not topic:
                return "no_topic"
            subscription_id, expires = await provider.watch(topic=topic)
        else:
            url = config.graph_notification_url()
            if not url:
                return "no_public_url"
            existing = mailbox.notification_subscription_id or ""
            subscription_id, expires = await provider.watch(
                notification_url=url, subscription_id=existing,
                client_state=client_state(existing) if existing else client_state_seed())
        mailbox.notification_subscription_id = subscription_id or None
        mailbox.notifications_expire_at = expires
        await ts.flush()
        return "renewed"
    except Exception as exc:
        logger.warning("could not renew notifications for mailbox %s", mailbox.id, exc_info=True)
        return f"failed:{type(exc).__name__}"


def renewal_due(mailbox, now: datetime) -> bool:
    expires = mailbox.notifications_expire_at
    if expires is None:
        return True
    if expires.tzinfo is None:
        from datetime import UTC

        expires = expires.replace(tzinfo=UTC)
    return expires - now < RENEW_WITHIN
